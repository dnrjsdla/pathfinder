import os
import tempfile
import unittest
from pathlib import Path

from pathfinder import clock, config, rules, signals
from pathfinder.senses import schedule, shell
from pathfinder.state import Today


class ScheduleFiresTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_schedule_fires_once_and_repeats(self):
        config.set(self.home, "schedules", [
            {"id": "a", "at": "2026-09-22 09:00", "repeat": "none", "text": "one-shot"},
            {"id": "b", "at": "2026-09-22 09:00", "repeat": "daily", "text": "daily"},
        ])
        due_ts, _ = clock.parse_local("2026-09-22 09:00")

        fired = schedule.poll({"_home": self.home}, due_ts + 1)
        self.assertEqual({s.kind for s in fired}, {"schedule_due"})
        self.assertEqual({s.fields["id"] for s in fired}, {"a", "b"})

        rows = {r["id"]: r for r in config.load(self.home)["schedules"]}
        self.assertNotIn("a", rows)             # a one-shot row is dropped once fired
        self.assertGreater(rows["b"]["at"], "2026-09-22 09:00")  # a repeat's `at` advances

        # right away again: "b" no longer due (its `at` moved into the future), nothing fires
        self.assertEqual(schedule.poll({"_home": self.home}, due_ts + 1), [])

    def test_schedule_not_yet_due_is_untouched(self):
        config.set(self.home, "schedules", [
            {"id": "a", "at": "2026-09-22 09:00", "repeat": "none", "text": "later"},
        ])
        due_ts, _ = clock.parse_local("2026-09-22 09:00")
        self.assertEqual(schedule.poll({"_home": self.home}, due_ts - 60), [])
        self.assertEqual(len(config.load(self.home)["schedules"]), 1)

    def test_schedule_repeat_only_fires_once_after_being_down(self):
        """Blocker 3: a daily schedule missed for a week must fire ONE signal, not one per
        missed day, and `at` must land in the future in a single poll."""
        config.set(self.home, "schedules", [
            {"id": "b", "at": "2026-09-15 09:00", "repeat": "daily", "text": "stand-up"},
        ])
        now, _ = clock.parse_local("2026-09-22 09:30")  # 7 days and 30 minutes later
        fired = schedule.poll({"_home": self.home}, now)
        self.assertEqual(len(fired), 1)
        row = config.load(self.home)["schedules"][0]
        next_ts, _ = clock.parse_local(row["at"])
        self.assertGreater(next_ts, now)
        # polling again right away must not fire again
        self.assertEqual(schedule.poll({"_home": self.home}, now), [])

    def test_remove_schedule_unknown_id_fails(self):
        """Live 2026-09-26: a Reminders id passed here came back "removed" and the model said so."""
        schedule.HOME = self.home
        self.addCleanup(setattr, schedule, "HOME", None)
        row = schedule._add_schedule("2026-09-30 09:00", "planted", "none")
        with self.assertRaises(LookupError):
            schedule._remove_schedule("237225CD-0650-4BF2-8F3F-A80455526A61")
        self.assertEqual(len(config.load(self.home)["schedules"]), 1)
        self.assertEqual(schedule._remove_schedule(row["id"]), {"removed": row["id"]})
        self.assertEqual(config.load(self.home)["schedules"], [])

    def test_schedule_tools_use_the_bound_home_not_config_home(self):
        """Should-fix 8: the sense's tools must use the ONE home `signals.load` bound, never a
        second answer from `config.home()` (which reads PATHFINDER_HOME) - they can legitimately
        differ, as in this test and in the review's own repro."""
        old = os.environ.get("PATHFINDER_HOME")
        other_home = Path(tempfile.mkdtemp())
        os.environ["PATHFINDER_HOME"] = str(other_home)
        try:
            signals.load({"senses": {"schedule": {}}}, self.home)  # binds schedule.HOME = self.home
            self.assertEqual(schedule.HOME, self.home)
            schedule._add_schedule("2026-09-30 09:00", "planted", "none")
            self.assertEqual(len(config.load(self.home).get("schedules", [])), 1)
            self.assertEqual(config.load(other_home).get("schedules", []), [])
        finally:
            if old is None:
                os.environ.pop("PATHFINDER_HOME", None)
            else:
                os.environ["PATHFINDER_HOME"] = old
            schedule.HOME = None


class ShellBuildTest(unittest.TestCase):
    def test_build_kind_and_summary(self):
        failed = shell.build({"cmd": "make test", "exit": 1, "seconds": 2, "cwd": "/x"}, now=100.0)
        self.assertEqual(failed.kind, "command_failed")
        self.assertIn("exit 1", failed.summary)
        finished = shell.build({"cmd": "make test", "exit": 0, "seconds": 2, "cwd": "/x"}, now=100.0)
        self.assertEqual(finished.kind, "command_finished")

    def test_build_clips_a_long_command_in_the_summary(self):
        long_cmd = "x" * 200
        sig = shell.build({"cmd": long_cmd, "exit": 1, "seconds": 1, "cwd": "/x"}, now=1.0)
        self.assertLess(len(sig.summary), len(long_cmd))


