"""installer.py - check, propose, install, roll back, uninstall. Every network/subprocess call
`check()` makes is injected (`run`, `urlopen`, `which`); the trial launch is the one real thing
that runs - against `tests/mcp_fake_server.py`, a real child process, no model credits."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pathfinder import config, tools as tools_mod
from pathfinder.installer import Installer, render_row, server_id
from pathfinder.ledger import Ledger
from pathfinder.state import State

FAKE_SERVER = Path(__file__).parent / "mcp_fake_server.py"

# The one real thing check()'s trial spawns (module docstring) - "npx"/"uvx" resolve to this
# script (chmod +x, a shebang, `parse_known_args` in main()) instead of a real registry launcher,
# so the pinned-spec/runtime-flag argv B3 now requires still runs offline, no model credits.
_WHICH = {"npx": str(FAKE_SERVER), "uvx": str(FAKE_SERVER)}


def _candidate(name="io.github.acme/widget", version="1.0.0", *, env=None,
              runtime=None, args=None, registry_type="npm"):
    identifier = "widget"
    return {
        "name": name, "version": version,
        "source_registry": {"url": "https://registry.example", "entry_id": name, "updated_at": "not_checked"},
        "description": "a widget server",
        "package": {
            "registry_type": registry_type, "identifier": identifier, "version": version,
            "runtime": runtime or "npx", "args": args if args is not None else ["-y", f"{identifier}@{version}"],
            "env": env or [], "remotes": [],
        },
        "verification": {
            "license": {"spdx": "not_checked", "source": "not_checked", "verdict": "unknown"},
            "malicious_scan": "not_checked", "sandbox_trial": "not_checked",
            "maintenance": {"last_publish_at": "not_checked", "namespace_verified": True},
            "description_hash": "not_checked", "trust_tier": "unverified",
        },
        "install": "not_checked", "billing": "unknown", "outcome": "proposed",
    }


def _fake_run(license="MIT", integrity="sha512-abc", returncode=0):
    def run(argv, **kwargs):
        stdout = json.dumps({"dist.integrity": integrity, "dist.tarball": "https://x", "license": license})
        return SimpleNamespace(returncode=returncode, stdout=stdout)
    return run


# A canned trial result (one read-only "echo" tool) - swapped in for `Installer._trial` by
# `_mock_trial` so a test of check()/install()'s own logic does not also have to pay for (or
# depend on) a real child process; `test_trial_runs_in_the_sandbox_and_hashes_tools` and
# `test_trial_timeout_fails` are the ones that keep the real thing.
_FAKE_TOOLS = [{"name": "echo", "description_hash": "sha256:" + "a" * 64, "schema_hash": "sha256:" + "b" * 64,
               "read_only_hint": True, "params": ["text"]}]


class FakeServers:
    def __init__(self):
        self._sessions = {}
        self._disabled = {}

    def unload(self, server_id):
        self._sessions.pop(server_id, None)
        self._disabled.pop(server_id, None)

    def disabled(self):
        return list(self._disabled.items())


class FakeAttach:
    """`reload`'s `load_result` (default "ok") stands in for what a real `Attach.reload` would
    have left behind - "ok" registers `mcp__<id>__echo` into the shared `tools.TOOLS` (B5's load
    check reads that same registry `install()` does); "dead" registers nothing (a server that
    dies before listing a tool); any other string is the reason `servers.disabled()` reports for
    `id` (B6's secret-missing case)."""
    def __init__(self):
        self.servers = FakeServers()
        self.reloaded = []
        self.load_result = "ok"

    async def reload(self, id):
        self.reloaded.append(id)
        if self.load_result == "ok":
            tools_mod.TOOLS[f"mcp__{id}__echo"] = None
        elif self.load_result != "dead":
            self.servers._disabled[id] = self.load_result


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.signals = []
        self.attach = FakeAttach()
        self.installer = Installer(self.home, self.state, self._record, self.signals.append,
                                   self.attach, lambda fn: fn())
        self.home_patch = mock.patch.dict("os.environ", {"HOME": str(self.home)})
        self.home_patch.start()
        self.which_patch = mock.patch("pathfinder.installer.shutil.which", side_effect=_WHICH.get)
        self.which_patch.start()

    def tearDown(self):
        for name in [n for n in tools_mod.TOOLS if n.startswith("mcp__")]:
            del tools_mod.TOOLS[name]
        self.which_patch.stop()
        self.home_patch.stop()
        self.state.close()
        self.tmp.cleanup()

    def _record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        return event

    def _check(self, cand, **kw):
        """`check()` with the trial mocked (see `_FAKE_TOOLS`) - every test but the two about the
        trial itself uses this."""
        with mock.patch.object(Installer, "_trial", return_value=(_FAKE_TOOLS, [])):
            return self.installer.check(cand, **kw)

    # -- server_id -------------------------------------------------------------------------
    def test_server_id_from_name(self):
        self.assertEqual(server_id("io.github.acme/widget-server", set()), "widget-server")
        self.assertEqual(server_id("io.github.acme/widget-server", {"widget-server"}), "widget-server-2")
        self.assertEqual(server_id("io.github.acme/widget-server",
                                   {"widget-server", "widget-server-2"}), "widget-server-3")
        self.assertLessEqual(len(server_id("x" * 60, set())), 32)
        self.assertEqual(server_id("io.github.acme/!!!", set()), "server")  # nothing sane left -> fallback

    # -- check: order and stopping ----------------------------------------------------------
    def test_check_fills_verification_in_order_and_stops(self):
        cand = _candidate(runtime="definitely-not-a-real-binary-xyz")
        out = self.installer.check(cand)
        self.assertEqual(out["outcome"], "install_failed")
        self.assertEqual(out["verification"]["failed_step"], "runtime")
        self.assertIn("not found", out["verification"]["reason"])
        self.assertEqual(out["verification"]["license"]["verdict"], "unknown")  # untouched
        self.assertEqual(out["install"], "not_checked")  # never started

    def test_unpinned_version_fails(self):
        cand = _candidate(version="latest")
        out = self._check(cand, run=_fake_run())
        self.assertEqual(out["outcome"], "install_failed")
        self.assertEqual(out["verification"]["failed_step"], "version")
        self.assertIn("must be pinned, got latest", out["verification"]["reason"])

    def test_trial_runs_in_the_sandbox_and_hashes_tools(self):
        cand = _candidate()
        out = self.installer.check(cand, run=_fake_run())
        self.assertNotEqual(out["outcome"], "install_failed")
        tools = out["verification"]["sandbox_trial"]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "echo")
        self.assertTrue(tools[0]["read_only_hint"])
        self.assertTrue(tools[0]["description_hash"].startswith("sha256:"))
        self.assertTrue(tools[0]["schema_hash"].startswith("sha256:"))
        self.assertFalse(out["verification"]["sandbox_trial"]["file_writes_outside_tmp"])
        self.assertEqual(out["verification"]["sandbox_trial"]["verdict"], "ok")
        self.assertEqual(out["verification"]["trust_tier"], "checked")

    def test_trial_timeout_fails(self):
        cand = _candidate(args=["-y", "widget@1.0.0", "--never-answer"])
        out = self.installer.check(cand, run=_fake_run(), trial_timeout=1)
        self.assertEqual(out["outcome"], "install_failed")
        self.assertEqual(out["verification"]["failed_step"], "trial")

    # -- proposal row ------------------------------------------------------------------------
    def test_proposal_row_lists_checks_and_grants(self):
        cand = self._check(_candidate(), run=_fake_run())
        aid = self.installer.propose(cand, "user")
        row = self.state.approval(aid)
        line = render_row(row)
        self.assertIn("install_server", line)
        self.assertIn("license MIT ok", line)
        self.assertIn("integrity ok", line)
        self.assertIn("trial ok, 1 tools (1 read-only)", line)

    def test_secret_missing_proposal_says_what_to_add(self):
        env = [{"name": "TOKEN", "secret": True, "required": True}]
        with mock.patch("pathfinder.installer.keychain.secret", return_value=None):
            cand = self._check(_candidate(env=env), run=_fake_run())
        self.assertEqual(cand["install"]["needs"], ["TOKEN"])
        aid = self.installer.propose(cand, "user")
        line = render_row(self.state.approval(aid))
        self.assertIn("needs: TOKEN", line)

    def test_explicit_request_shows_excluded_license_but_catalog_fails_it(self):
        catalog_check = self._check(_candidate(), run=_fake_run(license="GPL-3.0"))
        self.assertEqual(catalog_check["outcome"], "install_failed")
        self.assertEqual(catalog_check["verification"]["failed_step"], "license")

        explicit = self._check(_candidate(), explicit=True, run=_fake_run(license="GPL-3.0"))
        self.assertNotEqual(explicit["outcome"], "install_failed")
        self.assertEqual(explicit["verification"]["license"]["verdict"], "excluded")
        self.assertTrue(explicit["verification"]["license"]["warning"])
        self.assertEqual(explicit["verification"]["trust_tier"], "checked")  # the rest still ran

    # -- install / rollback / uninstall -------------------------------------------------------
    def _install(self, cand):
        aid = self.installer.propose(cand, "user")
        asyncio.run(self.installer.decide(aid, "approved"))
        return aid

    def test_install_merges_claude_code_non_destructively(self):
        claude_path = self.home / ".claude.json"
        before = {"oneUnrelatedKey": 1, "anotherKey": "x",
                  "mcpServers": {"other-server": {"command": "foo", "args": [], "env": {}}}}
        claude_path.write_text(json.dumps(before), encoding="utf-8")

        env = [{"name": "API_KEY", "secret": True, "required": True}]
        with mock.patch("pathfinder.installer.keychain.secret", return_value="shh"):
            cand = self._check(_candidate(env=env), run=_fake_run())
        self.assertEqual(cand["install"]["needs"], [])
        id = self.installer._server_id(cand["name"])
        self._install(cand)

        cfg = config.load(self.home)
        self.assertIn(id, cfg["mcp_servers"])
        self.assertEqual(cfg["mcp_servers"][id]["env"], {"API_KEY": "keychain:API_KEY"})

        after = json.loads(claude_path.read_text(encoding="utf-8"))
        self.assertEqual(after["oneUnrelatedKey"], 1)
        self.assertEqual(after["anotherKey"], "x")
        self.assertEqual(after["mcpServers"]["other-server"], before["mcpServers"]["other-server"])
        self.assertIn(id, after["mcpServers"])
        self.assertEqual(after["mcpServers"][id]["env"]["API_KEY"], "${API_KEY}")  # never the value
        backups = list(self.home.glob(".claude.json.pathfinder-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), before)
        self.assertEqual(self.attach.reloaded, [id])

    def test_install_rollback(self):
        pre_servers = {"kept": {"transport": "stdio", "command": ["true"], "env": {}}}
        config.set(self.home, "mcp_servers", pre_servers)
        claude_path = self.home / ".claude.json"
        before = {"untouched": True}
        claude_path.write_text(json.dumps(before), encoding="utf-8")

        cand = self._check(_candidate(), run=_fake_run())
        aid = self.installer.propose(cand, "user")

        real_record = self.installer._record

        def failing_record(type, **fields):
            if type == "pin_accept":
                raise RuntimeError("boom")
            return real_record(type, **fields)

        self.installer._record = failing_record
        asyncio.run(self.installer.decide(aid, "approved"))

        self.assertEqual(config.load(self.home)["mcp_servers"], pre_servers)
        self.assertEqual(json.loads(claude_path.read_text(encoding="utf-8")), before)

        events = list(self.ledger.read())
        failed = [e for e in events if e["type"] == "install_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["step"], "pins")

    def test_load_step_fails_when_server_dies(self):
        """B5: `attach.reload` not raising is not enough - a server that dies (or never lists a
        tool) at load must roll the install back, not `install_done`."""
        cand = self._check(_candidate(), run=_fake_run())
        id = self.installer._server_id(cand["name"])
        self.attach.load_result = "dead"
        self._install(cand)
        self.assertNotIn(id, config.load(self.home)["mcp_servers"])
        self.assertEqual(self.state.install(id)["state"], "failed")
        events = [e["type"] for e in self.ledger.read()]
        self.assertNotIn("install_done", events)
        failed = [e for e in self.ledger.read() if e["type"] == "install_failed"]
        self.assertEqual(failed[-1]["step"], "load")

    def test_load_step_ok_when_only_problem_is_a_listed_secret(self):
        """B6: a server held `disabled` only because of a secret the row already listed under
        `needs` is still `install_done`, not rolled back."""
        env = [{"name": "TOKEN", "secret": True, "required": True}]
        with mock.patch("pathfinder.installer.keychain.secret", return_value=None):
            cand = self._check(_candidate(env=env), run=_fake_run())
        id = self.installer._server_id(cand["name"])
        self.attach.load_result = "secret missing: TOKEN"
        self._install(cand)
        self.assertIn(id, config.load(self.home)["mcp_servers"])
        self.assertEqual(self.state.install(id)["state"], "installed")
        self.assertIn("install_done", [e["type"] for e in self.ledger.read()])

    def test_uninstall_is_the_reverse(self):
        """Two installs; uninstalling one leaves the other exactly as installed (B1: uninstall
        must never restore the install-time backup, which would wipe the second server's entry
        along with any later write to the file)."""
        claude_path = self.home / ".claude.json"
        before = {"untouched": True}
        claude_path.write_text(json.dumps(before), encoding="utf-8")

        cand1 = self._check(_candidate(name="io.github.acme/alpha"), run=_fake_run())
        id1 = self.installer._server_id(cand1["name"])
        self._install(cand1)
        cand2 = self._check(_candidate(name="io.github.acme/beta"), run=_fake_run())
        id2 = self.installer._server_id(cand2["name"])
        self._install(cand2)
        # a write neither install nor uninstall should touch or undo (Claude Code's own state)
        after_installs = json.loads(claude_path.read_text(encoding="utf-8"))
        after_installs["numStartups"] = 7
        claude_path.write_text(json.dumps(after_installs), encoding="utf-8")

        asyncio.run(self.installer.uninstall(id1))

        after = json.loads(claude_path.read_text(encoding="utf-8"))
        self.assertNotIn(id1, config.load(self.home)["mcp_servers"])
        self.assertIn(id2, config.load(self.home)["mcp_servers"])
        self.assertNotIn(id1, after["mcpServers"])
        self.assertIn(id2, after["mcpServers"])  # the other install survives
        self.assertEqual(after["numStartups"], 7)  # the later write survives too
        self.assertFalse(any(k.startswith(f"mcp__{id1}__") for k in config.load(self.home)["grants"]))
        self.assertTrue(any(k.startswith(f"mcp__{id2}__") for k in config.load(self.home)["grants"]))
        self.assertEqual(self.state.install(id1)["state"], "uninstalled")
        self.assertEqual(self.state.install(id2)["state"], "installed")

    def test_read_only_hint_grants_only_at_approval(self):
        cand = self._check(_candidate(), run=_fake_run())
        id = self.installer._server_id(cand["name"])
        self.installer.propose(cand, "user")
        self.assertEqual(config.load(self.home)["grants"], {})

        self._install(cand)
        self.assertEqual(config.load(self.home)["grants"].get(f"mcp__{id}__echo"), "allow")


    def test_missing_secret_is_still_referenced_in_both_configs(self):
        """B6: adding the secret to the Keychain later must be all it takes."""
        env = [{"name": "TOKEN", "secret": True, "required": True}]
        with mock.patch("pathfinder.installer.keychain.secret", return_value=None):
            cand = self._check(_candidate(env=env), run=_fake_run())
        self.assertEqual(cand["install"]["needs"], ["TOKEN"])
        self.attach.load_result = "secret missing: TOKEN"
        id = cand["install"]["id"]
        self._install(cand)
        self.assertEqual(config.load(self.home)["mcp_servers"][id]["env"], {"TOKEN": "keychain:TOKEN"})
        claude = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(claude["mcpServers"][id]["env"], {"TOKEN": "${TOKEN}"})
        self.assertEqual(self.state.install(id)["state"], "installed")

    def test_second_proposal_for_a_taken_id_leaves_the_live_install_alone(self):
        """N1: the clash is refused before any step is recorded; nothing is unloaded."""
        cand = self._check(_candidate(), run=_fake_run())
        again = self._check(_candidate(), run=_fake_run())  # both checked before either install
        id = cand["install"]["id"]
        self.assertEqual(again["install"]["id"], id)  # decided at check: the same id twice
        self._install(cand)
        self.assertIn(f"mcp__{id}__echo", tools_mod.TOOLS)
        self.attach.servers._sessions[id] = object()
        self._install(again)
        self.assertIn(f"mcp__{id}__echo", tools_mod.TOOLS)
        self.assertIn(id, self.attach.servers._sessions)
        self.assertEqual(self.state.install(id)["state"], "installed")
        self.assertIn(id, config.load(self.home)["mcp_servers"])
        failed = [e for e in self.ledger.read() if e["type"] == "install_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["id"], "")
        self.assertIn("already taken", failed[0]["reason"])

    def test_reproposing_an_installed_name_fails_at_check(self):
        """Review round 3: the same name again got `-2` at check and a second copy at approve."""
        cand = self._check(_candidate(), run=_fake_run())
        self._install(cand)
        again = self._check(_candidate(version="1.0.1"), run=_fake_run())
        self.assertEqual(again["outcome"], "install_failed")
        self.assertEqual(again["verification"]["failed_step"], "installed")
        self.assertIn(f"already installed as {cand['install']['id']}", again["verification"]["reason"])
        self.assertEqual(again["install"], "not_checked")  # nothing decided, nothing suffixed


if __name__ == "__main__":
    unittest.main()
