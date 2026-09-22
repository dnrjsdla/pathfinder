import json
import os
import tempfile
import unittest
from pathlib import Path

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

    def test_default_model_is_empty(self):
        cfg = config.load(self.home)
        self.assertEqual(cfg["model"], "")

    def test_unknown_key_raises_on_load(self):
        (self.home / "config.json").write_text(json.dumps({"not_a_real_key": 1}))
        with self.assertRaises(config.ConfigError):
            config.load(self.home)

    def test_unknown_key_raises_on_set(self):
        with self.assertRaises(config.ConfigError):
            config.set(self.home, "not_a_real_key", "value")

    def test_set_is_atomic(self):
        config.set(self.home, "model", "ollama:some-model")
        cfg = config.load(self.home)
        self.assertEqual(cfg["model"], "ollama:some-model")
        # atomic write leaves no leftover temp file behind
        leftovers = list(self.home.glob(".config-*.tmp"))
        self.assertEqual(leftovers, [])

    def test_set_appends_config_set_event_without_value(self):
        ledger = FakeLedger()
        config.set(self.home, "model", "ollama:secret-model-name", ledger=ledger)
        self.assertEqual(len(ledger.events), 1)
        event = ledger.events[0]
        self.assertEqual(event["type"], "config_set")
        self.assertEqual(event["key"], "model")
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
        for key, bad in (("max_steps", 0), ("max_steps", "abc"), ("max_steps", True), ("model", 5), ("providers", [])):
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


if __name__ == "__main__":
    unittest.main()