class SummaryFilterTest(unittest.TestCase):
    def test_sense_summaries_are_one_clean_line(self):
        """U33: a summary becomes notice text, then the assistant's own words in history - it passes
        `oneline` where the sense builds it (as mail and catalog do), so the ledger holds it clean."""
        from unittest import mock
        from pathfinder.senses import calendar, claude_code, reminders
        from pathfinder.tools import oneline
        bad = "a\x1b[31m\u202eb\u200b\nc"
        ev = {"id": "e", "title": bad, "start": 2000.0, "end": None, "all_day": False, "notes": bad}
        with mock.patch.object(calendar.ek, "list_events", return_value=[ev]), \
                mock.patch.object(reminders.ek, "list_reminders", return_value=[{"id": "r", "title": bad, "due": 2000.0}]):
            sigs = calendar.poll({}, 1000.0) + reminders.poll({}, 1000.0)
        with tempfile.TemporaryDirectory() as tmp:
            config.set(Path(tmp), "schedules", [{"id": "s", "at": "2026-09-22 09:00", "repeat": "none", "text": bad}])
            sigs += schedule.poll({"_home": Path(tmp)}, clock.parse_local("2026-09-22 09:01")[0])
        sigs.append(shell.build({"cmd": bad, "exit": 1, "seconds": 1, "cwd": "/x"}, now=1.0))
        sigs.append(claude_code.build({"event": "Notification", "cwd": "/x", "message": bad}, now=1.0))
        self.assertEqual([s.sense for s in sigs], ["calendar", "reminders", "schedule", "shell", "claude_code"])
        for s in sigs:
            self.assertEqual(s.summary, oneline(s.summary))
            self.assertTrue(s.summary.endswith("ab c"), s.summary)
        self.assertEqual([sigs[0].fields["title"], sigs[0].fields["notes"], sigs[1].fields["title"]], ["ab c"] * 3)


class MemorySenseTest(unittest.TestCase):
    def test_memory_kinds_default_silence_and_shipped_rules_speak(self):
        loaded = signals.load({"senses": {"memory": {}}})
        mem = next(s for s in loaded if s.name == "memory")
        self.assertEqual({k: row[2] for k, row in mem.kinds.items()},
                         {"memory_proposed": "silence", "memory_stalled": "silence"})
        parsed = rules.parse(rules.DEFAULT_RULES_PATH.read_text(encoding="utf-8"),
                             conditions=rules.CORE_CONDITIONS, validated_kinds=set(mem.kinds))
        today = Today(0, 0, None, 0, {})
        for kind in ("memory_proposed", "memory_stalled"):
            sig = signals.Signal(sense="memory", kind=kind, key=f"k-{kind}", summary="s",
                                 fields={}, trust="self")
            action, _why = rules.decide(sig, parsed, {}, today, 1000.0)
            self.assertEqual(action, "speak")


class ShellThreeFailuresTest(unittest.TestCase):
    def test_shell_three_failures_speak_one_silent(self):
        """The shipped RULES.md: fewer than 3 failures in the last 10 minutes stays silent; the
        3rd occurrence itself (count_over's window includes the current signal) speaks."""
        parsed = rules.parse(
            rules.DEFAULT_RULES_PATH.read_text(encoding="utf-8"),
            conditions={**rules.CORE_CONDITIONS, **shell.CONDITIONS},
            validated_kinds={"command_failed", "command_finished"},
        )
        sig = shell.build({"cmd": "flaky", "exit": 1, "seconds": 1, "cwd": "/x"}, now=1000.0)
        actions = []
        for count in (1, 2, 3):  # this failure IS the Nth occurrence, not one before it
            today = Today(spoken=0, judgments=0, last_signal_ts=None, burst=0,
                         recent_counts={"command_failed": count})
            action, _why = rules.decide(sig, parsed, {}, today, 1000.0)
            actions.append(action)
        self.assertEqual(actions, ["silence", "silence", "speak"])

    def test_command_finished_speaks_only_past_five_minutes(self):
        parsed = rules.parse(
            rules.DEFAULT_RULES_PATH.read_text(encoding="utf-8"),
            conditions={**rules.CORE_CONDITIONS, **shell.CONDITIONS},
            validated_kinds={"command_failed", "command_finished"},
        )
        today = Today(0, 0, None, 0, {})
        quick = shell.build({"cmd": "ls", "exit": 0, "seconds": 10, "cwd": "/x"}, now=1.0)
        slow = shell.build({"cmd": "build", "exit": 0, "seconds": 400, "cwd": "/x"}, now=1.0)
        self.assertEqual(rules.decide(quick, parsed, {}, today, 1000.0)[0], "silence")
        self.assertEqual(rules.decide(slow, parsed, {}, today, 1000.0)[0], "speak")


if __name__ == "__main__":
    unittest.main()
