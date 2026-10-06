import asyncio
import sys
import time
import types
import unittest

from pathfinder import signals
from pathfinder.signals import Sense, Signal


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


HOSTILE = "a‮b​c\x1b[31md\x1b[0m\ne"  # bidi mark, zero-width, ANSI colour, newline -> "abcd e"


class EnvelopeFilterTest(unittest.TestCase):
    """U21: a signal's key, summary and fields carry someone else's text to the model."""

    def test_envelope_strips_bidi_zero_width_and_ansi_from_fields(self):
        sig = Signal(sense="mail", kind="new_mail", key="k" + HOSTILE, summary="s" + HOSTILE,
                     fields={"subject": HOSTILE, HOSTILE: [HOSTILE, {"name": HOSTILE}], "n": 3, "x": None},
                     trust="third_party")
        self.assertEqual(signals.render_block(sig),
                         'kind: new_mail\ntrust: third_party\nkey: kabcd e\nsummary: sabcd e\nfields: '
                         '{"subject": "abcd e", "abcd e": ["abcd e", {"name": "abcd e"}], "n": 3, "x": null}')

    def test_envelope_clips_long_fields_and_summary(self):
        sig = Signal(sense="mail", kind="new_mail", key="k" * 500, summary="s" * 5000,
                     fields={"body": "b" * 5000, "many": ["x" * 400] * 20}, trust="third_party")
        kind, trust, key, summary, fields = signals.render_block(sig).split("\n")
        self.assertEqual(key, "key: " + "k" * 200)
        self.assertEqual(summary, "summary: " + "s" * 2200)
        self.assertTrue(fields.startswith('fields: {"body": "' + "b" * 300 + '", "many": ["' + "x" * 300 + '"'))
        self.assertEqual(len(fields), len("fields: ") + 2000)

    def test_single_line_summary_block_unchanged(self):
        sig = Signal(sense="calendar", kind="upcoming_event", key="event:E1:1790000000.0",
                     summary="Upcoming: 2026-09-30 14:00 Café", trust="third_party",
                     fields={"title": "Café", "end": None, "all_day": False, "minutes_until": 30})
        self.assertEqual(signals.render_block(sig),
                         "kind: upcoming_event\ntrust: third_party\nkey: event:E1:1790000000.0\n"
                         "summary: Upcoming: 2026-09-30 14:00 Café\n"
                         'fields: {"title": "Café", "end": null, "all_day": false, "minutes_until": 30}')

    def test_third_party_senses_pass_the_filter(self):
        from pathfinder.senses import claude_code
        samples = [
            Signal(sense="mail", kind="new_mail", key="<m1@example.com>", trust="third_party",
                   summary="Mail from x@example.com: " + HOSTILE,
                   fields={"from": "x@example.com", "sender_domain": "example.com", "subject": HOSTILE,
                           "received": "Wed, 30 Sep 2026 09:00:00 +0000"}),
            Signal(sense="calendar", kind="upcoming_event", key="event:E1:1790000000.0", trust="third_party",
                   summary="Upcoming: 2026-09-30 14:00 " + HOSTILE,
                   fields={"title": HOSTILE, "start": "2026-09-30 14:00", "end": None, "all_day": False,
                           "notes": HOSTILE, "minutes_until": 30}),
            Signal(sense="catalog", kind="tool_candidate", key="catalog:io.x/y@1.0.0", trust="third_party",
                   summary="New tool for p: io.x/y - " + HOSTILE,
                   fields={"candidate": {"name": "io.x/y", "description": HOSTILE,
                                         "package": {"identifier": HOSTILE}}, "matched_tags": ["x"]}),
            claude_code.build({"event": "Notification", "cwd": "/work/p", "message": HOSTILE}, now=0),
        ]
        for sig in samples:
            with self.subTest(sense=sig.sense):
                out = signals.render_block(sig)
                self.assertEqual(len(out.split("\n")), 5)
                for bad in ("‮", "​", "\x1b", "[31m", "[0m"):
                    self.assertNotIn(bad, out)
                self.assertIn("abcd e", out.split("\n")[4])


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

    def test_load_binds_home_onto_the_module(self):
        """Should-fix 8: a sense's tool handlers get no `options` argument, so `load` binds the
        same home it injects as `_home` onto the module itself as `HOME` - one source, not two."""
        mod = self._register("_fake_home", KINDS={"foo": ("always", "-", "silence", "")})
        loaded = signals.load({"senses": {"_fake_home": {}}}, home="/tmp/some-home")
        self.assertEqual(mod.HOME, "/tmp/some-home")
        self.assertEqual(loaded[0].options["_home"], "/tmp/some-home")


class SenseLoopStopTest(unittest.TestCase):
    def test_stop_awaits_an_inflight_poll(self):
        """Note 9: stop() must not return while a poll is still running in its own thread - a
        late arrival could touch `state` after the daemon has closed it."""
        finished = []

        def slow_poll(options, now):
            time.sleep(0.25)
            finished.append(True)
            return []

        sense = Sense(name="slow", trust="self", kinds={}, conditions={}, options={},
                      module=None, poll=slow_poll, cadence_s=0.01)

        async def scenario():
            loop = signals.SenseLoop([sense], push=lambda sig: None)
            loop.start()
            await asyncio.sleep(0.05)  # let _run schedule the poll
            t0 = time.time()
            await loop.stop()
            return time.time() - t0, list(finished)

        elapsed, done = asyncio.run(scenario())
        self.assertGreater(elapsed, 0.15)  # stop() actually waited for the slow poll
        self.assertEqual(done, [True])


if __name__ == "__main__":
    unittest.main()
