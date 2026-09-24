import unittest

from pathfinder.loop import STEER_ENVELOPE, TurnControl, run, settle


def _make_step(steps):
    """steps: a list of lists of (kind, payload) tuples, one list per call
    to step(messages)."""
    calls = iter(steps)

    def step(messages):
        return iter(next(calls))

    return step


class TestLoop(unittest.TestCase):
    def test_loop_tool_roundtrip(self):
        tool_call = {"id": "call_0", "name": "get_weather", "arguments": {"city": "Busan"}}
        step = _make_step([
            [("tool_calls", [tool_call])],
            [("text", "It's sunny."), ("usage", {"prompt_tokens": 5, "completion_tokens": 2})],
        ])
        seen_calls = []
        deltas = []

        def call_tool(id, name, args):
            seen_calls.append((id, name, args))
            return {"ok": True, "forecast": "Sunny"}

        def emit(kind, payload):
            if kind == "delta":
                deltas.append(payload)

        messages = [{"role": "user", "content": "weather?"}]
        outcome = run(step, messages, call_tool=call_tool, emit=emit)

        self.assertEqual(outcome.kind, "answer")
        self.assertEqual(seen_calls, [("call_0", "get_weather", {"city": "Busan"})])
        self.assertEqual(deltas, ["It's sunny."])
        self.assertEqual(outcome.usage, {"prompt_tokens": 5, "completion_tokens": 2})
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call_0")

    def test_loop_budget_exhausted(self):
        tool_call = {"id": "call_0", "name": "noop", "arguments": {}}
        step = _make_step([[("tool_calls", [tool_call])]] * 3)
        outcome = run(step, [], call_tool=lambda id, n, a: {"ok": True},
                      emit=lambda k, p: None, budget=3)
        self.assertEqual(outcome.kind, "exhausted")
        self.assertEqual(outcome.steps, 3)

    def test_loop_final_step_answers_when_exhausted(self):
        """D30: budget spent on tool calls -> one tool-less request, the turn ends in words."""
        tool_call = {"id": "call_0", "name": "noop", "arguments": {}}
        step = _make_step([[("tool_calls", [tool_call])]] * 2)
        final = _make_step([[("text", "looked twice, found nothing")]])
        messages, deltas = [], []
        outcome = run(step, messages, call_tool=lambda id, n, a: {"ok": True},
                      emit=lambda k, p: deltas.append(p) if k == "delta" else None, budget=2, final_step=final)
        self.assertEqual(outcome.kind, "exhausted")
        self.assertEqual(outcome.text, "looked twice, found nothing")
        self.assertEqual(deltas, ["looked twice, found nothing"])
        self.assertEqual(messages[-1], {"role": "assistant", "content": "looked twice, found nothing"})

    def test_loop_does_not_echo_reasoning(self):
        step = _make_step([[("reasoning", "thinking..."), ("text", "answer")]])
        messages = []
        run(step, messages, call_tool=lambda id, n, a: {}, emit=lambda k, p: None)
        assistant_msg = messages[-1]
        self.assertEqual(set(assistant_msg), {"role", "content"})
        self.assertEqual(assistant_msg["content"], "answer")

    def test_loop_cancelled(self):
        step = _make_step([[("text", "partial"), ("cancelled", None)]])
        outcome = run(step, [], call_tool=lambda id, n, a: {}, emit=lambda k, p: None)
        self.assertEqual(outcome.kind, "cancelled")

    def test_loop_interrupt_is_cancelled(self):
        control = TurnControl()
        control.interrupt.set()
        step = _make_step([[("text", "partial"), ("cancelled", None)]])
        outcome = run(step, [], call_tool=lambda id, n, a: {}, emit=lambda k, p: None, control=control)
        self.assertEqual(outcome.kind, "cancelled")

    def test_loop_interrupt_before_a_step_spends_no_request(self):
        """The real adapter only sees `cancel` between chunks, i.e. after the request went out
        (review, partition 2): an interrupt already set must stop the loop before it calls step."""
        control = TurnControl()
        calls = []

        def step(messages):
            calls.append(1)
            return iter([("tool_calls", [{"id": "c1", "name": "noop", "arguments": {}}])])

        def tool(id, name, args):
            control.interrupt.set()  # arrives while the tool runs
            return {"ok": True}

        outcome = run(step, [], call_tool=tool, emit=lambda k, p: None, control=control)
        self.assertEqual((outcome.kind, len(calls)), ("cancelled", 1))

    def test_loop_steer_at_tool_boundary(self):
        """A steer queued during a fake tool call becomes a plain user message
        before the next step - no envelope."""
        control = TurnControl()
        seen_messages = []

        def step(messages):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return iter([("tool_calls", [{"id": "c1", "name": "noop", "arguments": {}}])])
            return iter([("text", "done")])

        def call_tool(id, name, args):
            control.steers.append("do it differently")  # queued mid-tool-call, like the daemon does
            return {"ok": True}

        outcome = run(step, [], call_tool=call_tool, emit=lambda k, p: None, control=control)
        self.assertEqual(outcome.kind, "answer")
        self.assertEqual(len(seen_messages), 2)
        last_user = [m for m in seen_messages[1] if m.get("role") == "user"]
        self.assertEqual(last_user[-1]["content"], "do it differently")
        self.assertNotIn(STEER_ENVELOPE, last_user[-1]["content"])

    def test_loop_steer_mid_stream(self):
        """A fake step that yields text then cancelled with steer_cut set: the
        partial is kept as an assistant message, the steer is appended inside
        the envelope, and the next step runs."""
        control = TurnControl()

        def step(messages):
            if not any(m.get("role") == "user" and STEER_ENVELOPE in m.get("content", "") for m in messages):
                control.steers.append("stop at 20")
                control.steer_cut.set()
                return iter([("text", "1\n2\n3\n"), ("cancelled", None)])
            return iter([("text", "4\n...\n20\n")])

        messages = [{"role": "user", "content": "count to 100"}]
        outcome = run(step, messages, call_tool=lambda id, n, a: {}, emit=lambda k, p: None, control=control)

        self.assertEqual(outcome.kind, "answer")
        partials = [m for m in messages if m.get("role") == "assistant" and m.get("content") == "1\n2\n3\n"]
        self.assertEqual(len(partials), 1)
        envelope_msgs = [m for m in messages if m.get("role") == "user" and STEER_ENVELOPE in m["content"]]
        self.assertEqual(len(envelope_msgs), 1)
        self.assertIn("stop at 20", envelope_msgs[0]["content"])
        self.assertFalse(control.steer_cut.is_set())  # cleared once drained

    def test_loop_resume_settles_outstanding_calls(self):
        """A trailing assistant message with two calls, one already answered:
        only the other runs."""
        messages = [
            {"role": "user", "content": "do two things"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
        ]
        seen = []

        def call_tool(id, name, args):
            seen.append((id, name))
            return {"ok": True}

        settle(messages, call_tool, control=None)
        self.assertEqual(seen, [("c2", "b")])
        tool_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        self.assertEqual(sorted(tool_ids), ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
