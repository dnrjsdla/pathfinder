import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import config


class FakeLedger:
    """Records append() calls without touching the real ledger - config tests only
    need to see what config.set() reports, not how the ledger persists it."""

    def __init__(self):
        self.events = []

    def append(self, type, **data):
        event = {"type": type, **data}
        self.events.append(event)
        return event


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_old_config_with_the_retired_model_keys_loads(self):
        (self.home / "config.json").write_text(json.dumps({"model": "ollama:old", "worker_model": "", "max_steps": 3}))
        cfg = config.load(self.home)
        self.assertEqual((cfg["max_steps"], "model" in cfg, "worker_model" in cfg), (3, False, False))
        config.set(self.home, "max_steps", 4)  # the next write drops them from the file
        self.assertEqual(json.loads((self.home / "config.json").read_text()).keys() & {"model", "worker_model"}, set())

    def test_unknown_key_raises_on_load(self):
        (self.home / "config.json").write_text(json.dumps({"not_a_real_key": 1}))
        with self.assertRaises(config.ConfigError):
            config.load(self.home)

    def test_unknown_key_raises_on_set(self):
        with self.assertRaises(config.ConfigError):
            config.set(self.home, "not_a_real_key", "value")

    def test_set_is_atomic(self):
        config.set(self.home, "embed_model", "ollama:some-model")
        cfg = config.load(self.home)
        self.assertEqual(cfg["embed_model"], "ollama:some-model")
        # atomic write leaves no leftover temp file behind
        leftovers = list(self.home.glob(".config-*.tmp"))
        self.assertEqual(leftovers, [])

    def test_set_appends_config_set_event_without_value(self):
        ledger = FakeLedger()
        config.set(self.home, "embed_model", "ollama:secret-model-name", ledger=ledger)
        self.assertEqual(len(ledger.events), 1)
        event = ledger.events[0]
        self.assertEqual(event["type"], "config_set")
        self.assertEqual(event["key"], "embed_model")
        self.assertNotIn("value", event)
        self.assertNotIn("ollama:secret-model-name", json.dumps(event))

    def test_home_env_var(self):
        old = os.environ.get("PATHFINDER_HOME")
        os.environ["PATHFINDER_HOME"] = str(self.home / "nested")
        try:
            p = config.home()
            self.assertEqual(p, self.home / "nested")
            self.assertTrue(p.is_dir())
            self.assertEqual(p.stat().st_mode & 0o777, 0o700)
        finally:
            if old is None:
                del os.environ["PATHFINDER_HOME"]
            else:
                os.environ["PATHFINDER_HOME"] = old

    def test_values_are_type_checked_at_both_doors(self):
        for key, bad in (("max_steps", 0), ("max_steps", "abc"), ("max_steps", True), ("embed_model", 5), ("providers", [])):
            with self.assertRaises(config.ConfigError, msg=f"{key}={bad!r}"):
                config.set(self.home, key, bad)
        # a hand-edited file goes through the same check as `config set`
        (self.home / "config.json").write_text(json.dumps({"max_steps": 0}))
        with self.assertRaises(config.ConfigError):
            config.load(self.home)

    def test_sense_options_checked_by_config(self):
        with self.assertRaises(config.ConfigError):
            config.set(self.home, "senses", {"no_such_sense": {}})
        with self.assertRaises(config.ConfigError):
            config.set(self.home, "senses", {"schedule": {"no_such_option": 1}})
        cfg = config.set(self.home, "senses", {"schedule": {}})
        self.assertEqual(cfg["senses"], {"schedule": {}})

    def test_schedules_validated_shape_and_canonical_time(self):
        good = [{"id": "a", "at": "2026-09-22 09:00", "repeat": "none", "text": "x"}]
        cfg = config.set(self.home, "schedules", good)
        self.assertEqual(cfg["schedules"], good)
        with self.assertRaises(config.ConfigError):
            config.set(self.home, "schedules", [{"id": "a", "at": "2026-09-22 09:00", "text": "x"}])  # missing repeat
        with self.assertRaises(config.ConfigError):
            config.set(self.home, "schedules",
                      [{"id": "a", "at": "2026-09-22 09:00", "repeat": "monthly", "text": "x"}])
        with self.assertRaises(config.ConfigError):
            config.set(self.home, "schedules",
                      [{"id": "a", "at": "2026-09-22T09:00Z", "repeat": "none", "text": "x"}])

    def test_config_refuses_bad_mcp_servers(self):
        good_stdio = {"transport": "stdio", "command": ["npx", "server"]}
        cases = (
            {"bad_id": good_stdio},                                             # underscore
            {"srv": {"transport": "carrier-pigeon", "command": ["x"]}},          # unknown transport
            {"srv": {"transport": "stdio"}},                                    # stdio without command
            {"srv": {"transport": "stdio", "command": ["x"], "timeout": 0}},     # timeout 0
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(config.ConfigError):
                    config.set(self.home, "mcp_servers", value)

    def test_config_accepts_a_stdio_and_an_http_server(self):
        value = {
            "local": {"transport": "stdio", "command": ["npx", "-y", "server"], "env": {"TOKEN": "keychain:bot"}},
            "remote": {"transport": "http", "url": "https://example.invalid/mcp", "timeout": 10},
        }
        cfg = config.set(self.home, "mcp_servers", value)
        self.assertEqual(cfg["mcp_servers"], value)

    def test_models_ship_empty_and_refs_are_checked(self):
        self.assertEqual(config.load(self.home)["models"], {})
        cfg = config.set(self.home, "models", {"root": ["ollama:fake-a", "ollama:fake:tag"], "summarize": ["ollama:b"]})
        self.assertEqual(cfg["models"]["root"], ["ollama:fake-a", "ollama:fake:tag"])
        for bad in ({"boss": ["ollama:a"]}, {"root": []}, {"root": "ollama:a"}, {"root": ["ollama:"]},
                    {"root": [":a"]}, {"root": ["a"]}, {"root": [5]}, {"root": ["nowhere:a"]}):
            with self.subTest(value=bad):
                with self.assertRaises(config.ConfigError):
                    config.set(self.home, "models", bad)
        # a providers change that orphans a ref is refused too, and so is a hand-edited file
        with self.assertRaises(config.ConfigError):
            config.set(self.home, "providers", {"other": {"base_url": "http://127.0.0.1:1/v1"}})
        (self.home / "config.json").write_text(json.dumps({"models": {"root": ["nowhere:a"]}}))
        with self.assertRaises(config.ConfigError):
            config.load(self.home)

    def test_provider_shape_and_effort_checked(self):
        good = {"cloud": {"api": "anthropic", "base_url": "https://cloud.example", "effort": "xhigh"}}
        self.assertEqual(config.set(self.home, "providers", good)["providers"], good)
        for bad in ({"cloud": {"api": "anthropic", "base_url": "https://cloud.example", "effort": "huge"}},
                    {"local": {"base_url": "http://127.0.0.1:1/v1", "effort": "high"}},
                    {"local": "http://127.0.0.1:1/v1"},
                    {"local": {"api_key_env": ""}}, {"local": {"base_url": ""}},
                    {"local": {"base_url": "http://127.0.0.1:1/v1", "api": "gemini"}}):
            with self.subTest(value=bad):
                with self.assertRaises(config.ConfigError):
                    config.set(self.home, "providers", bad)

    def test_home_tightens_an_existing_directory(self):
        loose = self.home / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        old = os.environ.get("PATHFINDER_HOME")
        os.environ["PATHFINDER_HOME"] = str(loose)
        try:
            self.assertEqual(config.home().stat().st_mode & 0o777, 0o700)
        finally:
            if old is None:
                del os.environ["PATHFINDER_HOME"]
            else:
                os.environ["PATHFINDER_HOME"] = old

    def test_make_private_drops_group_other_keeps_owner_x_skips_symlinks_and_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, outside = Path(tmp) / "home", Path(tmp) / "outside"
            for d in (home / "events", home / "workspace", outside):
                d.mkdir(parents=True)
            ledger, script, mine, theirs = (home / "events" / "0.jsonl", home / "run.sh",
                                            home / "workspace" / "draft.txt", outside / "f")
            for f in (ledger, script, mine, theirs):
                f.write_text("x")
            for p, mode in ((home / "events", 0o755), (ledger, 0o644), (script, 0o755),
                            (home / "workspace", 0o755), (mine, 0o644), (outside, 0o755), (theirs, 0o644)):
                os.chmod(p, mode)
            (home / "link-file").symlink_to(theirs)
            (home / "link-dir").symlink_to(outside)
            config.make_private(home)

            def mode(p):
                return p.stat().st_mode & 0o777
            self.assertEqual([mode(home / "events"), mode(ledger), mode(script)], [0o700, 0o600, 0o700])
            self.assertEqual([mode(home / "workspace"), mode(mine)], [0o755, 0o644])
            self.assertEqual([mode(outside), mode(theirs)], [0o755, 0o644])  # neither link followed


    def test_make_private_logs_an_entry_it_cannot_change_and_goes_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "events").mkdir()
            names = ("stuck", "a.txt", "events/0.jsonl", "z.txt")
            for name in names:
                (home / name).write_text("x")
                os.chmod(home / name, 0o644)
            real = os.chmod

            def chmod(path, mode):
                if str(path).endswith("stuck"):
                    raise PermissionError(1, "Operation not permitted", str(path))
                real(path, mode)
            with mock.patch("pathfinder.config.os.chmod", chmod), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                config.make_private(home)
            self.assertIn("could not make", err.getvalue())
            self.assertIn("stuck", err.getvalue())
            self.assertEqual([(home / n).stat().st_mode & 0o777 for n in names], [0o644, 0o600, 0o600, 0o600])


if __name__ == "__main__":
    unittest.main()
