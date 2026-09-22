import tempfile
import unittest
from pathlib import Path

from pathfinder import clock, config, rules
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


class ShellThreeFailuresTest(unittest.TestCase):
    def test_shell_three_failures_speak_one_silent(self):
        """The shipped RULES.md: fewer than 3 failures recorded today stays silent; the 3rd
        (state.today's count_over reaching 3) speaks."""
        parsed = rules.parse(
            rules.DEFAULT_RULES_PATH.read_text(encoding="utf-8"),
            conditions={**rules.CORE_CONDITIONS, **shell.CONDITIONS},
            validated_kinds={"command_failed", "command_finished"},
        )
        sig = shell.build({"cmd": "flaky", "exit": 1, "seconds": 1, "cwd": "/x"}, now=1000.0)
        actions = []
        for count in (0, 1, 2, 3):
            today = Today(spoken=0, judgments=0, last_signal_ts=None, burst=0,
                         kind_counts={"command_failed": count})
            action, _why = rules.decide(sig, parsed, {}, today, 1000.0)
            actions.append(action)
        self.assertEqual(actions, ["silence", "silence", "silence", "speak"])

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
