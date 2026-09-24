"""catalog.py - the registry reader. A `ThreadingHTTPServer` stands in for the real registry
(and, for the license lookups, for PyPI's JSON API) so no test hits the network."""
from __future__ import annotations

import calendar
import json
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pathfinder import catalog, config
from pathfinder.ledger import Ledger
from pathfinder.senses import catalog as catalog_sense
from pathfinder.state import State

FIXTURE_NPM_ITEM = {
    "server": {
        "name": "io.github.example/myapp-tool",
        "description": "A myapp helper server for testing.",
        "version": "1.2.3",
        "repository": {"url": "https://github.com/example/myapp-tool", "source": "github"},
        "packages": [{
            "registryType": "npm",
            "registryBaseUrl": "https://registry.npmjs.org",
            "identifier": "myapp-tool-mcp",
            "version": "1.2.3",
            "runtimeHint": "npx",
            "transport": {"type": "stdio"},
            "runtimeArguments": [{"value": "-y", "type": "positional"}],
            "environmentVariables": [
                {"name": "API_KEY", "description": "a key", "isRequired": True, "isSecret": True},
                {"name": "OPTIONAL_FLAG", "description": "optional"},
            ],
        }],
        "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {
        "status": "active", "publishedAt": "2026-09-01T00:00:00Z",
        "updatedAt": "2026-09-20T00:00:00Z", "isLatest": True}},
}

FIXTURE_REMOTE_ITEM = {
    "server": {
        "name": "io.github.example/myapp-remote",
        "description": "A remote-only myapp server for testing.",
        "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://example.com/remote-mcp"}],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {
        "status": "active", "publishedAt": "2026-09-01T00:00:00Z",
        "updatedAt": "2026-09-01T00:00:00Z", "isLatest": True}},
}


def _pypi_item(name, identifier, version):
    return {
        "server": {
            "name": name, "description": f"{name} pypi package for testing.", "version": version,
            "packages": [{"registryType": "pypi", "identifier": identifier, "version": version,
                         "transport": {"type": "stdio"}}],
        },
        "_meta": {"io.modelcontextprotocol.registry/official": {
            "status": "active", "publishedAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-20T00:00:00Z", "isLatest": True}},
    }


class _RegistryHandler(BaseHTTPRequestHandler):
    """Serves `/v0.1/servers` (the registry) and `/<identifier>/<version>/json` (PyPI's shape) -
    everything the module needs to hit over HTTP, one fake process."""
    items: list = []
    metadata_extra: dict = {}
    pypi_licenses: dict = {}
    requests: list = []

    def log_message(self, *a):  # keep test output clean
        pass

    def do_GET(self):
        cls = type(self)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v0.1/servers":
            cls.requests.append(urllib.parse.parse_qs(parsed.query))
            body = json.dumps({"servers": cls.items,
                              "metadata": {"count": len(cls.items), **cls.metadata_extra}}).encode()
        else:
            parts = parsed.path.strip("/").split("/")
            lic = cls.pypi_licenses.get(tuple(parts[:2])) if len(parts) >= 2 else None
            if lic is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"info": {"license": lic}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FakeRegistryTest(unittest.TestCase):
    def setUp(self):
        _RegistryHandler.items = []
        _RegistryHandler.metadata_extra = {}
        _RegistryHandler.pypi_licenses = {}
        _RegistryHandler.requests = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RegistryHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FetchTest(_FakeRegistryTest):
    def test_fetch_paginates_and_stops_at_five_pages(self):
        _RegistryHandler.items = [FIXTURE_NPM_ITEM]
        _RegistryHandler.metadata_extra = {"nextCursor": "same-cursor-forever"}
        items = catalog.fetch(since=1758000000.0, search="thing", registry=self.base_url, pages=5)
        self.assertEqual(len(_RegistryHandler.requests), 5)
        self.assertEqual(len(items), 5)  # one item per page, five pages
        first = _RegistryHandler.requests[0]
        self.assertEqual(first["search"], ["thing"])
        self.assertIn("updated_since", first)
        self.assertTrue(first["updated_since"][0].endswith("Z"))
        self.assertEqual(_RegistryHandler.requests[1]["cursor"], ["same-cursor-forever"])


