import unittest
from unittest import mock

from pathfinder import rules
from pathfinder.llm import LLMError
from pathfinder.signals import Signal


def _today(spoken=0, judgments=0, last_signal_ts=None, burst=0, recent_counts=None):
    from pathfinder.state import Today
    return Today(spoken, judgments, last_signal_ts, burst, recent_counts or {})


def _signal(kind="command_failed", key="k1", trust="self", fields=None):
    return Signal(sense="shell", kind=kind, key=key, summary="s", fields=fields or {}, trust=trust)


class ParseClosedVocabTest(unittest.TestCase):
    def test_rules_parse_closed_vocab(self):
        text = "---\n---\n## command_failed\n\n| bogus_word | 3 | speak | |\n"
        with self.assertRaises(ValueError):
            rules.parse(text, validated_kinds={"command_failed"})

    def test_unvalidated_section_is_kept_but_not_checked(self):
        """A section for a kind with no loaded sense is parsed, not rejected - its words are
        simply not checked (the sense may be off)."""
        text = "---\n---\n## new_mail\n\n| totally_unknown | 1 | speak | |\n"
        parsed = rules.parse(text, validated_kinds=frozenset())
        self.assertEqual(parsed.by_kind["new_mail"], [("totally_unknown", "1", "speak", "")])

    def test_default_validates_every_kind_found(self):
        with self.assertRaises(ValueError):
            rules.parse("---\n---\n## anything\n\n| nope | 1 | speak | |\n")  # validated_kinds=None


class FirstMatchTest(unittest.TestCase):
    def test_rules_first_match_wins(self):
        text = ("---\n---\n## command_failed\n\n"
                "| count_over | 3 | speak | |\n"
                "| always | - | silence | |\n")
        parsed = rules.parse(text)
        hot = _today(recent_counts={"command_failed": 5})
        action, why = rules.decide(_signal(), parsed, {}, hot, 1000.0)
        self.assertEqual((action, why), ("speak", "count_over=3"))
        cold = _today(recent_counts={"command_failed": 1})
        action2, why2 = rules.decide(_signal(), parsed, {}, cold, 1000.0)
        self.assertEqual((action2, why2), ("silence", "always=-"))


class NoRowTest(unittest.TestCase):
    def test_rules_no_row_is_ask_model(self):
        parsed = rules.parse("---\n---\n## command_failed\n\n| count_over | 99 | speak | |\n")
        action, why = rules.decide(_signal(), parsed, {}, _today(recent_counts={}), 1000.0)
        self.assertEqual((action, why), ("ask_model", "no rule matched"))
        # a kind with no section at all is the same story
        action2, _ = rules.decide(_signal(kind="unheard_of"), parsed, {}, _today(), 1000.0)
        self.assertEqual(action2, "ask_model")


class PrefiltersTest(unittest.TestCase):
    def test_rules_prefilters(self):
        parsed = rules.parse("---\ndaily_budget: 2\nquiet_hours: 23-8\n---\n## command_failed\n\n| always | - | speak | |\n")
        with mock.patch("pathfinder.rules.clock.hour", return_value=2):  # inside 23-8 (wraps midnight)
            self.assertEqual(rules.decide(_signal(), parsed, {}, _today(), 1000.0), ("defer", "quiet hours"))
            # the user's own 3 a.m. alarm is theirs to hear: trust "user" skips quiet hours too
            self.assertEqual(rules.decide(_signal(trust="user"), parsed, {}, _today(), 1000.0)[0], "speak")
        with mock.patch("pathfinder.rules.clock.hour", return_value=12):
            self.assertEqual(rules.decide(_signal(), parsed, {}, _today(spoken=2), 1000.0),
                             ("defer", "daily budget spent"))
            # a signal the user started (trust "user", e.g. schedule) is exempt from the budget
            self.assertEqual(rules.decide(_signal(trust="user"), parsed, {}, _today(spoken=99), 1000.0)[0], "speak")


class ResolveAndJudgeTest(unittest.TestCase):
    def test_resolve_user_lists(self):
        self.assertEqual(rules.resolve("USER.waiting_for", {"waiting_for": ["a@example.com"]}), ["a@example.com"])
        self.assertEqual(rules.resolve("USER.missing", {}), [])
        self.assertIsNone(rules.resolve("-", {}))
        self.assertEqual(rules.resolve("true", {}), True)
        self.assertEqual(rules.resolve("3", {}), 3)


class JudgeTest(unittest.TestCase):
    """D13's model call - one request, no tools, no history, strict JSON."""

    CFG = {"model": "p:m", "providers": {"p": {"base_url": "http://x", "api_key_env": ""}}}

    def test_judge_strict_json(self):
        with mock.patch("pathfinder.llm.openai_compat.stream",
                        return_value=iter([("text", '{"speak": true, "reason": "waiting for this"}')])):
            self.assertEqual(rules.judge(_signal(), self.CFG), (True, "waiting for this"))

    def test_judge_strips_a_json_fence(self):
        """Note 8: a model that wraps its answer in a ```json fence must still parse."""
        fenced = '```json\n{"speak": true, "reason": "fenced"}\n```'
        with mock.patch("pathfinder.llm.openai_compat.stream", return_value=iter([("text", fenced)])):
            self.assertEqual(rules.judge(_signal(), self.CFG), (True, "fenced"))

    def test_judge_garbage_is_false(self):
        with mock.patch("pathfinder.llm.openai_compat.stream", return_value=iter([("text", "not json at all")])):
            self.assertEqual(rules.judge(_signal(), self.CFG), (False, "unparseable"))

    def test_judge_missing_fields_is_false(self):
        with mock.patch("pathfinder.llm.openai_compat.stream", return_value=iter([("text", '{"speak": true}')])):
            self.assertEqual(rules.judge(_signal(), self.CFG), (False, "unparseable"))

    def test_judge_llm_error_is_false(self):
        with mock.patch("pathfinder.llm.openai_compat.stream", side_effect=LLMError(402, "no credit")):
            self.assertEqual(rules.judge(_signal(), self.CFG), (False, "model unavailable"))

    def test_judge_no_model_configured_is_false(self):
        self.assertEqual(rules.judge(_signal(), {}), (False, "model unavailable"))

    def test_judge_uses_worker_model_when_set(self):
        cfg = {**self.CFG, "worker_model": "p:worker"}
        seen = {}

        def fake_stream(endpoint, messages, tools=None):
            seen["model"] = endpoint.model
            return iter([("text", '{"speak": false, "reason": "not urgent"}')])

        with mock.patch("pathfinder.llm.openai_compat.stream", side_effect=fake_stream):
            rules.judge(_signal(), cfg)
        self.assertEqual(seen["model"], "worker")


if __name__ == "__main__":
    unittest.main()
