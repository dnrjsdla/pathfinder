import unittest

from pathfinder.loop import run


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

        def call_tool(name, args):
            seen_calls.append((name, args))
            return {"ok": True, "forecast": "Sunny"}

        def emit(kind, payload):
            if kind == "delta":
                deltas.append(payload)

        messages = [{"role": "user", "content": "weather?"}]
        outcome = run(step, messages, call_tool=call_tool, emit=emit)

        self.assertEqual(outcome.kind, "answer")
        self.assertEqual(seen_calls, [("get_weather", {"city": "Busan"})])
        self.assertEqual(deltas, ["It's sunny."])
        self.assertEqual(outcome.usage, {"prompt_tokens": 5, "completion_tokens": 2})
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call_0")

    def test_loop_budget_exhausted(self):
        tool_call = {"id": "call_0", "name": "noop", "arguments": {}}
        step = _make_step([[("tool_calls", [tool_call])]] * 3)
        outcome = run(step, [], call_tool=lambda n, a: {"ok": True},
                      emit=lambda k, p: None, budget=3)
        self.assertEqual(outcome.kind, "exhausted")
        self.assertEqual(outcome.steps, 3)

    def test_loop_does_not_echo_reasoning(self):
        step = _make_step([[("reasoning", "thinking..."), ("text", "answer")]])
        messages = []
        run(step, messages, call_tool=lambda n, a: {}, emit=lambda k, p: None)
        assistant_msg = messages[-1]
        self.assertEqual(set(assistant_msg), {"role", "content"})
        self.assertEqual(assistant_msg["content"], "answer")

    def test_loop_cancelled(self):
        step = _make_step([[("text", "partial"), ("cancelled", None)]])
        outcome = run(step, [], call_tool=lambda n, a: {}, emit=lambda k, p: None)
        self.assertEqual(outcome.kind, "cancelled")


if __name__ == "__main__":
    unittest.main()
