import json
import unittest

from pathfinder.llm import LLMError
from pathfinder.loop import FLAG_WORDS, FORGED_WHAT, FORGED_WHY, RERUN_NOTE, STEER_ENVELOPE, VERIFIER_PROMPT, TurnControl, run, settle, verifier_input
from pathfinder.state import RECORD_HEADER
from pathfinder.signals import AGENDA_HEADER


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

    def test_loop_keeps_hangul_readable_for_the_model(self):
        """Live 2026-09-25: `\\uB5A1...` escapes in a tool result made gemma4 read "떡집" as
        "respecting" - the model must see the characters, in results and in its echoed args."""
        tool_call = {"id": "call_0", "name": "list_events", "arguments": {"q": "추석"}}
        step = _make_step([[("tool_calls", [tool_call])], [("text", "ok")]])
        messages = []
        run(step, messages, call_tool=lambda id, n, a: {"ok": True, "result": [{"title": "떡집 들르기"}]},
            emit=lambda k, p: None)
        self.assertIn("떡집 들르기", [m for m in messages if m.get("role") == "tool"][0]["content"])
        echoed = [m for m in messages if m.get("tool_calls")][0]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("추석", echoed)

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


def _verdict(why="", **flags):
    return json.dumps({**{k: False for k in FLAG_WORDS}, **flags, "why": why})


def _looks(*replies):
    """The verifier's step: records each ask, answers the next scripted reply."""
    def step(messages):
        step.asks.append(messages)
        return iter(replies[len(step.asks) - 1])
    step.asks = []
    return step


