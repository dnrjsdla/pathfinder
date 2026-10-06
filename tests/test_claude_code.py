import unittest

from pathfinder import rules
from pathfinder.senses import claude_code
from pathfinder.state import Today


def _parsed():
    return rules.parse(rules.DEFAULT_RULES_PATH.read_text(encoding="utf-8"),
                       conditions={**rules.CORE_CONDITIONS, **claude_code.CONDITIONS},
                       validated_kinds={"task_done", "needs_input"})


class ClaudeCodeBuildTest(unittest.TestCase):
    def test_build_kinds(self):
        stop = claude_code.build({"event": "Stop", "cwd": "/x"}, now=1.0)
        notif = claude_code.build({"event": "Notification", "cwd": "/x"}, now=1.0)
        self.assertEqual(stop.kind, "task_done")
        self.assertEqual(notif.kind, "needs_input")
        self.assertEqual(stop.trust, "self")

    def test_build_uses_the_hook_message_when_given(self):
        sig = claude_code.build({"event": "Stop", "cwd": "/x", "message": "tests are green"}, now=1.0)
        self.assertEqual(sig.summary, "tests are green")


class ClaudeCodeAwayTest(unittest.TestCase):
    def test_claude_code_speaks_only_when_away(self):
        parsed = _parsed()
        stop = claude_code.build({"event": "Stop", "cwd": "/x"}, now=1.0)
        notif = claude_code.build({"event": "Notification", "cwd": "/x"}, now=1.0)
        away = Today(0, 0, None, 0, {}, away=True)
        present = Today(0, 0, None, 0, {}, away=False)
        self.assertEqual(rules.decide(stop, parsed, {}, away, 1000.0)[0], "speak")
        self.assertEqual(rules.decide(stop, parsed, {}, present, 1000.0)[0], "silence")
        self.assertEqual(rules.decide(notif, parsed, {}, away, 1000.0)[0], "speak")
        self.assertEqual(rules.decide(notif, parsed, {}, present, 1000.0)[0], "silence")


if __name__ == "__main__":
    unittest.main()
