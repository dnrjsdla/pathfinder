import json
import logging
import tempfile
import unittest
from pathlib import Path

from pathfinder import config
from pathfinder.mcp import protocol as pr
from pathfinder.mcp import transport as tr
from pathfinder.mcp.servers import Servers, _resolve, load


class FakeTransport(tr.Transport):
    """No process, no socket - `send` answers `tools/list` from a canned list."""

    def __init__(self, tool_defs):
        self.tool_defs = list(tool_defs)
        self.closed = False

    def send(self, message, *, extra_headers=None, timeout=30):
        if message["method"] == "tools/list":
            return {"tools": list(self.tool_defs)}
        return {}

    def close(self):
        self.closed = True


def _lookup(secrets):
    return lambda service, account: secrets.get((service, account))


class LoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_reads_stdio_and_http_servers(self):
        cfg = config.set(self.home, "mcp_servers", {
            "local": {"transport": "stdio", "command": ["npx", "-y", "server"]},
            "remote": {"transport": "http", "url": "https://example.invalid/mcp", "timeout": 5},
        })
        configs = {c.server_id: c for c in load(cfg)}
        self.assertEqual(configs["local"].transport_kind, "stdio")
        self.assertEqual(configs["local"].command, ("npx", "-y", "server"))
        self.assertEqual(configs["remote"].endpoint, "https://example.invalid/mcp")
        self.assertEqual(configs["remote"].timeout, 5)
        self.assertEqual(configs["local"].disabled, "")

    def test_secret_from_keychain_never_in_config(self):
        cfg = config.set(self.home, "mcp_servers", {
            "gh": {"transport": "stdio", "command": ["npx", "gh-server"], "env": {"TOKEN": "keychain:bot"}},
        })
        on_disk = json.loads((self.home / "config.json").read_text())
        self.assertEqual(on_disk["mcp_servers"]["gh"]["env"], {"TOKEN": "keychain:bot"})

        [found] = load(cfg, lookup=_lookup({("pathfinder-mcp-gh", "bot"): "s3cr3t"}))
        self.assertEqual(found.env, {"TOKEN": "s3cr3t"})
        self.assertEqual(found.disabled, "")
        # config.json on disk still says the reference, never the resolved value
        on_disk_again = json.loads((self.home / "config.json").read_text())
        self.assertEqual(on_disk_again["mcp_servers"]["gh"]["env"]["TOKEN"], "keychain:bot")

        [missing] = load(cfg, lookup=_lookup({}))
        self.assertTrue(missing.disabled.startswith("secret missing"))
        self.assertIn("TOKEN", missing.disabled)

    def test_entry_grammar_table(self):
        """U34: `keychain:` required, `keychain?:` optional, `=<default>` used only when the item is
        missing; the account ends at the first `=`; anything else is a literal."""
        found = _lookup({("pathfinder-mcp-s", "A"): "sa"})
        for value, resolved, missing, unset in [
            ("keychain:A", {"K": "sa"}, [], []),
            ("keychain:B", {}, ["K"], []),
            ("keychain:A=d", {"K": "sa"}, [], []),           # the item wins over the default
            ("keychain:B=d", {"K": "d"}, [], []),
            ("keychain:B=", {"K": ""}, [], []),              # an empty default is a default
            ("keychain:B=x=y:z", {"K": "x=y:z"}, [], []),
            ("keychain?:A", {"K": "sa"}, [], []),
            ("keychain?:B", {}, [], ["K"]),
            ("keychain?:B=d", {"K": "d"}, [], []),
            ("https://x.invalid/a=b", {"K": "https://x.invalid/a=b"}, [], []),
            ("keychain", {"K": "keychain"}, [], []),
        ]:
            with self.subTest(value=value):
                self.assertEqual(_resolve({"K": value}, "s", found), (resolved, missing, unset))

    def test_old_entry_grammar_is_required_without_default(self):
        """An entry written before U34 (`keychain:NAME` only) still means required, no default."""
        cfg = {"mcp_servers": {"gh": {"transport": "stdio", "command": ["x"], "env": {"T": "keychain:bot"}}}}
        self.assertEqual(load(cfg, lookup=_lookup({}))[0].disabled, "secret missing: T")
        [found] = load(cfg, lookup=_lookup({("pathfinder-mcp-gh", "bot"): "s"}))
        self.assertEqual((found.disabled, found.env), ("", {"T": "s"}))

    def test_missing_optional_secret_starts_without_it(self):
        cfg = {"mcp_servers": {"gh": {"transport": "stdio", "command": ["x"],
                                      "env": {"TOKEN": "keychain?:TOKEN", "LEVEL": "info"}}}}
        [found] = load(cfg, lookup=_lookup({}))
        self.assertEqual(found.disabled, "")
        self.assertEqual(found.env, {"LEVEL": "info"})  # not set at all - not an empty string