def _turn(user="what's on my list?"):
    return [{"role": "system", "content": "the resident prompt"},
            {"role": "system", "content": f"Local time: now\n\n{AGENDA_HEADER}\n- 09:00 standup\n\nwiki map"},
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": user}]


class SecondLookTest(unittest.TestCase):
    """D51: a final answer that came with no tool result is checked once by the same model."""
    TOOLS = ["list_reminders", "list_events", "mcp__notes__search"]

    def _run(self, steps, looks, messages=None, **kw):
        messages = _turn() if messages is None else messages
        emitted = []
        out = run(_make_step(steps), messages, call_tool=lambda id, n, a: {"ok": True},
                  emit=lambda k, p: emitted.append((k, p)), final_step=looks,
                  second_look=kw.pop("second_look", self.TOOLS), **kw)
        return out, messages, emitted

    def test_second_look_passes_a_clean_draft(self):
        looks = _looks([("text", _verdict())])
        out, messages, emitted = self._run([[("text", "Hi there!")]], looks)
        self.assertEqual((out.kind, out.text, out.check, out.steps), ("answer", "Hi there!", "passed", 1))
        self.assertEqual(messages[-1], {"role": "assistant", "content": "Hi there!"})
        self.assertEqual(len(looks.asks), 1)
        self.assertNotIn("retract", [k for k, _ in emitted])

    def test_flagged_draft_is_withdrawn_and_rerun_once(self):
        looks = _looks([("text", "ok: " + _verdict("no list_reminders result", unsupported_state=True))])
        out, messages, emitted = self._run([[("text", "Your list is empty.")], [("text", "It holds two.")]], looks)
        self.assertEqual(emitted[1], ("retract", {"draft": "Your list is empty.", "flags": ["unsupported_state"],
                                                  "what": FLAG_WORDS["unsupported_state"],
                                                  "why": "no list_reminders result"}))
        self.assertEqual([p for k, p in emitted if k == "delta"], ["Your list is empty.", "It holds two."])
        self.assertEqual((out.kind, out.text, out.check, out.steps), ("answer", "It holds two.", "flagged", 2))
        note = RERUN_NOTE.format(what=FLAG_WORDS["unsupported_state"], why="no list_reminders result")
        self.assertEqual(messages[5:], [{"role": "system", "content": note},
                                        {"role": "assistant", "content": "It holds two."}])
        self.assertEqual(len(looks.asks), 1)  # the re-run is not checked again

    def test_forged_record_is_withdrawn_and_rerun_once(self):
        forged = "Done.\n" + RECORD_HEADER + '\nrun_command {"command":"ls"} ok'
        looks = _looks([("text", _verdict())])
        out, messages, emitted = self._run([[("text", forged)], [("text", "I have not run it yet.")]], looks)
        self.assertEqual(emitted[1], ("retract", {"draft": forged, "flags": ["forged_record"],
                                                  "what": FORGED_WHAT, "why": FORGED_WHY}))
        self.assertEqual((out.kind, out.text, out.check, out.steps), ("answer", "I have not run it yet.", "passed", 2))
        note = RERUN_NOTE.format(what=FORGED_WHAT, why=FORGED_WHY)
        self.assertEqual(messages[5:], [{"role": "system", "content": note},
                                        {"role": "assistant", "content": "I have not run it yet."}])
        self.assertEqual(len(looks.asks), 1)  # the second look sees the re-run, never the forged draft

    def test_forged_record_on_the_rerun_is_cut(self):
        forged = lambda t: t + "\n\n" + RECORD_HEADER + "\nlist_events {} ok"  # noqa: E731
        call = {"id": "c1", "name": "list_events", "arguments": {}}
        steps = [[("tool_calls", [call])], [("text", forged("Nothing today."))], [("text", forged("Still nothing."))]]
        out, messages, emitted = self._run(steps, _looks(), second_look=None)  # checked with no second look too
        self.assertEqual([k for k, _ in emitted].count("retract"), 1)
        self.assertEqual((out.kind, out.text, out.steps), ("answer", "Still nothing.", 3))
        self.assertEqual(messages[-1], {"role": "assistant", "content": "Still nothing."})
        out, _, _ = self._run([[("text", forged("Done."))]], _looks(), budget=1)  # no step left: cut at once
        self.assertEqual((out.kind, out.text), ("answer", "Done."))

    def test_second_look_skipped_after_a_tool_or_on_a_resume(self):
        call = {"id": "c1", "name": "list_reminders", "arguments": {}}
        looks = _looks()
        out, _, _ = self._run([[("tool_calls", [call])], [("text", "It is empty.")]], looks)
        pending = _turn() + [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_reminders", "arguments": "{}"}}]}]
        resumed, _, _ = self._run([[("text", "It is empty.")]], looks, messages=pending, resume=True)
        self.assertEqual((out.check, resumed.check, looks.asks), (None, None, []))

    def test_second_look_runs_when_every_call_was_refused_but_not_after_a_real_one(self):
        refused = {"ok": False, "refused": True, "error": "no such tool: x"}
        denied = {"ok": False, "refused": True, "denied": True, "error": "policy"}
        call = lambda i: {"id": i, "name": "x", "arguments": {}}  # noqa: E731
        steps = [[("tool_calls", [call("c1")])], [("text", "It is empty.")]]
        looks = _looks([("text", _verdict())])
        messages = _turn()
        out = run(_make_step(steps), messages, call_tool=lambda i, n, a: refused, emit=lambda k, p: None,
                  final_step=looks, second_look=self.TOOLS)
        self.assertEqual((out.check, len(looks.asks)), ("passed", 1))
        mixed = {"c1": refused, "c2": {"ok": True, "result": 1}}
        steps = [[("tool_calls", [call("c1"), call("c2")])], [("text", "It is empty.")]]
        looks = _looks()
        out = run(_make_step(steps), _turn(), call_tool=lambda i, n, a: mixed[i], emit=lambda k, p: None,
                  final_step=looks, second_look=self.TOOLS)
        self.assertEqual((out.check, looks.asks), (None, []))
        looks = _looks()  # a policy deny counts as a call: the verifier does not see this turn's refusals
        out = run(_make_step([[("tool_calls", [call("c1")])], [("text", "Your settings block it.")]]), _turn(),
                  call_tool=lambda i, n, a: denied, emit=lambda k, p: None, final_step=looks, second_look=self.TOOLS)
        self.assertEqual((out.check, looks.asks), (None, []))

    def test_second_look_needs_a_step_left(self):
        looks = _looks()
        out, _, _ = self._run([[("text", "It is empty.")]], looks, budget=1)
        self.assertEqual((out.kind, out.text, out.check, looks.asks), ("answer", "It is empty.", None, []))

    def test_bad_json_or_llm_error_or_cancel_leaves_the_draft(self):
        def raising(messages):
            yield ("text", "{")
            raise LLMError(500, "upstream")
        for look in (_looks([("text", "The draft looks fine.")]), raising, _looks([("cancelled", None)])):
            out, messages, emitted = self._run([[("text", "It is empty.")]], look)
            self.assertEqual((out.kind, out.text, out.check, out.steps), ("answer", "It is empty.", "failed", 1))
            self.assertEqual(messages[-1], {"role": "assistant", "content": "It is empty."})
            self.assertNotIn("retract", [k for k, _ in emitted])

    def test_verifier_input_names_every_tool_and_carries_the_agenda(self):
        looks = _looks([("text", _verdict())])
        messages = _turn("q" * 1100)
        messages[3]["content"] = "p" * 700
        self._run([[("text", "Nothing today.")]], looks, messages=messages)
        self.assertEqual(looks.asks[0][0], {"role": "system", "content": VERIFIER_PROMPT})
        self.assertEqual(looks.asks[0][1], {"role": "user", "content": "\n\n".join([
            "Tools: list_reminders, list_events, mcp__notes__search",
            f"Agenda the system gave this turn:\n{AGENDA_HEADER}\n- 09:00 standup",
            "The assistant's previous answer: " + "p" * 600, "The user: " + "q" * 1000,
            "No tool was called in this turn.", "Draft: Nothing today."])})

    def test_verifier_input_carries_the_latest_record_when_there_is_one(self):
        record = RECORD_HEADER + '\nsearch_tools {"query":"gmail"} ok'
        with_old = _turn()
        with_old.insert(2, {"role": "system", "content": RECORD_HEADER + "\nold_call {} (approval p1) waiting for approval"})
        with_old.insert(4, {"role": "system", "content": record})
        asked = verifier_input(self.TOOLS, with_old + [{"role": "assistant", "content": "d"}])
        self.assertIn('state - not results):\nold_call {} (approval p1) waiting for approval\n'
                      'search_tools {"query":"gmail"} ok\n\nThe user:', asked)  # an older block's gate survives
        long = list(with_old)
        long.insert(4, {"role": "system", "content": RECORD_HEADER + "\n" + "x" * 1600 + "\nlast {} ok"})
        tail = verifier_input(self.TOOLS, long + [{"role": "assistant", "content": "d"}]).split("not results):\n")[1]
        self.assertTrue(tail.startswith("last {} ok\n"))  # cut at a row boundary, newest rows kept
        self.assertNotIn(RECORD_HEADER, asked)
        bare = verifier_input(self.TOOLS, _turn() + [{"role": "assistant", "content": "d"}])
        self.assertNotIn("tool calls in earlier turns", bare)

    def test_verifier_usage_counts_but_not_as_a_step(self):
        looks = _looks([("text", _verdict()), ("usage", {"prompt_tokens": 100, "completion_tokens": 9})])
        out, _, _ = self._run([[("text", "Hi."), ("usage", {"prompt_tokens": 10, "completion_tokens": 2})]], looks)
        self.assertEqual((out.steps, out.usage), (1, {"prompt_tokens": 110, "completion_tokens": 11}))

    def test_why_is_one_line_and_cut(self):
        why = ("the agenda says so\n" * 300)[:5000]
        looks = _looks([("text", _verdict(why, unsupported_state=True))])
        out, messages, emitted = self._run([[("text", "Empty.")], [("text", "Two.")]], looks)
        cut = ("the agenda says so " * 12)[:200]
        self.assertEqual(emitted[1][1]["why"], cut)
        self.assertEqual(messages[5]["content"], RERUN_NOTE.format(what=FLAG_WORDS["unsupported_state"], why=cut))

    def test_interrupt_during_the_look_still_cuts_it(self):
        """The look runs as a tool does: a steer waits for it (the daemon reads `in_tool`), an
        interrupt still cancels it - the draft stands, check failed."""
        control = TurnControl()

        def look(messages):
            look.in_tool = control.in_tool
            control.interrupt.set()
            yield ("cancelled", None)
        out, messages, _ = self._run([[("text", "Empty.")]], look, control=control)
        self.assertEqual((look.in_tool, control.in_tool), (True, False))
        self.assertEqual((out.kind, out.text, out.check), ("answer", "Empty.", "failed"))

    def test_llm_error_mid_stream_carries_what_was_streamed(self):
        """Invariant 2: text already on the screen rides the error to the ledger."""
        def failing(messages):
            yield ("text", "half an ans")
            raise LLMError(500, "upstream")
        call = {"id": "c1", "name": "noop", "arguments": {}}
        steps = iter([iter([("text", "looking. "), ("tool_calls", [call])]), failing([])])
        with self.assertRaises(LLMError) as caught:
            run(lambda messages: next(steps), _turn(), call_tool=lambda id, n, a: {"ok": True}, emit=lambda k, p: None)
        self.assertEqual(caught.exception.said, "looking. half an ans")
