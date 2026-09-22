import sys
import unittest
from unittest import mock

from pathfinder.broker import Broker, Gate
from pathfinder.senses import calendar as calendar_sense
from pathfinder.senses import reminders as reminders_sense


def _no_pyobjc():
    """Guarantee `import EventKit`/`Foundation` fails regardless of what's actually installed -
    the trick: a module set to None in sys.modules makes `import` raise ImportError."""
    return mock.patch.dict(sys.modules, {"EventKit": None, "Foundation": None})


class CalendarOffTest(unittest.TestCase):
    def setUp(self):
        calendar_sense._off_logged = False
        reminders_sense._off_logged = False

    def test_calendar_off_without_pyobjc(self):
        with _no_pyobjc():
            with self.assertRaises(RuntimeError) as cm:
                calendar_sense.poll({}, 1000.0)
            self.assertIn("install pyobjc-framework-EventKit", str(cm.exception))
            # logged once (SenseLoop.on_error would have recorded it) - then quiet forever
            self.assertEqual(calendar_sense.poll({}, 1000.0), [])
            self.assertEqual(calendar_sense.poll({}, 2000.0), [])

    def test_calendar_tools_return_error_string_when_off(self):
        with _no_pyobjc():
            with self.assertRaises(RuntimeError) as cm:
                calendar_sense._list_events(**{"from": "2026-09-22 09:00", "to": "2026-09-22 10:00"})
            self.assertIn("install pyobjc-framework-EventKit", str(cm.exception))
            with self.assertRaises(RuntimeError):
                calendar_sense._add_event("x", "2026-09-22 09:00", "2026-09-22 10:00")

    def test_reminders_off_without_pyobjc(self):
        with _no_pyobjc():
            with self.assertRaises(RuntimeError) as cm:
                reminders_sense.poll({}, 1000.0)
            self.assertIn("install pyobjc-framework-EventKit", str(cm.exception))
            self.assertEqual(reminders_sense.poll({}, 1000.0), [])


class AddEventGateTest(unittest.TestCase):
    def test_add_event_gate_shows_canonical_datetime(self):
        """The gate fires before the handler ever runs (no EventKit needed here): normalize
        already turned the model's `T`-separated time into the canonical local string."""
        b = Broker(dict(calendar_sense.TOOLS), "user", {}, control=None, emit=lambda k, p: None)
        with self.assertRaises(Gate) as cm:
            b.call("c1", "add_event", {"title": "Standup", "start": "2026-09-22T09:00", "end": "2026-09-22T09:30"})
        self.assertEqual(cm.exception.arguments["start"], "2026-09-22 09:00")
        self.assertEqual(cm.exception.arguments["end"], "2026-09-22 09:30")

    def test_add_event_refuses_an_offset(self):
        b = Broker(dict(calendar_sense.TOOLS), "user", {}, control=None, emit=lambda k, p: None)
        result = b.call("c1", "add_event", {"title": "x", "start": "2026-09-22 09:00+09:00", "end": "2026-09-22 10:00"})
        self.assertFalse(result["ok"])
        self.assertIn("offset", result["error"])


class AddEventEndBeforeStartTest(unittest.TestCase):
    def test_end_before_start_refused_before_any_eventkit_call(self):
        """Should-fix 6: caught before `_eventkit` is ever touched - no PyObjC needed here."""
        with self.assertRaises(ValueError):
            calendar_sense._add_event("x", "2026-09-22 10:00", "2026-09-22 09:00")


class ListEventsWindowCapTest(unittest.TestCase):
    def test_window_over_31_days_refused(self):
        """Should-fix 7: an unbounded `list_events` window is an unbounded EventKit scan."""
        with self.assertRaises(ValueError):
            calendar_sense._list_events(**{"from": "2026-01-01 00:00", "to": "2026-03-01 00:00"})

    def test_window_at_31_days_is_fine(self):
        with _no_pyobjc():  # still refused, but for being off, not for the window size
            with self.assertRaises(RuntimeError) as cm:
                calendar_sense._list_events(**{"from": "2026-01-01 00:00", "to": "2026-02-01 00:00"})
            self.assertIn("install pyobjc-framework-EventKit", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