class ServersTest(unittest.TestCase):
    def test_load_one_adds_to_a_running_set(self):
        servers = Servers([])
        self.assertEqual(servers.entries(), [])

        config_row = _stdio_config("gh")
        servers.load_one(config_row)
        session = servers._sessions["gh"]
        session._transport = FakeTransport([{"name": "search_issues", "description": "d",
                                             "inputSchema": {"type": "object"}}])

        entries = servers.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].qualified_name, "mcp__gh__search_issues")
        self.assertEqual(entries[0].server_id, "gh")

        result = servers.call("mcp__gh__search_issues", {})
        self.assertIsInstance(result, dict)

    def test_disabled_server_stays_listed_not_dropped(self):
        servers = Servers([_stdio_config("gh", disabled="secret missing: TOKEN")])
        self.assertEqual(servers.entries(), [])
        self.assertEqual(servers.disabled(), [("gh", "secret missing: TOKEN")])

    def test_a_server_that_fails_to_list_is_logged_and_skipped(self):
        servers = Servers([_stdio_config("ok"), _stdio_config("broken")])
        servers._sessions["ok"]._transport = FakeTransport([{"name": "t", "description": "d"}])

        class Boom(tr.Transport):
            def send(self, message, *, extra_headers=None, timeout=30):
                raise pr.McpTransportError("dead")

        servers._sessions["broken"]._transport = Boom()
        with self.assertLogs("pathfinder.mcp", level="WARNING"):
            entries = servers.entries()
        self.assertEqual([e.server_id for e in entries], ["ok"])

    def test_call_unknown_server_raises(self):
        servers = Servers([])
        with self.assertRaises(pr.McpError):
            servers.call("mcp__nope__thing", {})

    def test_close_closes_every_session(self):
        servers = Servers([_stdio_config("gh")])
        fake = FakeTransport([])
        servers._sessions["gh"]._transport = fake
        servers.close()
        self.assertTrue(fake.closed)

    def test_load_one_closes_the_replaced_session(self):
        servers = Servers([_stdio_config("gh")])
        old_transport = FakeTransport([])
        servers._sessions["gh"]._transport = old_transport

        servers.load_one(_stdio_config("gh"))

        self.assertTrue(old_transport.closed)
        self.assertIsNot(servers._sessions["gh"]._transport, old_transport)

    def test_load_one_disabling_drops_the_session(self):
        servers = Servers([_stdio_config("gh")])
        fake = FakeTransport([])
        servers._sessions["gh"]._transport = fake

        servers.load_one(_stdio_config("gh", disabled="secret missing: TOKEN"))

        self.assertTrue(fake.closed)
        self.assertNotIn("gh", servers._sessions)
        self.assertEqual(servers.disabled(), [("gh", "secret missing: TOKEN")])

    def test_load_one_reenabling_clears_disabled(self):
        servers = Servers([_stdio_config("gh", disabled="secret missing: TOKEN")])

        servers.load_one(_stdio_config("gh"))

        self.assertEqual(servers.disabled(), [])
        self.assertIn("gh", servers._sessions)

    def test_disabled_secret_missing_logs_once_per_instance(self):
        servers = Servers([])
        cfg = _stdio_config("gh", disabled="secret missing: TOKEN")
        with self.assertLogs("pathfinder.mcp", level="WARNING") as logs:
            servers.load_one(cfg)
            servers.load_one(cfg)
        self.assertEqual(len(logs.output), 1)

    def test_unload_closes_and_drops(self):
        servers = Servers([_stdio_config("gh")])
        fake = FakeTransport([])
        servers._sessions["gh"]._transport = fake

        servers.unload("gh")

        self.assertTrue(fake.closed)
        self.assertNotIn("gh", servers._sessions)
        self.assertEqual(servers.disabled(), [])


def _stdio_config(server_id, disabled=""):
    from pathfinder.mcp.servers import ServerConfig
    return ServerConfig(server_id=server_id, transport_kind="stdio", command=("true",), disabled=disabled)


if __name__ == "__main__":
    unittest.main()