class CandidateTest(unittest.TestCase):
    def test_candidate_from_registry_entry(self):
        cand = catalog.candidate(FIXTURE_NPM_ITEM)
        self.assertEqual(set(cand), set(catalog.SPEC_FIELDS))
        self.assertEqual(cand["name"], "io.github.example/myapp-tool")
        self.assertEqual(cand["version"], "1.2.3")
        self.assertEqual(cand["source_registry"],
                         {"url": catalog.REGISTRY, "entry_id": "io.github.example/myapp-tool",
                          "updated_at": "2026-09-20T00:00:00Z"})
        pkg = cand["package"]
        self.assertEqual(pkg["registry_type"], "npm")  # npm chosen over the entry's own remotes
        self.assertEqual(pkg["identifier"], "myapp-tool-mcp")
        self.assertEqual(pkg["runtime"], "npx")
        self.assertEqual(pkg["args"], ["-y", f"{pkg['identifier']}@{pkg['version']}"])
        self.assertEqual(pkg["env"], [
            {"name": "API_KEY", "secret": True, "required": True},
            {"name": "OPTIONAL_FLAG", "secret": False, "required": False},
        ])
        self.assertEqual(pkg["remotes"], FIXTURE_NPM_ITEM["server"]["remotes"])
        self.assertEqual(cand["verification"], {
            "license": {"spdx": "not_checked", "source": "not_checked", "verdict": "unknown"},
            "malicious_scan": "not_checked", "sandbox_trial": "not_checked",
            "maintenance": {"last_publish_at": "2026-09-01T00:00:00Z", "namespace_verified": True},
            "description_hash": "not_checked", "trust_tier": "unverified",
        })
        self.assertEqual(cand["install"], "not_checked")
        self.assertEqual(cand["billing"], "unknown")
        self.assertEqual(cand["outcome"], "proposed")

    def test_candidate_from_remote_only_entry(self):
        cand = catalog.candidate(FIXTURE_REMOTE_ITEM)
        self.assertEqual(set(cand), set(catalog.SPEC_FIELDS))
        pkg = cand["package"]
        self.assertEqual(pkg["registry_type"], "remote")
        self.assertEqual(pkg["identifier"], "https://example.com/remote-mcp")
        self.assertEqual(pkg["env"], [])
        self.assertEqual(pkg["remotes"], FIXTURE_REMOTE_ITEM["server"]["remotes"])


class TagsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def _push_context(self, cwd):
        event = self.ledger.append("signal", origin="signal", trust="self", sense="attach",
                                   kind="context", key=f"attach:{cwd}", summary="working",
                                   fields={"cwd": cwd, "project": Path(cwd).name})
        self.state.apply(event)

    def test_tags_from_context_and_markers(self):
        base = Path(self._tmp.name)
        proj = base / "MyProj"
        proj.mkdir()
        (proj / "package.json").write_text("{}")
        (proj / ".git").mkdir()
        vanished = base / "Vanished"  # never created

        self._push_context(str(proj))
        self._push_context(str(vanished))

        tags = catalog.tags(self.state, self.home, time.time())
        self.assertTrue({"myproj", "node", "javascript", "npm", "git", "vanished"} <= tags)


class MatchesTest(unittest.TestCase):
    def test_matches_whole_words(self):
        digital = {"name": "io.github.example/digital-agency", "description": "a digital agency helper"}
        self.assertEqual(catalog.matches(digital, {"git"}), [])  # "git" must not match inside "digital"

        node_fetch = {"name": "io.github.example/node-fetch", "description": ""}
        self.assertEqual(catalog.matches(node_fetch, {"node"}), ["node"])

        underscored = {"name": "io.github.example/git_extra", "description": ""}
        self.assertEqual(catalog.matches(underscored, {"git"}), ["git"])  # "_" is a separator

        namespaced = {"name": "io.github.example/thing", "description": ""}
        self.assertEqual(catalog.matches(namespaced, {"github", "example"}), [])  # the namespace never matches


class _Completed:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout = returncode, stdout


def _fake_run(stdout="", returncode=0):
    return lambda *a, **kw: _Completed(returncode, stdout)


class LicenseTest(unittest.TestCase):
    def _npm_candidate(self):
        return {"package": {"registry_type": "npm", "identifier": "example-pkg", "version": "1.0.0",
                            "runtime": "npx", "args": [], "env": [], "remotes": []}}

    def test_license_verdicts(self):
        cand = self._npm_candidate()
        self.assertEqual(catalog.license(cand, run=_fake_run('"GPL-3.0-only"'))["verdict"], "excluded")
        self.assertEqual(catalog.license(cand, run=_fake_run('"MIT"'))["verdict"], "ok")
        self.assertEqual(catalog.license(cand, run=_fake_run("", returncode=1))["verdict"], "unknown")
        self.assertEqual(catalog.license(cand, run=_fake_run('"UNLICENSED"'))["verdict"], "excluded")
        self.assertEqual(catalog.license(cand, run=_fake_run('"SEE LICENSE IN LICENSE.md"'))["verdict"], "unknown")
        self.assertEqual(catalog.license(cand, run=_fake_run('"gpl-3.0"'))["verdict"], "excluded")

    def test_pypi_license_shapes_are_excluded(self):
        """Review blocker: a classifier, the full license text and lowercase all came back ok."""
        for value in ("License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
                      "GNU AFFERO GENERAL PUBLIC LICENSE Version 3, 19 November 2007 ...",
                      "gpl-3.0-or-later"):
            self.assertEqual(catalog._verdict(value), "excluded", value)
        self.assertEqual(catalog._verdict("Apache-2.0"), "ok")


