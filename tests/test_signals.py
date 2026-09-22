import sys
import types
import unittest

from pathfinder import signals
from pathfinder.signals import Signal


class FakeState:
    """Just enough of state.State for seen()/today() - no SQLite needed here."""

    def __init__(self, seen_rows=(), today=None):
        self._rows = list(seen_rows)  # (key, ts)
        self._today = today

    def seen(self, key, since):
        return any(k == key and ts >= since for k, ts in self._rows)

    def today(self, now=None):
        return self._today


class Today:
    def __init__(self, last_signal_ts=None, burst=0):
        self.last_signal_ts = last_signal_ts
        self.burst = burst


class RenderTest(unittest.TestCase):
    def test_render_block_matches_bench_shape(self):
        sig = Signal(sense="schedule", kind="schedule_due", key="k1", summary="s1",
                    fields={"a": 1}, trust="user")
        self.assertEqual(signals.render_block(sig),
                         'kind: schedule_due\ntrust: user\nkey: k1\nsummary: s1\nfields: {"a": 1}')

    def test_render_envelope_prefixes_the_constant(self):
        sig = Signal(sense="shell", kind="command_failed", key="k1", summary="s", fields={}, trust="self")
        out = signals.render_envelope([sig])
        self.assertTrue(out.startswith(signals.SIGNAL_ENVELOPE))
        self.assertIn("kind: command_failed", out)


class DedupeTest(unittest.TestCase):
    def test_signals_dedupe_24h(self):
        now = 1_000_000.0
        state = FakeState(seen_rows=[("k1", now - 100)])
        self.assertTrue(signals.seen_recently(state, "k1", now))
        self.assertFalse(signals.seen_recently(state, "k2", now))
        self.assertFalse(signals.seen_recently(state, "k1", now + 90000))  # more than 24h later


class BurstLimitTest(unittest.TestCase):
    class Rules:
        min_interval_s = 30
        burst_limit = 5

    def test_signals_burst_limit(self):
        rules = self.Rules()
        blocked = FakeState(today=Today(last_signal_ts=None, burst=5))
        self.assertEqual(signals.hold_for(blocked, rules, now=1000.0), 60.0)
        clear = FakeState(today=Today(last_signal_ts=None, burst=2))
        self.assertEqual(signals.hold_for(clear, rules, now=1000.0), 0.0)

    def test_signals_min_interval(self):
        rules = self.Rules()
        recent = FakeState(today=Today(last_signal_ts=990.0, burst=0))
        self.assertAlmostEqual(signals.hold_for(recent, rules, now=1000.0), 20.0)


class SenseValidationTest(unittest.TestCase):
    def _register(self, name, **attrs):
        mod = types.ModuleType(f"pathfinder.senses.{name}")
        mod.OPTIONS = attrs.get("OPTIONS", {})
        mod.TRUST = attrs.get("TRUST", "self")
        mod.CONDITIONS = attrs.get("CONDITIONS", {})
        mod.KINDS = attrs["KINDS"]
        sys.modules[mod.__name__] = mod
        self.addCleanup(sys.modules.pop, mod.__name__, None)
        return mod

    def test_sense_without_silence_row_refused(self):
        self._register("_fake_no_silence", KINDS={"foo": ("always", "-", "speak", "")})
        with self.assertRaises(ValueError):
            signals.load({"senses": {"_fake_no_silence": {}}})

    def test_sense_with_silence_row_loads(self):
        self._register("_fake_ok", KINDS={"foo": ("always", "-", "silence", "")})
        loaded = signals.load({"senses": {"_fake_ok": {}}})
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "_fake_ok")

    def test_sense_bad_trust_refused(self):
        self._register("_fake_bad_trust", TRUST="nope", KINDS={"foo": ("always", "-", "silence", "")})
        with self.assertRaises(ValueError):
            signals.load({"senses": {"_fake_bad_trust": {}}})


if __name__ == "__main__":
    unittest.main()