class PollTest(_FakeRegistryTest):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self._patched_pypi = catalog.PYPI_URL
        catalog.PYPI_URL = self.base_url

    def tearDown(self):
        catalog.PYPI_URL = self._patched_pypi
        self.state.close()
        self._tmp.cleanup()
        super().tearDown()

    def _push_context(self, cwd):
        event = self.ledger.append("signal", origin="signal", trust="self", sense="attach",
                                   kind="context", key=f"attach:{cwd}", summary="working",
                                   fields={"cwd": cwd, "project": Path(cwd).name})
        self.state.apply(event)

    def _deny(self, name):
        pending = self.ledger.append("approval_pending", id=f"deny-{name}", call_id="c1",
                                     tool="install_server", args={"name": name}, messages=[])
        self.state.apply(pending)
        decided = self.ledger.append("approval", id=pending["id"], decision="denied")
        self.state.apply(decided)

    def test_poll_skips_installed_declined_and_excluded(self):
        self._push_context(str(Path(self._tmp.name) / "myapp"))  # a vanished cwd -> tag "myapp"
        config.set(self.home, "mcp_servers",
                  {"myapp-installed": {"transport": "stdio", "command": ["npx", "thing"]}})
        self._deny("io.github.example/myapp-declined")

        installed_item = _pypi_item("io.github.example/myapp-installed", "myapp-installed-pkg", "1.0.0")
        declined_item = _pypi_item("io.github.example/myapp-declined", "myapp-declined-pkg", "1.0.0")
        gpl_item = _pypi_item("io.github.example/myapp-gpl", "myapp-gpl-pkg", "1.0.0")
        clean_item = _pypi_item("io.github.example/myapp-tool", "myapp-tool-pkg", "2.0.0")
        _RegistryHandler.items = [installed_item, declined_item, gpl_item, clean_item]
        _RegistryHandler.pypi_licenses = {
            ("myapp-gpl-pkg", "1.0.0"): "GPL-3.0-only",
            ("myapp-tool-pkg", "2.0.0"): "MIT",
        }

        now = time.time()
        signals = catalog_sense.poll({"_home": self.home, "registry": self.base_url}, now, state=self.state)

        self.assertEqual(len(signals), 1)
        [signal] = signals
        self.assertEqual(signal.key, "catalog:io.github.example/myapp-tool@2.0.0")
        self.assertEqual(signal.kind, "tool_candidate")
        self.assertIn("myapp", signal.summary)
        self.assertIn("io.github.example/myapp-tool", signal.summary)
        self.assertEqual(signal.fields["candidate"]["name"], "io.github.example/myapp-tool")
        self.assertEqual(signal.fields["matched_tags"], ["myapp"])

    def test_since_ignores_tool_held_and_clamps_to_a_week(self):
        """A `tool_held` row (same sense, not a poll) 30 days old must not pin `since` there
        forever - three daily polls finding no candidates still advance/clamp it (review: a
        deaf sense after a quiet week)."""
        self._push_context(str(Path(self._tmp.name) / "myapp"))
        now = time.time()
        self.state.db.execute(
            "INSERT INTO signals (key, ts, sense, kind) VALUES (?, ?, ?, ?)",
            ("catalog:tool_held:x", now - 30 * 86400, "catalog", "tool_held"))
        _RegistryHandler.items = []

        for day in range(3):
            t = now + day * 86400
            catalog_sense.poll({"_home": self.home, "registry": self.base_url}, t, state=self.state)
            since_str = _RegistryHandler.requests[-1]["updated_since"][0]
            since = calendar.timegm(time.strptime(since_str, "%Y-%m-%dT%H:%M:%SZ"))
            self.assertGreaterEqual(since, t - 7 * 86400 - 1)

    def test_poll_skips_a_malformed_item_and_keeps_going(self):
        self._push_context(str(Path(self._tmp.name) / "myapp"))
        bad_item = {"server": {"description": "no name field", "version": "1.0.0"}}  # candidate() KeyErrors
        matching_item = _pypi_item("io.github.example/myapp-tool", "myapp-tool-pkg", "2.0.0")
        other_item = _pypi_item("io.github.example/other-thing", "other-thing-pkg", "1.0.0")  # untagged
        _RegistryHandler.items = [bad_item, matching_item, other_item]
        _RegistryHandler.pypi_licenses = {("myapp-tool-pkg", "2.0.0"): "MIT"}

        signals = catalog_sense.poll({"_home": self.home, "registry": self.base_url}, time.time(), state=self.state)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].fields["candidate"]["name"], "io.github.example/myapp-tool")


class SearchToolsTest(_FakeRegistryTest):
    def setUp(self):
        super().setUp()
        self._patched_registry = catalog.REGISTRY
        catalog.REGISTRY = self.base_url

    def tearDown(self):
        catalog.REGISTRY = self._patched_registry
        super().tearDown()

    def test_search_tools_returns_lines(self):
        _RegistryHandler.items = [FIXTURE_NPM_ITEM, FIXTURE_REMOTE_ITEM]
        lines = catalog_sense._search_tools("myapp")
        self.assertEqual(len(lines), 2)
        self.assertIn("io.github.example/myapp-tool", lines[0])
        self.assertIn("npm", lines[0])
        self.assertIn("license not checked", lines[0])
        self.assertIn("remote", lines[1])


if __name__ == "__main__":
    unittest.main()
