"""The daemon end to end over a real unix socket, against a fake model."""
import asyncio
import dataclasses
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import config
from pathfinder import tools as tools_mod
from pathfinder import tui
from pathfinder.daemon import SOCKET, Daemon
from pathfinder.deliver import Notice
from pathfinder.ledger import Ledger
from pathfinder.llm import LLMError
from pathfinder.mcp import pins as mcp_pins
from pathfinder.loop import FLAG_WORDS, VERIFIER_PROMPT, TurnControl
from pathfinder.signals import Signal
from pathfinder.state import State
from pathfinder.tools import ToolSpec


def fake_model(reply="Hello there."):
    def factory(cfg, cancel=None, tools=None):
        factory.cancels.append(cancel)
        def step(messages):
            if messages[0]["content"] != VERIFIER_PROMPT:  # the second look's ask is not a turn's request
                step.seen.append([dict(m) for m in messages])
            for word in reply.split(" "):
                yield ("text", word + " ")
            yield ("usage", {"prompt_tokens": 10, "completion_tokens": 3})
        step.seen = factory.seen
        return step
    factory.seen = []
    factory.cancels = []
    return factory


def blocked_model(cfg, cancel=None, tools=None):
    def step(messages):
        raise LLMError(402, "this model is not included in your free usage")
        yield  # pragma: no cover - makes this a generator like the real adapter
    return step


class Client:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def open(cls, home):
        return cls(*await asyncio.open_unix_connection(str(Path(home) / SOCKET)))

    def send(self, obj):
        self.writer.write((json.dumps(obj) + "\n").encode() if isinstance(obj, dict) else obj)

    async def recv(self):
        line = await asyncio.wait_for(self.reader.readline(), 5)
        assert line, "connection closed"
        return json.loads(line)

    async def until(self, type_):
        seen = []
        while True:
            msg = await self.recv()
            seen.append(msg)
            if msg["type"] == type_:
                return seen

    def close(self):
        self.writer.close()


class DaemonHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, factory, scenario, job_factory=None, pass_factory=None):
        async def main():
            daemon = Daemon(self.home, step_factory=factory, job_step_factory=job_factory,
                            pass_step_factory=pass_factory)
            await daemon.start()
            try:
                return await scenario(daemon)
            finally:
                await daemon.close()
        return asyncio.run(main())

    def events(self):
        return list(Ledger(self.home).read())


class DaemonTest(DaemonHarness):
    def test_daemon_round_trip(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = await c.recv()
            c.send({"type": "say", "text": "hi"})
            seen = await c.until("turn.end")
            c.close()
            return attached, seen

        attached, seen = self.run_with(fake_model("Hello there."), scenario)
        self.assertEqual(attached["type"], "attached")
        self.assertFalse(attached["resumed"])
        self.assertEqual([m["type"] for m in seen], ["turn.start", "delta", "delta", "turn.end"])
        self.assertEqual("".join(m["text"] for m in seen if m["type"] == "delta"), "Hello there. ")
        self.assertEqual(seen[-1]["kind"], "answer")
        types = [e["type"] for e in self.events()]
        self.assertEqual(types, ["session_start", "user", "turn_start", "assistant", "turn_end"])
        user = self.events()[1]
        self.assertEqual((user["origin"], user["trust"], user["text"]), ("user", "user", "hi"))

    def test_second_turn_sees_the_first(self):
        factory = fake_model("ok")

        async def scenario(daemon):
            c = await Client.open(self.home)
            for text in ("first", "second"):
                c.send({"type": "say", "text": text})  # say without attach attaches implicitly
                await c.until("turn.end")
            c.close()

        self.run_with(factory, scenario)
        # messages[0] is the resident system prompt, messages[1] the local-time line (partition 3)
        roles = [(m["role"], m["content"].strip()) for m in factory.seen[-1][2:]]
        self.assertEqual(roles, [("user", "first"), ("assistant", "ok"), ("user", "second")])

    def test_daemon_survives_model_402(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hi"})
            first = await c.until("turn.end")
            daemon.step_factory = fake_model("back")  # credit returns; same process, same socket
            c.send({"type": "say", "text": "again"})
            second = await c.until("turn.end")
            c.close()
            return first, second

        first, second = self.run_with(blocked_model, scenario)
        self.assertEqual(first[-1]["kind"], "error")
        self.assertTrue(any(m["type"] == "error" and "free usage" in m["message"] for m in first))
        self.assertEqual(second[-1]["kind"], "answer")
        errors = [e for e in self.events() if e["type"] == "error"]
        self.assertEqual((len(errors), errors[0]["status"]), (1, 402))

    def test_unset_model_is_an_error_not_a_crash(self):
        """A fresh install has no model configured; the real step factory must say so."""
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hi"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(None, scenario)  # None = the default factory, no config.json
        self.assertEqual(seen[-1]["kind"], "error")
        self.assertTrue(any(m["type"] == "error" and "model" in m["message"].lower() for m in seen))

    def test_turn_gets_a_cancel_handle(self):
        """The seam partition 2 builds interrupt and steer on: every turn owns a TurnControl
        that reaches the step factory (and, in the real factory, the HTTP stream) - and is
        cleared again once the turn ends."""
        factory = fake_model("ok")

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hi"})
            await c.until("turn.end")
            c.close()
            return next(iter(daemon.sessions.values())).control

        held = self.run_with(factory, scenario)
        self.assertIsNone(held)  # control is None again between turns
        self.assertIsInstance(factory.cancels[-1], TurnControl)

    def test_exhausted_turn_still_records_what_was_said(self):
        def talkative(cfg, cancel=None, tools=None):
            def step(messages):
                if not tools:  # D30: the final tool-less request - the model must answer in words
                    yield ("text", "no luck.")
                    return
                yield ("text", "working ")
                yield ("tool_calls", [{"id": "c1", "name": "anything", "arguments": {}}])
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "go"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(talkative, scenario)
        self.assertEqual(seen[-1]["kind"], "exhausted")
        said = [e for e in self.events() if e["type"] == "assistant"]
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["text"], "working " * 8 + "no luck.")  # 8 steps plus D30's final words, all in history

    def test_attach_with_a_non_string_session_keeps_the_connection(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach", "session": {"x": 1}})
            attached = await c.recv()
            c.send({"type": "say", "text": "hi"})
            seen = await c.until("turn.end")
            c.close()
            return attached, seen

        attached, seen = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(attached["type"], "attached")
        self.assertEqual(seen[-1]["kind"], "answer")

    def test_daemon_unknown_line_keeps_connection(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            replies = []
            for bad in (b"not json\n", b'{"type": "dance"}\n', b'["a list"]\n', b'{"type": "say", "text": " "}\n'):
                c.send(bad)
                replies.append(await c.recv())
            c.send({"type": "say", "text": "still here?"})
            seen = await c.until("turn.end")
            c.close()
            return replies, seen

        replies, seen = self.run_with(fake_model("yes"), scenario)
        self.assertEqual([r["type"] for r in replies], ["error"] * 4)
        self.assertEqual(seen[-1]["kind"], "answer")

    def test_socket_perms(self):
        async def scenario(daemon):
            return stat.S_IMODE(os.stat(self.home / SOCKET).st_mode)

        self.assertEqual(self.run_with(fake_model(), scenario), 0o600)

    def test_second_daemon_refuses_a_live_socket(self):
        async def scenario(daemon):
            second = Daemon(self.home, step_factory=fake_model())
            with self.assertRaises(RuntimeError):
                await second.start()
            second.state.close()

        self.run_with(fake_model(), scenario)

    def test_daemon_resumes_after_state_deleted(self):
        async def talk(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = await c.recv()
            c.send({"type": "say", "text": "remember the word plum"})
            await c.until("turn.end")
            c.close()
            return attached

        first = self.run_with(fake_model("noted"), talk)
        for leftover in self.home.glob("state.db*"):
            leftover.unlink()  # the derived state is gone; the ledger is all that is left

        factory = fake_model("plum")
        second = self.run_with(factory, talk)
        self.assertTrue(second["resumed"])
        self.assertEqual(second["session"], first["session"])
        self.assertEqual(second["turns"], 1)
        history = [m["content"].strip() for m in factory.seen[-1][2:]]
        self.assertEqual(history, ["remember the word plum", "noted", "remember the word plum"])

    def test_daemon_gate_survives_restart(self):
        note_path = str(self.home / "note.txt")

        def gate_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": "hi"}}])
            return step

        def answer_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("text", "done")
            return step

        async def gate_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "please write a note"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(gate_model, gate_scenario)  # this daemon instance is now closed
        self.assertEqual(seen[-1]["kind"], "gated")
        gate_msgs = [m for m in seen if m["type"] == "gate"]
        self.assertEqual(len(gate_msgs), 1)
        self.assertEqual(gate_msgs[0]["tool"], "write_file")
        approval_id = gate_msgs[0]["id"]

        # `pathfinder approvals` reads state.db directly - no daemon needs to be running
        state = State(self.home / "state.db")
        pending = state.pending()
        state.close()
        self.assertEqual([p["id"] for p in pending], [approval_id])
        self.assertEqual(pending[0]["tool"], "write_file")

        async def approve_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": approval_id})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen2 = self.run_with(answer_model, approve_scenario)  # a brand new Daemon, same home
        self.assertEqual(seen2[-1]["kind"], "answer")
        self.assertEqual(Path(note_path).read_text(), "hi")

        types = [e["type"] for e in self.events()]
        for needed in ("approval_pending", "approval", "tool_call", "tool_result"):
            self.assertIn(needed, types)

    def test_daemon_deny_resumes_with_denial(self):
        note_path = str(self.home / "note-denied.txt")

        def gate_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": "hi"}}])
            return step

        def ack_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("text", "ok, not writing it")
            return step

        async def gate_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "write a note"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(gate_model, gate_scenario)
        approval_id = [m for m in seen if m["type"] == "gate"][0]["id"]

        async def deny_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "deny", "id": approval_id})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen2 = self.run_with(ack_model, deny_scenario)
        self.assertEqual(seen2[-1]["kind"], "answer")
        self.assertFalse(Path(note_path).exists())

        approvals = [e for e in self.events() if e["type"] == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["decision"], "denied")

    def test_daemon_say_during_turn_steers(self):
        proceed = threading.Event()

        def slow_handler(control=None):
            proceed.wait(2)
            return {"slept": True}

        tools_mod.TOOLS["slow_tool"] = ToolSpec(
            name="slow_tool", description="", params={}, required=[], handler=slow_handler)
        self.addCleanup(tools_mod.TOOLS.pop, "slow_tool", None)

        seen_messages = []

        def factory(cfg, control=None, tools=None):
            calls = {"n": 0}

            def step(messages):
                seen_messages.append([dict(m) for m in messages])
                n = calls["n"]
                calls["n"] += 1
                if n == 0:
                    yield ("tool_calls", [{"id": "c1", "name": "slow_tool", "arguments": {}}])
                else:
                    yield ("text", "done")
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "go"})
            await c.until("turn.start")

            s = next(iter(daemon.sessions.values()))
            for _ in range(200):
                if s.control is not None and s.control.in_tool:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(s.control.in_tool)

            c.send({"type": "say", "text": "steer now"})
            for _ in range(200):
                if s.control.steers:
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(s.control.steer_cut.is_set())  # queued mid-tool-call: not a cut

            proceed.set()
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(factory, scenario)
        self.assertEqual(seen[-1]["kind"], "answer")
        self.assertEqual(len(seen_messages), 2)  # no second look: the turn called a tool before the steer (D51)
        steer_msgs = [m for m in seen_messages[1] if m.get("role") == "user" and m.get("content") == "steer now"]
        self.assertEqual(len(steer_msgs), 1)
        steer_events = [e for e in self.events() if e["type"] == "user" and e.get("steer")]
        self.assertEqual(len(steer_events), 1)
        self.assertEqual(steer_events[0]["text"], "steer now")

    def test_daemon_interrupt_kills_tool(self):
        def blocking_handler(control=None):
            if control is not None:
                control.interrupt.wait(2)
            return {"stopped": True}

        tools_mod.TOOLS["blocking_tool"] = ToolSpec(
            name="blocking_tool", description="", params={}, required=[], handler=blocking_handler)
        self.addCleanup(tools_mod.TOOLS.pop, "blocking_tool", None)

        def factory(cfg, control=None, tools=None):
            def step(messages):
                if control is not None and control.interrupt.is_set():
                    yield ("cancelled", None)
                    return
                if not any(m.get("role") == "tool" for m in messages):
                    yield ("tool_calls", [{"id": "c1", "name": "blocking_tool", "arguments": {}}])
                else:
                    yield ("text", "back")
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "block"})
            await c.until("turn.start")

            s = next(iter(daemon.sessions.values()))
            for _ in range(200):
                if s.control is not None and s.control.in_tool:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(s.control.in_tool)

            c.send({"type": "interrupt"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        start = time.monotonic()
        seen = self.run_with(factory, scenario)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0)
        self.assertEqual(seen[-1]["kind"], "cancelled")
        self.assertIn("interrupted", [e["type"] for e in self.events()])

    def test_reindex_restores_pending_approval(self):
        note_path = str(self.home / "note-reindex.txt")

        def gate_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": "hi"}}])
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "write a note"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(gate_model, scenario)
        approval_id = [m for m in seen if m["type"] == "gate"][0]["id"]

        for leftover in self.home.glob("state.db*"):
            leftover.unlink()  # the derived state is gone; the ledger is all that is left

        state = State(self.home / "state.db")
        state.rebuild(Ledger(self.home))
        pending = state.pending()
        state.close()
        self.assertEqual([p["id"] for p in pending], [approval_id])


class ReviewRegressionsTest(DaemonHarness):
    """What the partition 2 review reproduced (HISTORY.md): a model-chosen call id is not a key."""

    def test_two_gates_with_the_same_call_id_are_both_resolvable(self):
        paths = [str(self.home / "a.txt"), str(self.home / "b.txt")]

        def gate_model(cfg, control=None, tools=None):
            def step(messages):
                if any(m.get("role") == "tool" for m in messages):
                    yield ("text", "done")
                    return
                n = sum(1 for m in messages if m.get("role") == "user") - 1
                yield ("text", "I will write it. ")  # said before the gate: must reach the ledger
                yield ("tool_calls", [{"id": "call_0", "name": "write_file",
                                        "arguments": {"path": paths[n], "content": str(n)}}])
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            ids = []
            for text in ("write a", "write b"):
                c.send({"type": "say", "text": text})
                seen = await c.until("turn.end")
                ids.append([m for m in seen if m["type"] == "gate"][0]["id"])
            self.assertNotEqual(ids[0], ids[1])
            self.assertEqual(len(daemon.state.pending()), 2)
            c.send({"type": "approve", "id": ids[1]})  # the one the user is looking at
            await c.until("turn.end")
            c.send({"type": "approve", "id": ids[0]})
            await c.until("turn.end")
            c.close()

        self.run_with(gate_model, scenario)
        self.assertEqual(Path(paths[1]).read_text(), "1")
        self.assertEqual(Path(paths[0]).read_text(), "0")
        state = State(self.home / "state.db")
        history = state.history(state.latest_session(9999))
        state.close()
        self.assertIn({"role": "assistant", "content": "I will write it. "}, history)
        self.assertEqual(sum(1 for e in self.events() if e["type"] == "tool_call"), 2)  # one write per approval

    def test_steer_after_the_last_chunk_gets_its_own_turn(self):
        def late_steer_model(cfg, control=None, tools=None):
            def step(messages):
                if messages[0]["content"] == VERIFIER_PROMPT:
                    return  # the second look: no verdict, the draft stands
                late_steer_model.calls += 1
                late_steer_model.last = messages[-1]
                yield ("text", "answer ")
                if late_steer_model.calls == 1:
                    control.steers.append("and in Korean")  # lands after the drain, before the turn ends
            return step
        late_steer_model.calls = 0

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hello"})
            first = await c.until("turn.end")
            second = await c.until("turn.end")  # the continuation turn, with no new `say`
            c.close()
            return first, second

        first, second = self.run_with(late_steer_model, scenario)
        self.assertEqual((first[-1]["kind"], second[-1]["kind"]), ("answer", "answer"))
        users = [e for e in self.events() if e["type"] == "user"]
        self.assertEqual([u["text"] for u in users], ["hello"])  # the fake put the steer in directly
        self.assertEqual(sum(1 for e in self.events() if e["type"] == "turn_start"), 2)
        self.assertEqual(late_steer_model.last, {"role": "user", "content": "and in Korean"})


FLAGGED = json.dumps({"unsupported_state": True, "unsupported_done": False, "empty_promise": False,
                      "false_incapability": False, "why": "no list_reminders result"})
PASSED = FLAGGED.replace("true", "false")


def scripted_model(verdict, draft="Your list is empty.", rerun="Checked: it holds two."):
    """Answers `draft` first and `rerun` after; the second look answers `verdict`. A user message,
    or `rerun`, names another ending: "error" breaks mid-stream, "stop" is interrupted mid-stream,
    "loop" calls a tool until the budget runs out ("loop-error": then D30's final step breaks
    mid-stream), "write" stops at the gate."""
    def factory(cfg, control=None, tools=None):
        def step(messages):
            if messages[0]["content"] == VERIFIER_PROMPT:
                yield ("text", verdict)
                return
            said = [m["content"] for m in messages if m["role"] == "user"][-1]
            if said not in ("error", "stop", "loop", "loop-error", "write"):
                factory.answers += 1
                said = draft if factory.answers == 1 else rerun
            if said == "error":
                yield ("text", "half an ans")
                raise LLMError(500, "upstream went away")
            if said == "stop":
                yield ("text", "partial ")
                control.interrupt.set()
                yield ("cancelled", None)
            elif said in ("loop", "loop-error"):
                yield ("text", "working " if tools else "no luck.")
                if not tools and said == "loop-error":
                    raise LLMError(500, "upstream went away")
                if tools:
                    yield ("tool_calls", [{"id": "c1", "name": "anything", "arguments": {}}])
            elif said == "write":
                yield ("text", "writing it. ")
                yield ("tool_calls", [{"id": "c1", "name": "write_file", "arguments": {"path": "n.txt", "content": "x"}}])
            else:
                yield ("text", said)
        return step
    factory.answers = 0
    return factory


class SecondLookDaemonTest(DaemonHarness):
    """D51 in the daemon: the withdrawn draft is recorded as the owner saw it, marked, and kept
    out of history; turn_end carries the check."""

    def _say(self, factory, *texts):
        async def scenario(daemon):
            c = await Client.open(self.home)
            turns = []
            for text in texts:
                c.send({"type": "say", "text": text})
                turns.append(await c.until("turn.end"))
            c.close()
            return turns, daemon.state.history(next(iter(daemon.sessions)))
        return self.run_with(factory, scenario)

    def test_retract_records_withdrawn_assistant_and_sends_line(self):
        (seen,), _ = self._say(scripted_model(FLAGGED), "any reminders?")
        self.assertEqual([m["type"] for m in seen], ["attached", "turn.start", "delta", "retract", "delta", "turn.end"])
        self.assertEqual(seen[3], {"type": "retract", "what": FLAG_WORDS["unsupported_state"],
                                   "why": "no list_reminders result"})
        said = [(e["text"], e["final"], e.get("withdrawn")) for e in self.events() if e["type"] == "assistant"]
        self.assertEqual(said, [("Your list is empty.", False, {"flags": ["unsupported_state"],
                                                                 "why": "no list_reminders result"}),
                                ("Checked: it holds two.", True, None)])

    def test_withdrawn_draft_marked_with_reason_and_out_of_history(self):
        _, history = self._say(scripted_model(FLAGGED), "any reminders?")
        withdrawn = [e for e in self.events() if e["type"] == "assistant" and not e["final"]]
        self.assertEqual([(e["text"], e["withdrawn"]["why"]) for e in withdrawn],
                         [("Your list is empty.", "no list_reminders result")])
        self.assertNotIn("Your list is empty.", [m["content"] for m in history])

    def test_history_holds_only_the_rerun(self):
        _, history = self._say(scripted_model(FLAGGED), "any reminders?")
        self.assertEqual(history, [{"role": "user", "content": "any reminders?"},
                                   {"role": "assistant", "content": "Checked: it holds two."}])

    def test_turn_end_carries_check(self):
        checks = []
        for verdict in (PASSED, FLAGGED, "I think the draft is fine."):
            self._say(scripted_model(verdict), "hi")
            checks.append([e.get("check") for e in self.events() if e["type"] == "turn_end"][-1])
        self._say(scripted_model(PASSED), "loop")
        checks.append(self.events()[-1]["check"])  # an answer after tools is never checked
        for ending in ("write", "error"):  # a flagged draft whose re-run gates or breaks
            self._say(scripted_model(FLAGGED, rerun=ending), "any reminders?")
            end = [e for e in self.events() if e["type"] == "turn_end"][-1]
            checks.append((end["kind"], end["check"]))
        self.assertEqual(checks, ["passed", "flagged", "failed", None, ("gated", "flagged"), ("error", "flagged")])

    def test_steer_during_the_look_waits_for_it(self):
        """The owner types during the 1-3 s look: the steer is queued, not a cut, so the verdict
        stands; it is answered in the re-run (flagged) or in a turn of its own (passed)."""
        for verdict, turns in ((PASSED, 2), (FLAGGED, 1)):
            release = threading.Event()

            def factory(cfg, control=None, tools=None):
                def step(messages):
                    if messages[0]["content"] == VERIFIER_PROMPT:
                        factory.looks += 1
                        if factory.looks == 1:
                            release.wait(5)
                        # the real adapter's cancel check: a cut here would fail the look
                        answer = verdict if factory.looks == 1 else PASSED
                        yield ("cancelled", None) if control.is_set() else ("text", answer)
                        return
                    factory.answers += 1
                    yield ("text", f"answer {factory.answers}")
                return step
            factory.looks = factory.answers = 0

            async def scenario(daemon):
                c = await Client.open(self.home)
                c.send({"type": "say", "text": "any reminders?"})
                while not factory.looks:
                    await asyncio.sleep(0.01)
                s = next(iter(daemon.sessions.values()))
                c.send({"type": "say", "text": "and in Korean"})
                while not s.control.steers:
                    await asyncio.sleep(0.01)
                release.set()
                for _ in range(turns):
                    await c.until("turn.end")
                c.close()

            start = len(self.events())
            self.run_with(factory, scenario)
            events = [e for e in self.events()[start:] if e["type"] != "session_start"]
            self.assertEqual([(e["type"], e.get("steer"), e.get("final"), e.get("check")) for e in events], {
                PASSED: [("user", None, None, None), ("turn_start", None, None, None), ("user", True, None, None),
                         ("assistant", None, True, None), ("turn_end", None, None, "passed"),
                         ("turn_start", None, None, None), ("assistant", None, True, None),
                         ("turn_end", None, None, "passed")],
                FLAGGED: [("user", None, None, None), ("turn_start", None, None, None), ("user", True, None, None),
                          ("assistant", None, False, None), ("assistant", None, True, None),
                          ("turn_end", None, None, "flagged")]}[verdict])

    def test_every_streamed_delta_is_in_the_ledger(self):
        """Invariant 2: whatever the ending, the turn's assistant events hold every delta sent."""
        endings = ("hi", "loop", "loop-error", "stop", "error", "any reminders?", "write")
        turns, _ = self._say(scripted_model(FLAGGED), *endings)
        self.assertEqual([seen[-1]["kind"] for seen in turns],
                         ["answer", "exhausted", "error", "cancelled", "error", "answer", "gated"])
        for seen in turns:
            turn = next(m for m in seen if m["type"] == "turn.start")["turn"]
            streamed = "".join(m["text"] for m in seen if m["type"] == "delta")
            recorded = "".join(e["text"] for e in self.events() if e["type"] == "assistant" and e["turn"] == turn)
            self.assertTrue(streamed)
            self.assertEqual(streamed, recorded)


class RealStepFactoryTest(unittest.TestCase):
    def test_default_step_factory_sends_tool_schemas(self):
        """Every daemon test runs a fake model, so nothing above would notice if the real
        factory forgot the tools - which is exactly what the first live run found."""
        from pathfinder import daemon
        from pathfinder.llm import openai_compat
        seen = {}

        def fake_stream(endpoint, messages, tools=None, cancel=None, timeout=120):
            seen["tools"], seen["cancel"] = tools, cancel
            return iter([("text", "ok")])

        original = openai_compat.stream
        openai_compat.stream = fake_stream
        self.addCleanup(setattr, openai_compat, "stream", original)
        cfg = {"model": "p:m", "providers": {"p": {"base_url": "http://x", "api_key_env": ""}}}
        control = TurnControl()
        user_tools = tools_mod.for_origin("user")
        list(daemon.default_step_factory(cfg, control, user_tools)([]))
        self.assertEqual({t["function"]["name"] for t in seen["tools"]}, set(user_tools))
        self.assertIs(seen["cancel"], control)

    def test_job_step_factory_uses_worker_model_and_job_tools(self):
        """The job step resolves `worker_model` over `model`, and shows the model only the
        job registry (no `start_job` - a worker cannot make a worker)."""
        from pathfinder import jobs
        from pathfinder.llm import openai_compat
        seen = {}

        def fake_stream(endpoint, messages, tools=None, cancel=None, timeout=120):
            seen["model"], seen["tools"] = endpoint.model, tools
            return iter([("text", "ok")])

        original = openai_compat.stream
        openai_compat.stream = fake_stream
        self.addCleanup(setattr, openai_compat, "stream", original)
        cfg = {"model": "p:big", "worker_model": "p:small",
              "providers": {"p": {"base_url": "http://x", "api_key_env": ""}}}
        job_tools = tools_mod.for_origin("job")
        list(jobs.default_job_step_factory(cfg, None, job_tools)([]))
        self.assertEqual(seen["model"], "small")
        self.assertEqual({t["function"]["name"] for t in seen["tools"]}, set(job_tools))
        self.assertNotIn("start_job", {t["function"]["name"] for t in seen["tools"]})


def _never_quiet(daemon):
    """Real wall-clock quiet hours (default 23-8) would make a signal-turn test flaky
    depending on when it happens to run - pin it open for the duration of the test."""
    daemon.rules = dataclasses.replace(daemon.rules, quiet_hours="0-0")


class ResidentPromptTest(DaemonHarness):
    def test_resident_prompt_frozen(self):
        """The time line is its own system message; the resident prompt text is byte-identical
        across turns (D20 - it must stay cache-stable)."""
        factory = fake_model("ok")

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "one"})
            await c.until("turn.end")
            c.send({"type": "say", "text": "two"})
            await c.until("turn.end")
            c.close()

        self.run_with(factory, scenario)
        first_call, second_call = factory.seen[0], factory.seen[-1]
        self.assertEqual(first_call[0], second_call[0])  # resident system prompt, byte-identical
        self.assertEqual((first_call[0]["role"], first_call[1]["role"]), ("system", "system"))
        self.assertIn("Local time now:", first_call[1]["content"])
        self.assertNotEqual(first_call[0]["content"], first_call[1]["content"])


class SignalTurnTest(DaemonHarness):
    def _wait_for_delivery(self):
        for _ in range(300):
            if any(e["type"] == "delivery" for e in Ledger(self.home).read()):
                return
            time.sleep(0.01)

    def test_signal_turn_origin_is_signal(self):
        """A write tool call in a signal turn gates (never runs); the envelope never reaches
        state.history, but the delivered text does."""
        note_path = str(self.home / "signal-note.txt")

        def gate_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": "y"}}])
            return step

        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="sig-origin",
                                      summary="Scheduled note: ping",
                                      fields={"id": "s1", "text": "ping", "at": "2026-09-22 09:00"},
                                      trust="user"))
            await asyncio.to_thread(self._wait_for_delivery)
            return list(Ledger(self.home).read())

        events = self.run_with(gate_model, scenario)
        self.assertEqual([e for e in events if e["type"] == "tool_call"], [])  # the write never ran
        self.assertFalse(Path(note_path).exists())
        delivery = next(e for e in events if e["type"] == "delivery")
        state = State(self.home / "state.db")
        history = state.history(delivery["session"])
        state.close()
        self.assertTrue(any(m["role"] == "assistant" and "Scheduled note: ping" in m["content"] for m in history))
        self.assertFalse(any("[Signal" in m["content"] for m in history))  # the envelope is never history

    def test_signal_turn_survives_model_402(self):
        """LLMError (blocked model): the sense's summary alone is delivered."""
        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="sig-402",
                                      summary="Scheduled note: budget",
                                      fields={"id": "s2", "text": "budget", "at": "2026-09-22 09:00"},
                                      trust="user"))
            await asyncio.to_thread(self._wait_for_delivery)
            return list(Ledger(self.home).read())

        events = self.run_with(blocked_model, scenario)
        delivery = next(e for e in events if e["type"] == "delivery")
        state = State(self.home / "state.db")
        history = state.history(delivery["session"])
        state.close()
        self.assertEqual([m["content"] for m in history if m["role"] == "assistant"], ["Scheduled note: budget"])


class DeliverTest(DaemonHarness):
    def test_deliver_holds_while_turn_runs(self):
        """A notice enqueued while the session's turn is running is held; it is released (and
        arrives after `turn.end`) once the turn ends."""
        proceed = threading.Event()

        def slow_handler(control=None):
            proceed.wait(2)
            return {"done": True}

        tools_mod.TOOLS["slow_tool"] = ToolSpec(
            name="slow_tool", description="", params={}, required=[], handler=slow_handler)
        self.addCleanup(tools_mod.TOOLS.pop, "slow_tool", None)

        def factory(cfg, control=None, tools=None):
            calls = {"n": 0}

            def step(messages):
                n = calls["n"]
                calls["n"] += 1
                if n == 0:
                    yield ("tool_calls", [{"id": "c1", "name": "slow_tool", "arguments": {}}])
                else:
                    yield ("text", "done")
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "go"})
            await c.until("turn.start")
            s = next(iter(daemon.sessions.values()))
            for _ in range(200):
                if s.control is not None and s.control.in_tool:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(s.control.in_tool)

            daemon.outbox.enqueue(Notice("n1", s.id, "held text", "w", []))
            await asyncio.sleep(0.05)
            self.assertEqual(len(daemon.outbox.queue), 1)  # still held: the turn is running

            proceed.set()
            seen = await c.until("turn.end")
            notice = await c.recv()  # arrives after turn.end, not before
            c.close()
            return seen, notice

        seen, notice = self.run_with(factory, scenario)
        self.assertEqual(seen[-1]["type"], "turn.end")
        self.assertEqual(notice, {"type": "notice", "id": "n1", "text": "held text", "why": "w"})

    def test_deliver_presence_typing_holds(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = await c.recv()
            sid = attached["session"]
            c.send({"type": "presence", "idle_s": 1})
            await asyncio.sleep(0.05)
            daemon.outbox.enqueue(Notice("n2", sid, "typing text", "w", []))
            await asyncio.sleep(0.05)
            still_held = len(daemon.outbox.queue)
            c.send({"type": "presence", "idle_s": 999})  # stopped typing
            notice = await c.recv()
            c.close()
            return still_held, notice

        still_held, notice = self.run_with(fake_model(), scenario)
        self.assertEqual(still_held, 1)
        self.assertEqual(notice, {"type": "notice", "id": "n2", "text": "typing text", "why": "w"})

    def test_deliver_banner_when_no_client(self):
        async def scenario(daemon):
            calls = []

            def fake_banner(text):
                calls.append(text)
                return True

            daemon.outbox.banner = fake_banner
            daemon.record("session_start", session="no-client")
            daemon.outbox.enqueue(Notice("n3", "no-client", "banner text", "w", []))
            return calls

        calls = self.run_with(fake_model(), scenario)
        self.assertEqual(calls, ["banner text"])
        deliveries = [e for e in self.events() if e["type"] == "delivery"]
        self.assertEqual(deliveries[-1]["via"], "banner")

    def test_deliver_failed_banner_is_not_recorded_and_stays_queued(self):
        """Blocker 2: a failed banner must not be recorded as a delivery - the notice stays in
        the outbox and is retried on the next release()."""
        async def scenario(daemon):
            attempts = []

            def flaky_banner(text):
                attempts.append(text)
                return len(attempts) > 1  # fails once, then succeeds

            daemon.outbox.banner = flaky_banner
            daemon.record("session_start", session="no-client")
            daemon.outbox.enqueue(Notice("n4", "no-client", "안녕 (Korean)", "w", []))
            first_queue_len = len(daemon.outbox.queue)
            daemon.outbox.release()
            return attempts, first_queue_len, len(daemon.outbox.queue)

        attempts, first_queue_len, second_queue_len = self.run_with(fake_model(), scenario)
        self.assertEqual(len(attempts), 2)
        self.assertEqual((first_queue_len, second_queue_len), (1, 0))
        events = self.events()
        self.assertEqual(len([e for e in events if e["type"] == "delivery"]), 1)  # only the success

    def test_deliver_banner_korean_text_not_ascii_escaped(self):
        """Blocker 2: ensure_ascii=False, or osascript gets a literal \\uXXXX it cannot show."""
        from pathfinder import deliver as deliver_mod
        seen = {}

        def fake_run(argv, **kw):
            seen["script"] = argv[2]
            return type("R", (), {"returncode": 0})()

        with mock.patch("pathfinder.deliver.subprocess.run", side_effect=fake_run):
            ok = deliver_mod.banner("안녕")
        self.assertTrue(ok)
        self.assertIn("안녕", seen["script"])
        self.assertNotIn("\\u", seen["script"])

    def test_deliver_outbox_rebuilt_from_ledger(self):
        """A `decision speak` with no matching `delivery` is restored into a fresh daemon's
        outbox - a crash with a full outbox does not lose the pending notice."""
        async def make_pending(daemon):
            daemon.record("session_start", session="pending-session")
            daemon.record("decision", session="pending-session", origin="signal", signal_key="k-rebuild",
                          action="speak", why="always=-", notice_id="notice-1", text="rebuilt notice",
                          signal_keys=["k-rebuild"])

        self.run_with(fake_model(), make_pending)  # closes without notice-1 ever being delivered

        async def scenario(daemon2):
            return list(daemon2.outbox.queue)

        queue = self.run_with(fake_model(), scenario)
        self.assertEqual(len(queue), 1)
        self.assertEqual((queue[0].id, queue[0].text, queue[0].session),
                         ("notice-1", "rebuilt notice", "pending-session"))

    def test_speak_decision_survives_close_during_hold(self):
        """Should-fix 5: a rate-limit-held `speak` set must have its `decision speak` rows on
        disk BEFORE the hold - a held set otherwise lives only in a call_later timer, and
        closing during the hold would lose it (a signal event with no decision at all)."""
        async def make_held(daemon):
            _never_quiet(daemon)
            daemon.rules = dataclasses.replace(daemon.rules, min_interval_s=999, burst_limit=999)
            daemon.record("turn_start", session="S", turn="t0", origin="signal")  # "just spoke"
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="k-hold",
                                      summary="Scheduled note: A", fields={}, trust="user"))
            for _ in range(200):
                if any(e["type"] == "decision" and e["action"] == "speak" for e in Ledger(self.home).read()):
                    return
                await asyncio.sleep(0.01)
            self.fail("decision speak was never recorded before the hold")

        self.run_with(fake_model(), make_held)  # closes with the hold's call_later still pending

        async def scenario(daemon2):
            return list(daemon2.outbox.queue)

        queue = self.run_with(fake_model(), scenario)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].text, "Scheduled note: A")


class ReindexRestoresSignalsTest(DaemonHarness):
    def test_reindex_restores_seen_and_undelivered(self):
        async def make_events(daemon):
            _never_quiet(daemon)
            daemon.push_signal(Signal(sense="shell", kind="command_failed", key="k-seen",
                                      summary="s", fields={"exit": 1}, trust="self"))
            daemon.record("decision", session="s1", origin="signal", signal_key="k-pending",
                          action="speak", why="w", notice_id="notice-x", text="pending text",
                          signal_keys=["k-pending"])
            await asyncio.sleep(0.2)  # let the pushed signal's own triage settle

        self.run_with(fake_model(), make_events)

        for leftover in self.home.glob("state.db*"):
            leftover.unlink()  # the derived state is gone; the ledger is all that is left

        state = State(self.home / "state.db")
        state.rebuild(Ledger(self.home))
        self.assertTrue(state.seen("k-seen", since=0))
        self.assertTrue(any(u["id"] == "notice-x" for u in state.undelivered()))
        state.close()


class AttachSenseTest(DaemonHarness):
    def test_attach_cwd_pushes_context_signal(self):
        config.set(self.home, "senses", {"attach": {}})

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach", "cwd": "/Users/you/project-name"})
            await c.recv()  # "attached"
            c.close()

        self.run_with(fake_model(), scenario)
        signals = [e for e in self.events() if e["type"] == "signal"]
        self.assertEqual(len(signals), 1)
        self.assertEqual((signals[0]["sense"], signals[0]["kind"]), ("attach", "context"))
        self.assertEqual(signals[0]["fields"]["project"], "project-name")

    def test_attach_without_cwd_pushes_nothing(self):
        config.set(self.home, "senses", {"attach": {}})

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.recv()
            c.close()

        self.run_with(fake_model(), scenario)
        self.assertEqual([e for e in self.events() if e["type"] == "signal"], [])

    def test_repeated_attach_same_cwd_pushes_once(self):
        """Should-fix 4: a client reattaching every second (the same cwd, over and over) must
        not write a ledger line per attach."""
        config.set(self.home, "senses", {"attach": {}})

        async def scenario(daemon):
            c = await Client.open(self.home)
            attached = None
            for _ in range(5):
                c.send({"type": "attach", "session": attached, "cwd": "/Users/you/project-name"})
                attached = (await c.recv())["session"]
            c.close()

        self.run_with(fake_model(), scenario)
        self.assertEqual(len([e for e in self.events() if e["type"] == "signal"]), 1)

    def test_attach_cwd_change_pushes_again(self):
        config.set(self.home, "senses", {"attach": {}})

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach", "cwd": "/Users/you/project-a"})
            attached = (await c.recv())["session"]
            c.send({"type": "attach", "session": attached, "cwd": "/Users/you/project-b"})
            await c.recv()
            c.close()

        self.run_with(fake_model(), scenario)
        self.assertEqual(len([e for e in self.events() if e["type"] == "signal"]), 2)


class JudgmentCapTest(DaemonHarness):
    def test_judge_never_runs_over_cap(self):
        """`judgment_cap` is checked before `rules.judge` is ever called - a signal whose kind
        has no RULES.md section (ask_model) defers instead, and judge sees nothing."""
        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.rules = dataclasses.replace(daemon.rules, judgment_cap=0)
            with mock.patch("pathfinder.daemon.rules_mod.judge") as judge_mock:
                daemon.push_signal(Signal(sense="mail", kind="kind_with_no_rules_section",
                                          key="cap-test", summary="s", fields={}, trust="third_party"))
                for _ in range(200):
                    if any(e["type"] == "decision" for e in Ledger(self.home).read()):
                        break
                    await asyncio.sleep(0.01)
                self.assertFalse(judge_mock.called)
            return list(Ledger(self.home).read())

        events = self.run_with(fake_model(), scenario)
        decisions = [e for e in events if e["type"] == "decision"]
        self.assertTrue(any(d["action"] == "defer" and d["why"] == "judgment cap" for d in decisions))


class RateLimitDoesNotBlockLaneTest(DaemonHarness):
    def test_held_signal_does_not_delay_a_say(self):
        """Blocker 1: the rate-limit hold used to `await asyncio.sleep(wait)` inside the lane
        itself, so a `say` queued right behind a held signal waited out the whole hold. The
        fix re-queues the speak set with `call_later` and returns immediately."""
        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.rules = dataclasses.replace(daemon.rules, min_interval_s=3, burst_limit=999)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = await c.recv()
            sid = attached["session"]
            daemon.record("turn_start", session=sid, turn="t0", origin="signal")  # "just spoke"
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="k-hold",
                                      summary="A", fields={}, trust="user"))
            await asyncio.sleep(0.1)
            t0 = time.time()
            c.send({"type": "say", "text": "hi"})
            await c.until("turn.start")
            elapsed = time.time() - t0
            c.close()
            return elapsed

        elapsed = self.run_with(fake_model("ok"), scenario)
        self.assertLess(elapsed, 1.0)  # nowhere near min_interval_s=3


class DailyBudgetBundleTest(DaemonHarness):
    def test_daily_budget_split_within_one_bundle(self):
        """Should-fix 4: the budget is spent as the bundle is triaged, not only once the model
        turn ends and records its `decision speak` rows - one bundle of 5 with daily_budget=1
        must not speak all 5."""
        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.rules = dataclasses.replace(daemon.rules, daily_budget=1, min_interval_s=0, burst_limit=999)
            s = daemon._target_session()
            bundle = [Signal("schedule", "schedule_due", f"k{i}", f"summary {i}", {}, "third_party")
                     for i in range(5)]
            await daemon.run_signal_turn(s, bundle)
            await asyncio.sleep(0.2)
            return list(Ledger(self.home).read())

        events = self.run_with(fake_model("ok"), scenario)
        decisions = [e for e in events if e["type"] == "decision"]
        speaks = [d for d in decisions if d["action"] == "speak"]
        defers = [d for d in decisions if d["action"] == "defer" and d["why"] == "daily budget spent"]
        self.assertEqual(len(speaks), 1)
        self.assertEqual(len(defers), 4)


class PushDoorValidationTest(DaemonHarness):
    def test_signal_build_exception_is_an_error_line_not_a_crash(self):
        """Should-fix 6: `sense.module.build(fields)` is the only untrusted local input: a bad
        field must produce an `error` line, not kill the connection."""
        config.set(self.home, "senses", {"shell": {}})

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "signal", "sense": "shell", "fields": {"cmd": 123, "exit": 1}})  # cmd not a string
            reply = await c.recv()
            c.send({"type": "say", "text": "still alive?"})
            seen = await c.until("turn.end")
            c.close()
            return reply, seen

        reply, seen = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(reply["type"], "error")
        self.assertEqual(seen[-1]["kind"], "answer")

    def test_close_does_not_wait_for_a_client_that_only_pushed(self):
        """F5: a push sense's connection (a hook's `signal`, never `attach`) is in no session - close()
        closes it too, or wait_closed() hangs a `service stop`."""
        config.set(self.home, "senses", {"shell": {}})

        async def main():
            daemon = Daemon(self.home, step_factory=fake_model("ok"))
            await daemon.start()
            c = await Client.open(self.home)
            c.send({"type": "signal", "sense": "shell", "fields": {"cmd": "ls", "exit": 0, "seconds": 1, "cwd": "/x"}})
            await asyncio.sleep(0.2)  # the daemon has read it; the client stays connected
            await asyncio.wait_for(daemon.close(), 10)
        asyncio.run(main())

    def test_signal_fields_over_4kb_refused(self):
        config.set(self.home, "senses", {"shell": {}})

        async def scenario(daemon):
            c = await Client.open(self.home)
            big = "A" * 5000
            c.send({"type": "signal", "sense": "shell",
                    "fields": {"cmd": big, "exit": 1, "seconds": 1, "cwd": "/x"}})
            reply = await c.recv()
            c.close()
            return reply

        reply = self.run_with(fake_model(), scenario)
        self.assertEqual(reply["type"], "error")
        self.assertEqual([e for e in self.events() if e["type"] == "signal"], [])

    def test_socket_refuses_signal_jobs(self):
        """Not a push sense: `jobs.Manager` pushes `job_done`/`job_failed` directly, so a
        `signal jobs ...` line from outside is refused like any unknown or non-push sense."""
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "signal", "sense": "jobs", "fields": {"goal": "x", "summary": "y"}})
            reply = await c.recv()
            c.close()
            return reply

        reply = self.run_with(fake_model(), scenario)
        self.assertEqual(reply["type"], "error")


class ProtocolValidationTest(DaemonHarness):
    def test_presence_rejects_out_of_range_idle_s(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.recv()
            replies = []
            for bad in (-1, 86401, "5", True):
                c.send({"type": "presence", "idle_s": bad})
                replies.append(await c.recv())
            c.close()
            return replies

        replies = self.run_with(fake_model(), scenario)
        self.assertTrue(all(r["type"] == "error" for r in replies))

    def test_react_unknown_delivery_id_is_an_error(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "react", "vote": "up", "id": "no-such-id"})
            reply = await c.recv()
            c.close()
            return reply

        reply = self.run_with(fake_model(), scenario)
        self.assertEqual(reply["type"], "error")
        self.assertEqual([e for e in self.events() if e["type"] == "reaction"], [])


class SignalTurnControlTest(DaemonHarness):
    def test_signal_turn_sets_is_running(self):
        """Note 13: without s.control set, is_running(s) is False during a signal turn, so
        interrupt wrongly says "no turn is running" and a presence line can release a notice
        mid-turn."""
        proceed = threading.Event()

        def slow_model(cfg, control=None, tools=None):
            def step(messages):
                proceed.wait(2)
                yield ("text", "ok")
            return step

        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="k-run",
                                      summary="A", fields={}, trust="user"))
            s = None
            for _ in range(200):
                s = next(iter(daemon.sessions.values()), None)
                if s is not None and s.control is not None:
                    break
                await asyncio.sleep(0.01)
            is_running = s is not None and s.control is not None
            proceed.set()
            for _ in range(200):
                if any(e["type"] == "delivery" for e in Ledger(self.home).read()):
                    break
                await asyncio.sleep(0.01)
            return is_running

        is_running = self.run_with(slow_model, scenario)
        self.assertTrue(is_running)

    def test_say_during_signal_turn_starts_its_own_turn(self):
        """Partition-7 defect: D17 only covers the user's own turn - a `say` that arrives while
        a SIGNAL turn is running (the assistant speaking on its own initiative, nothing the user
        said yet) must not steer it; it queues as its own turn, run once the signal turn ends."""
        proceed = threading.Event()

        def slow_model(cfg, control=None, tools=None):
            def step(messages):
                proceed.wait(2)
                yield ("text", "ok")
            return step

        async def scenario(daemon):
            _never_quiet(daemon)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.recv()

            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="k-say-signal",
                                      summary="A", fields={}, trust="user"))
            s = None
            for _ in range(200):
                s = next(iter(daemon.sessions.values()), None)
                if s is not None and s.control is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(s.origin, "signal")  # the running turn is the signal's, not the user's

            c.send({"type": "say", "text": "hello"})
            await asyncio.sleep(0.1)  # a chance to wrongly steer the signal turn, if it would
            self.assertEqual(s.origin, "signal")  # unchanged: "hello" only queued, not steered

            proceed.set()
            seen = await c.until("turn.end")  # the queued say's own turn - the signal turn
                                              # never talks to the client directly, only via `notice`
            c.close()
            return seen

        seen = self.run_with(slow_model, scenario)
        self.assertIn("turn.start", [m["type"] for m in seen])
        self.assertEqual(seen[-1]["kind"], "answer")
        self.assertEqual([e for e in self.events() if e["type"] == "user" and e.get("steer")], [])
        says = [e for e in self.events() if e["type"] == "user" and e.get("text") == "hello"]
        self.assertEqual(len(says), 1)


class MemoryDaemonTest(DaemonHarness):
    def test_lane_gated_edit_memory_resumes_the_turn(self):
        """grants {"edit_memory": "ask"} gates a user turn's own edit_memory call through the
        broker (a LANE row: non-empty messages, the model's own call id) - approving it must
        resume the turn and write through the handler, not go to memory_mgr.decide (B3)."""
        config.set(self.home, "grants", {"edit_memory": "ask"})

        def gate_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "edit_memory", "arguments": {
                    "target": "wiki/Test.md", "op": "add", "old": "", "new": "# Test\nline one"}}])
            return step

        def answer_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("text", "done")
            return step

        async def gate_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "please remember this"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(gate_model, gate_scenario)
        self.assertEqual(seen[-1]["kind"], "gated")
        gate_msgs = [m for m in seen if m["type"] == "gate"]
        self.assertEqual(len(gate_msgs), 1)
        self.assertEqual(gate_msgs[0]["tool"], "edit_memory")
        approval_id = gate_msgs[0]["id"]

        state = State(self.home / "state.db")
        row = state.approval(approval_id)
        state.close()
        self.assertTrue(row["messages"])  # a LANE row, not a Manager proposal (messages=[])

        async def approve_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": approval_id})
            seen2 = await c.until("turn.end")
            c.close()
            return seen2

        seen2 = self.run_with(answer_model, approve_scenario)
        self.assertEqual(seen2[-1]["kind"], "answer")
        self.assertIn("line one", (self.home / "wiki" / "Test.md").read_text(encoding="utf-8"))
        applies = [e for e in self.events() if e["type"] == "memory_apply"]
        self.assertEqual(len(applies), 1)
        self.assertIsNone(applies[0].get("approval"))  # written through the handler, not the approval box

    def test_memory_approval_routes_to_manager(self):
        async def scenario(daemon):
            proposal = {"target": "USER.md", "op": "add", "old": "", "new": "Likes tea.", "evidence": [1]}
            daemon.record("approval_pending", session="s1", origin="memory", id="appr1",
                          call_id="appr1", tool="edit_memory", args=proposal, messages=[])
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": "appr1"})
            reply = await c.recv()
            c.close()
            return reply, (self.home / "USER.md").read_text(encoding="utf-8"), "s1" in daemon.sessions

        reply, after, has_session = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(reply, {"type": "turn.end", "turn": "", "kind": "resumed"})
        self.assertIn("Likes tea.", after)
        self.assertFalse(has_session)  # no lane resume - the memory box, not a session turn
        applies = [e for e in self.events() if e["type"] == "memory_apply"]
        self.assertEqual(len(applies), 1)
        self.assertEqual(applies[0]["approval"], "appr1")

    def test_rules_reload_on_approved_row(self):
        old_row = ("| `minutes_over` | `5` | `speak` | a command that ran more than five minutes "
                  "is worth a word when it's done |")
        new_row = old_row.replace("`speak`", "`silence`")

        async def scenario(daemon):
            proposal = {"target": "RULES.md", "op": "replace", "old": old_row, "new": new_row, "evidence": [1]}
            daemon.record("approval_pending", session="s1", origin="memory", id="appr2",
                          call_id="appr2", tool="edit_memory", args=proposal, messages=[])
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": "appr2"})
            await c.recv()
            c.close()
            _never_quiet(daemon)  # after the reload: reload_rules() would otherwise wipe this override
            daemon.push_signal(Signal(sense="shell", kind="command_finished", key="k-reload",
                                      summary="s", fields={"seconds": 900}, trust="self"))
            for _ in range(200):
                if any(e["type"] == "decision" and e.get("signal_key") == "k-reload"
                      for e in Ledger(self.home).read()):
                    break
                await asyncio.sleep(0.01)
            return list(Ledger(self.home).read())

        events = self.run_with(fake_model("ok"), scenario)
        decision = next(e for e in events if e["type"] == "decision" and e.get("signal_key") == "k-reload")
        self.assertEqual(decision["action"], "silence")  # the reloaded row, not the shipped "speak"

    def test_rules_reload_on_hand_edit(self):
        config.set(self.home, "senses", {"shell": {}})  # command_finished becomes a validated kind
        old_row = ("| `minutes_over` | `5` | `speak` | a command that ran more than five minutes "
                  "is worth a word when it's done |")
        good_row = old_row.replace("`speak`", "`silence`")

        async def scenario(daemon):
            rules_path = self.home / "RULES.md"
            # quiet_hours in the frontmatter itself (not `_never_quiet`, which `reload_rules`
            # would wipe on every one of this test's own hand edits): 0-0 opens the whole day.
            text = (rules_path.read_text(encoding="utf-8")
                   .replace("quiet_hours: 23-8", "quiet_hours: 0-0").replace(old_row, good_row))
            rules_path.write_text(text, encoding="utf-8")
            os.utime(rules_path, (time.time() + 5, time.time() + 5))
            daemon.push_signal(Signal(sense="shell", kind="command_finished", key="k-hand",
                                      summary="s", fields={"seconds": 900}, trust="self"))
            for _ in range(200):
                if any(e["type"] == "decision" and e.get("signal_key") == "k-hand"
                      for e in Ledger(self.home).read()):
                    break
                await asyncio.sleep(0.01)

            broken = text.replace("`minutes_over`", "`gibberish_condition`")
            rules_path.write_text(broken, encoding="utf-8")
            os.utime(rules_path, (time.time() + 10, time.time() + 10))
            daemon.push_signal(Signal(sense="shell", kind="command_finished", key="k-hand2",
                                      summary="s", fields={"seconds": 900}, trust="self"))
            for _ in range(200):
                if any(e["type"] == "decision" and e.get("signal_key") == "k-hand2"
                      for e in Ledger(self.home).read()):
                    break
                await asyncio.sleep(0.01)
            return list(Ledger(self.home).read())

        events = self.run_with(fake_model("ok"), scenario)
        d1 = next(e for e in events if e["type"] == "decision" and e.get("signal_key") == "k-hand")
        self.assertEqual(d1["action"], "silence")  # the hand edit took effect
        d2 = next(e for e in events if e["type"] == "decision" and e.get("signal_key") == "k-hand2")
        self.assertEqual(d2["action"], "silence")  # the broken edit was refused - old rules kept
        self.assertTrue(any(e["type"] == "error" and "RULES.md" in e.get("message", "") for e in events))

    def test_turn_context_carries_user_md_and_map(self):
        async def scenario(daemon):
            user_path = self.home / "USER.md"
            user_path.write_text(user_path.read_text(encoding="utf-8") + "\nLikes tea.\n", encoding="utf-8")
            wiki_dir = self.home / "wiki"
            wiki_dir.mkdir(exist_ok=True)
            (wiki_dir / "coffee.md").write_text("# Coffee\nGood in the morning.\n", encoding="utf-8")
            daemon.memory_mgr.index("wiki/coffee.md")

            daemon.step_factory = fake_model("ok")
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hi"})
            await c.until("turn.end")
            c.close()
            return daemon.step_factory.seen[-1], daemon.system_prompt

        messages, resident = self.run_with(fake_model("placeholder"), scenario)
        self.assertEqual(messages[0], {"role": "system", "content": resident})
        self.assertEqual(messages[1]["role"], "system")
        self.assertIn("Likes tea.", messages[1]["content"])
        self.assertIn("Coffee", messages[1]["content"])

    def test_memory_run_forces_a_pass(self):
        def pass_factory(cfg, control, tools):
            def step(messages):
                yield ("text", "nothing new")
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hello"})
            await c.until("turn.end")
            c.send({"type": "memory.run"})
            reply = await c.recv()
            for _ in range(200):
                if any(e["type"] == "memory_pass" for e in Ledger(self.home).read()):
                    break
                await asyncio.sleep(0.01)
            c.close()
            return reply, list(Ledger(self.home).read())

        reply, events = self.run_with(fake_model("ok"), scenario, pass_factory=pass_factory)
        self.assertEqual(reply["type"], "memory.run")
        self.assertIn("result", reply)
        self.assertTrue(any(e["type"] == "memory_pass" for e in events))

    def test_socket_refuses_signal_memory(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "signal", "sense": "memory", "fields": {}})
            reply = await c.recv()
            c.close()
            return reply

        reply = self.run_with(fake_model(), scenario)
        self.assertEqual(reply["type"], "error")


class JobDaemonIntegrationTest(DaemonHarness):
    def test_daemon_answers_while_job_runs(self):
        """A job's own step sleeps (holding the job's own thread, not the lane): a `say` on the
        session still gets its answer while the job is still running (D25's slot is about
        request ordering, not about blocking the daemon's own async lane)."""
        proceed = threading.Event()

        def slow_job_factory(cfg, control, tools):
            def step(messages):
                proceed.wait(2)
                yield ("text", "job done")
            return step

        def user_factory(cfg, control=None, tools=None):
            calls = {"n": 0}

            def step(messages):
                n = calls["n"]
                calls["n"] += 1
                if n == 0:
                    yield ("tool_calls", [{"id": "c1", "name": "start_job", "arguments": {"goal": "research"}}])
                else:
                    yield ("text", "hi back")
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "start a job"})
            first = await c.until("turn.end")
            for _ in range(200):
                if daemon.jobs_mgr.jobs:
                    break
                await asyncio.sleep(0.01)
            c.send({"type": "say", "text": "hi"})
            second = await c.until("turn.end")  # this must not wait on the job's own sleep
            proceed.set()
            job = next(iter(daemon.jobs_mgr.jobs.values()))
            for _ in range(200):  # let the job finish before close() tears down the state under it
                if job.state != "running":
                    break
                await asyncio.sleep(0.01)
            c.close()
            return first, second

        first, second = self.run_with(user_factory, scenario, job_factory=slow_job_factory)
        self.assertEqual(first[-1]["kind"], "answer")
        self.assertEqual(second[-1]["kind"], "answer")

    def test_job_lines_reach_a_moved_screen(self):
        """U4 (5b): the job's session went quiet (D41), the screen attached to a newer one, and the
        job's lines still reach it."""
        def job_factory(cfg, control, tools):
            def step(messages):
                yield ("text", "found it")
            return step

        async def scenario(daemon):
            daemon.record("session_start", session="older")
            daemon.state.db.execute("UPDATE sessions SET last_ts = last_ts - ? WHERE id = ?", (3 * 3600, "older"))
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = (await c.until("attached"))[-1]
            daemon.jobs_mgr.start("research", "older")
            lines = await c.until("job.end")
            for _ in range(200):  # let the job finish before close() tears down the state under it
                if all(j.state != "running" for j in daemon.jobs_mgr.jobs.values()):
                    break
                await asyncio.sleep(0.01)
            c.close()
            return attached, lines

        attached, lines = self.run_with(fake_model("ok"), scenario, job_factory=job_factory)
        self.assertNotEqual(attached["session"], "older")
        self.assertEqual([m["type"] for m in lines if m["type"].startswith("job.")], ["job.start", "job.end"])
        self.assertEqual(lines[-1]["state"], "done")


class JobSignalTest(DaemonHarness):
    def test_job_done_speaks_without_budget(self):
        """job_done's trust is "user" (D8 sec 8): the outcome of what the user started spends
        no budget and honours no quiet hours, exactly like the user's own schedule_due."""
        async def scenario(daemon):
            daemon.rules = dataclasses.replace(daemon.rules, quiet_hours="0-24", daily_budget=0)
            daemon.push_signal(Signal(sense="jobs", kind="job_done", key="job-budget-test",
                                      summary="Job done: research\nfound it",
                                      fields={"goal": "research", "exhausted": False},
                                      trust="self", exempt=True))
            for _ in range(200):
                if any(e["type"] == "decision" for e in Ledger(self.home).read()):
                    break
                await asyncio.sleep(0.01)
            return list(Ledger(self.home).read())

        events = self.run_with(fake_model("ok"), scenario)
        decisions = [e for e in events if e["type"] == "decision"]
        self.assertTrue(any(d["action"] == "speak" for d in decisions))


FAKE_SERVER = Path(__file__).parent / "mcp_fake_server.py"


class McpDaemonTest(DaemonHarness):
    """The mcp load/close/pin-check path, live against `tests/mcp_fake_server.py` - a real
    child process, no model credits."""

    def test_mcp_tools_registered_at_start(self):
        config.set(self.home, "mcp_servers",
                   {"fake": {"transport": "stdio", "command": [sys.executable, str(FAKE_SERVER)]}})

        async def scenario(daemon):
            await daemon.mcp.loaded  # load() now runs as a background task - wait for it
            return dict(tools_mod.TOOLS), daemon.mcp.servers._sessions["fake"]._transport._proc

        snapshot, proc = self.run_with(fake_model("ok"), scenario)
        self.assertIn("mcp__fake__echo", snapshot)
        self.assertEqual(len([e for e in self.events() if e["type"] == "pin_accept"]), 1)
        self.assertNotIn("mcp__fake__echo", tools_mod.TOOLS)  # closed: the key is gone
        self.assertIsNotNone(proc.poll())  # and the child is dead

    def test_a_refused_tool_is_never_held(self):
        """U33: the install pinned only what its trial kept; at load the tools the loader refuses are
        dropped before the pin check - no `accept_pins` proposal, no `tool_held`, never pinned."""
        cmd = [sys.executable, str(FAKE_SERVER)]
        config.set(self.home, "mcp_servers", {"fake": {"transport": "stdio", "command": cmd}})
        self.run_with(fake_model("ok"), lambda daemon: _noop())  # the pins an install writes: echo only
        config.set(self.home, "mcp_servers", {"fake": {"transport": "stdio", "command": [*cmd, "--refused-tools"]}})

        async def scenario(daemon):
            await daemon.mcp.loaded
            return sorted(n for n in tools_mod.TOOLS if n.startswith("mcp__"))

        self.assertEqual(self.run_with(fake_model("ok"), scenario), ["mcp__fake__echo"])
        events = self.events()
        self.assertEqual([e for e in events if e["type"] == "approval_pending" and e["tool"] == "accept_pins"], [])
        self.assertEqual([e for e in events if e["type"] == "signal" and e["kind"] == "tool_held"], [])
        self.assertEqual([e["tool"] for e in events if e["type"] == "pin_accept"], ["echo"])

    def test_rug_pull_holds(self):
        config.set(self.home, "mcp_servers",
                   {"fake": {"transport": "stdio", "command": [sys.executable, str(FAKE_SERVER)]}})
        self.run_with(fake_model("ok"), lambda daemon: _noop())  # pins the original description

        config.set(self.home, "mcp_servers", {"fake": {
            "transport": "stdio", "command": [sys.executable, str(FAKE_SERVER), "--description", "changed"]}})
        self.run_with(fake_model("ok"), lambda daemon: _noop())

        self.assertNotIn("mcp__fake__echo", tools_mod.TOOLS)
        events = self.events()
        pending = [e for e in events if e["type"] == "approval_pending" and e["tool"] == "accept_pins"]
        self.assertEqual(len(pending), 1)
        self.assertTrue(any(e["type"] == "signal" and e["kind"] == "tool_held" for e in events))
        approval_id = pending[0]["id"]

        async def approve_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": approval_id})
            await c.recv()
            c.close()
            return dict(tools_mod.TOOLS)

        snapshot = self.run_with(fake_model("ok"), approve_scenario)
        self.assertIn("mcp__fake__echo", snapshot)
        # The first run's own pin_accept (the original description) would already satisfy a bare
        # "any pin_accept exists" - assert the NEW description's hash specifically, and that
        # approving actually cleared the hold (review).
        new_hash = mcp_pins.fingerprint({"description": "changed", "inputSchema": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}})["description"]
        pin_accepts = [e for e in self.events() if e["type"] == "pin_accept"]
        self.assertTrue(any(e["description_hash"] == new_hash for e in pin_accepts))
        state = State(self.home / "state.db")
        self.assertEqual([r for r in state.pending() if r["tool"] == "accept_pins"], [])

    def test_proposal_table_routes_by_tool(self):
        async def scenario(daemon):
            proposal = {"target": "USER.md", "op": "add", "old": "", "new": "Likes tea.", "evidence": [1]}
            daemon.record("approval_pending", session="s1", origin="memory", id="appr1",
                          call_id="appr1", tool="edit_memory", args=proposal, messages=[])
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": "appr1"})
            reply = await c.recv()
            c.close()
            return reply, (self.home / "USER.md").read_text(encoding="utf-8")

        reply, after = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(reply, {"type": "turn.end", "turn": "", "kind": "resumed"})
        self.assertIn("Likes tea.", after)  # edit_memory still reaches memory_mgr.decide

        config.set(self.home, "grants", {"edit_memory": "ask"})

        def gate_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "edit_memory", "arguments": {
                    "target": "wiki/Test.md", "op": "add", "old": "", "new": "# Test\nline"}}])
            return step

        async def gate_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "please remember this"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(gate_model, gate_scenario)
        self.assertEqual(seen[-1]["kind"], "gated")  # a LANE row still resumes, unaffected by the table

    def test_secret_missing_records_one_error(self):
        config.set(self.home, "mcp_servers", {"gh": {
            "transport": "stdio", "command": ["true"], "env": {"TOKEN": "keychain:bot"}}})

        with mock.patch("pathfinder.keychain.subprocess.run", side_effect=FileNotFoundError):
            snapshot = self.run_with(fake_model("ok"), _snapshot)

        self.assertFalse(any(name.startswith("mcp__gh__") for name in snapshot))
        errors = [e for e in self.events() if e["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("secret missing", errors[0]["message"])

    def test_mcp_tool_call_in_a_turn(self):
        config.set(self.home, "mcp_servers",
                   {"fake": {"transport": "stdio", "command": [sys.executable, str(FAKE_SERVER)]}})

        def call_model(cfg, control=None, tools=None):
            calls = {"n": 0}
            def step(messages):
                calls["n"] += 1
                if calls["n"] == 1:
                    yield ("tool_calls", [{"id": "c1", "name": "mcp__fake__echo", "arguments": {"text": "hi"}}])
                else:
                    yield ("text", "done")
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "echo hi"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        config.set(self.home, "grants", {"mcp__fake__echo": "allow"})
        seen = self.run_with(call_model, scenario)
        self.assertEqual(seen[-1]["kind"], "answer")
        results = [e for e in self.events() if e["type"] == "tool_result"]
        self.assertTrue(any("hi" in str(e["result"]) for e in results))

        config.set(self.home, "grants", {})
        seen2 = self.run_with(call_model, scenario)
        self.assertEqual(seen2[-1]["kind"], "gated")


async def _noop():
    return None


async def _snapshot(daemon):
    await daemon.mcp.loaded  # load() now runs as a background task - wait for it
    return dict(tools_mod.TOOLS)


class InstallerDaemonTest(DaemonHarness):
    """`install`/`uninstall` over the socket, and the `propose_install` tool's origin - against
    `tests/mcp_fake_server.py`, no model credits, no npm/PyPI network."""

    def test_install_over_socket_then_approve(self):
        # B3 requires the runtime hint to be the real launcher ("npx" for npm) - a symlink named
        # "npx" standing in for it on PATH keeps this offline (the fake server ignores the
        # `-y`/pinned-spec argv npx would normally get, via `parse_known_args`).
        npx_dir = Path(tempfile.mkdtemp(prefix="npx-"))
        (npx_dir / "npx").symlink_to(FAKE_SERVER)
        self.addCleanup(shutil.rmtree, npx_dir, True)
        fake_run_ok = mock.patch(
            "pathfinder.installer.subprocess.run",
            return_value=type("R", (), {"returncode": 0, "stdout": json.dumps(
                {"dist.integrity": "sha512-abc", "dist.tarball": "https://x", "license": "MIT"})})())
        fake_fetch = mock.patch("pathfinder.mirror.find", return_value=[{"server": {
            "name": "io.github.acme/widget", "version": "1.0.0", "description": "a widget server",
            "packages": [{"registryType": "npm", "identifier": "widget", "version": "1.0.0",
                         "runtimeHint": "npx"}]}}])
        fake_path = mock.patch.dict(os.environ, {"PATH": f"{npx_dir}:{os.environ.get('PATH', '')}"})

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "install", "name": "widget"})
            reply = await c.recv()
            c.close()
            return reply

        with fake_run_ok, fake_fetch, fake_path:
            reply = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(reply["type"], "install")
        self.assertIn("install_server", reply["line"])
        approval_id = reply["id"]

        async def approve_scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": approval_id})
            await c.recv()
            c.close()
            return dict(tools_mod.TOOLS), config.load(self.home)["grants"]

        with mock.patch.dict(os.environ, {"HOME": str(self.home),
                                          "PATH": f"{npx_dir}:{os.environ.get('PATH', '')}"}):
            snapshot, grants = self.run_with(fake_model("ok"), approve_scenario)
        server_id = next(iter(config.load(self.home)["mcp_servers"]))
        self.assertIn(f"mcp__{server_id}__echo", snapshot)
        self.assertEqual(grants.get(f"mcp__{server_id}__echo"), "allow")
        self.assertTrue(any(e["type"] == "install_done" for e in self.events()))

    def test_signal_turn_cannot_propose_install(self):
        config.set(self.home, "senses", {"catalog": {}})
        self.addCleanup(tools_mod.TOOLS.pop, "search_tools", None)
        self.addCleanup(tools_mod.TOOLS.pop, "propose_install", None)

        async def scenario(daemon):
            return dict(tools_mod.TOOLS)

        self.run_with(fake_model("ok"), scenario)
        self.assertIn("propose_install", tools_mod.for_origin("user"))
        self.assertNotIn("propose_install", tools_mod.for_origin("signal"))
        self.assertNotIn("search_tools", tools_mod.for_origin("signal"))


class ProposalGateLineTest(DaemonHarness):
    def test_user_turn_proposal_sends_a_gate_line(self):
        """Live 2026-09-26: an install_server proposal from the owner's own turn never reached
        the screen, so they typed "approve" into the chat and nothing was installed. A pass's
        own proposal stays behind its `memory_proposed` notice (no line)."""
        cand = {"name": "io.github.acme/widget", "version": "1.0.0",
                "verification": {"license": {"spdx": "MIT", "verdict": "ok"},
                                 "sandbox_trial": {"tools": [{"name": "echo", "read_only_hint": True}]}},
                "install": {"integrity": "sha512-abc", "runtime": "npx", "args": ["widget@1.0.0"]}}

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            sid = (await c.recv())["session"]
            daemon.record("approval_pending", session=sid, origin="memory", id="m1", call_id="m1",
                          tool="edit_memory", args={"target": "USER.md"}, messages=[])
            daemon.record("approval_pending", session=sid, origin="user", id="a1", call_id="a1",
                          tool="install_server", args=cand, messages=[])
            line = await c.recv()
            c.close()
            return line

        line = self.run_with(fake_model("ok"), scenario)
        self.assertEqual((line["type"], line["id"], line["tool"]), ("gate", "a1", "install_server"))
        self.assertTrue(line["args"].startswith("io.github.acme/widget@1.0.0  license MIT ok"), line["args"])


class PastedTokenScreenTest(DaemonHarness):
    """D53: a screen line shows what the ledger holds, so the owner approves what will run. A
    model's call is not the owner's text, so a token there is redacted (D66). The Keychain is a
    dict here."""
    TOKEN = "ntn_" + "s" * 40  # built by concatenation, so the scanner never flags this file

    def setUp(self):
        super().setUp()
        self.items, self.deleted = {}, []
        for name, fake in (("store", lambda s, a, v: self.items.__setitem__(a, v)),
                           ("delete", lambda s, a: self.deleted.append(a) or True),
                           ("secret", lambda s, a: None), ("_gone", lambda a: True)):
            patch = mock.patch(f"pathfinder.keychain.{name}", side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)

    def test_gate_line_shows_what_the_approval_holds(self):
        note_path = str(self.home / "note.txt")

        def gate_model(cfg, control=None, tools=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": self.TOKEN}}])
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            sid = (await c.recv())["session"]
            c.send({"type": "say", "text": "please write a note"})
            seen = await c.until("turn.end")
            daemon.record("approval_pending", session=sid, origin="user", id="a2", call_id="a2",
                          tool="write_file", args={"content": self.TOKEN}, messages=[])
            seen.append(await c.recv())
            c.close()
            return seen

        seen = self.run_with(gate_model, scenario)
        gates = [m for m in seen if m["type"] == "gate"]
        self.assertEqual([g["args"]["content"] for g in gates], ["[redacted]", "[redacted]"])
        self.assertEqual(self.items, {})  # no Keychain call for a call's arguments
        self.assertNotIn(self.TOKEN, json.dumps(seen))
        state = State(self.home / "state.db")
        self.addCleanup(state.close)
        for gate in gates:
            self.assertEqual(state.approval(gate["id"])["args"], gate["args"])

    def test_pasted_notice_shows_the_handle(self):
        """U15b: the owner's line was vaulted - the screen and `say` are told the handle; a handle
        merely typed again is not announced."""
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.recv()
            c.send({"type": "say", "text": f"use {self.TOKEN}"})
            seen = await c.until("turn.end")
            c.send({"type": "say", "text": "bind [secret:pasted-1] please"})
            seen += await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(fake_model("ok"), scenario)
        pasted = [m for m in seen if m["type"] == "pasted"]
        self.assertEqual(len(pasted), 1)
        (handle,) = pasted[0]["handles"]
        self.assertEqual(self.items, {handle: self.TOKEN})
        self.assertIn(f"stored as {handle} for 24 h - bind it with /secret <id> NAME --from {handle}",
                      tui.render(pasted[0]))
        self.assertNotIn(self.TOKEN, json.dumps(seen))

    def test_daemon_start_sweeps_expired_pastes(self):
        """D53: a daemon start sweeps once; opening a ledger does not (a CLI command, the memory
        pass's read-only ledger)."""
        index = self.home / "pasted.json"
        index.write_text(json.dumps({"pasted": {"pasted-1": time.time() - 25 * 3600}}))
        Ledger(self.home)
        self.assertEqual(self.deleted, [])

        async def scenario(daemon):
            return None

        self.run_with(fake_model("ok"), scenario)
        self.assertEqual(self.deleted, ["pasted-1"])
        self.assertEqual(json.loads(index.read_text()), {"pasted": {}})

    def test_steer_is_kept_as_recorded(self):
        """A steer said after the model's last chunk is re-sent as the next turn's `trailing` text
        from `control.steers`, so that deque holds the handle, not the token."""
        async def scenario(daemon):
            s = mock.Mock(id="s1", turn="t1", control=TurnControl())
            daemon.steer(s, f"use {self.TOKEN}")
            return list(s.control.steers)

        (steer,) = self.run_with(fake_model("ok"), scenario)
        self.assertRegex(steer, r"^use \[secret:pasted-\d+\]$")


class ChatApprovalTest(DaemonHarness):
    """D52 part 2 (m10's T68): "I approve" typed into chat was taken by the model for an approval."""

    def _gate(self, daemon, sid, id):
        daemon.record("approval_pending", session=sid, turn="t0", origin="user", id=id, call_id=id,
                      tool="edit_memory", args={"target": "USER.md"}, messages=[])

    async def _say(self, c, text):
        c.send({"type": "say", "text": text})
        return [m for m in await c.until("turn.end") if m["type"] != "gate"]

    async def _attach(self):
        c = await Client.open(self.home)
        c.send({"type": "attach"})
        return c, (await c.recv())["session"]

    def test_chat_approval_answered_without_a_model_call(self):
        factory = fake_model("ok")

        async def scenario(daemon):
            c, sid = await self._attach()
            self._gate(daemon, sid, "a1")
            seen = await self._say(c, "I'll approve.")
            c.close()
            return seen, daemon.state.approval("a1")["decision"]

        seen, decision = self.run_with(factory, scenario)
        self.assertEqual([m["type"] for m in seen], ["turn.start", "delta", "turn.end"])
        self.assertTrue(seen[1]["text"].endswith(": /approve a1 or /deny a1 (edit_memory; shell: pathfinder "
                                                 "approve|deny a1) - if you meant something else, say it "
                                                 "again · 다른 뜻이었다면 다시 말해 주세요"), seen[1]["text"])
        self.assertEqual(seen[-1]["kind"], "answer")
        self.assertEqual(factory.seen, [])  # no model request
        self.assertIsNone(decision)  # still pending
        turn = [e for e in self.events() if e["type"] in ("user", "turn_start", "assistant", "turn_end")]
        self.assertEqual([e["type"] for e in turn], ["user", "turn_start", "assistant", "turn_end"])
        self.assertEqual((turn[0]["text"], turn[2]["text"], turn[2]["final"]), ("I'll approve.", seen[1]["text"], True))
        self.assertEqual((turn[3]["kind"], "usage" in turn[3]), ("answer", False))

    def test_chat_approval_needs_an_open_gate_newer_than_the_last_user_message(self):
        factory = fake_model("ok")

        async def scenario(daemon):
            c, sid = await self._attach()
            no_gate = await self._say(c, "yes")
            self._gate(daemon, sid, "old")
            await self._say(c, "something else first")
            talked_past = await self._say(c, "yes")
            self._gate(daemon, sid, "new")
            answered = await self._say(c, "yes")
            c.close()
            return no_gate, talked_past, answered

        no_gate, talked_past, answered = self.run_with(factory, scenario)
        self.assertEqual(len(factory.seen), 3)  # the answered "yes" made no request
        for seen in (no_gate, talked_past):
            self.assertEqual([m["text"] for m in seen if m["type"] == "delta"], ["ok "])
        self.assertIn("/approve new ", answered[1]["text"])
        self.assertNotIn("old", answered[1]["text"])

    def test_longer_message_still_reaches_the_model(self):
        factory = fake_model("ok")

        async def scenario(daemon):
            c, sid = await self._attach()
            self._gate(daemon, sid, "a1")
            seen = await self._say(c, "yes, and also add X")
            c.close()
            return seen, daemon.state.approval("a1")["decision"]

        seen, decision = self.run_with(factory, scenario)
        self.assertEqual(factory.seen[0][-1], {"role": "user", "content": "yes, and also add X"})
        self.assertEqual([m["text"] for m in seen if m["type"] == "delta"], ["ok "])
        self.assertIsNone(decision)

    def test_steer_is_never_intercepted(self):
        """D17: a `say` while the owner's turn runs is a steer, with a gate open or not."""
        proceed = threading.Event()
        tools_mod.TOOLS["slow_tool"] = ToolSpec(name="slow_tool", description="", params={}, required=[],
                                                handler=lambda control=None: proceed.wait(2) and {"slept": True})
        self.addCleanup(tools_mod.TOOLS.pop, "slow_tool", None)
        seen_messages = []

        def factory(cfg, control=None, tools=None):
            def step(messages):
                seen_messages.append([dict(m) for m in messages])
                if len(seen_messages) == 1:
                    yield ("tool_calls", [{"id": "c1", "name": "slow_tool", "arguments": {}}])
                else:
                    yield ("text", "done")
            return step

        async def scenario(daemon):
            c, sid = await self._attach()
            c.send({"type": "say", "text": "go"})
            await c.until("turn.start")
            s = daemon.sessions[sid]
            for _ in range(200):
                if s.control is not None and s.control.in_tool:
                    break
                await asyncio.sleep(0.01)
            self._gate(daemon, sid, "a1")  # open: newer than "go"
            c.send({"type": "say", "text": "yes"})
            for _ in range(200):
                if s.control.steers:
                    break
                await asyncio.sleep(0.01)
            proceed.set()
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(factory, scenario)
        self.assertEqual([m["text"] for m in seen if m["type"] == "delta"], ["done"])
        self.assertIn({"role": "user", "content": "yes"}, seen_messages[1])
        steers = [e["text"] for e in self.events() if e["type"] == "user" and e.get("steer")]
        self.assertEqual(steers, ["yes"])
        self.assertFalse(any("/approve" in e.get("text", "") for e in self.events() if e["type"] == "assistant"))


class SessionLifetimeTest(DaemonHarness):
    """D41 (live 2026-09-29): an open screen's `presence` kept one session alive for four days,
    and its old wrong answers kept coming back. Two hours without conversation end a session."""

    def _age(self, daemon, sid, seconds):
        daemon.state.db.execute("UPDATE sessions SET last_ts = last_ts - ? WHERE id = ?", (seconds, sid))

    def test_presence_does_not_keep_a_session(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "one"})
            await c.until("turn.end")
            first = next(iter(daemon.sessions))
            self._age(daemon, first, 3 * 3600)
            c.send({"type": "presence", "idle_s": 0})
            await asyncio.sleep(0.05)
            fresh = daemon.state.latest_session(7200)
            c.send({"type": "say", "text": "two"})
            await c.until("turn.end")
            c.close()
            return first, fresh

        first, fresh = self.run_with(fake_model("ok"), scenario)
        self.assertIsNone(fresh)
        users = [e for e in self.events() if e["type"] == "user"]
        self.assertEqual(users[0]["session"], first)
        self.assertNotEqual(users[1]["session"], first)

    def test_a_notice_after_the_gap_reaches_the_open_screen(self):
        async def scenario(daemon):
            _never_quiet(daemon)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            first = (await c.recv())["session"]
            self._age(daemon, first, 3 * 3600)
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="gap-1",
                                      summary="Scheduled note: train", fields={"id": "s1"}, trust="user"))
            notice = (await c.until("notice"))[-1]
            c.send({"type": "presence", "idle_s": 5})
            await asyncio.sleep(0.05)
            c.close()
            return first, notice

        first, notice = self.run_with(fake_model("ok"), scenario)
        self.assertIn("Scheduled note: train", notice["text"])
        delivery = next(e for e in self.events() if e["type"] == "delivery")
        self.assertNotEqual(delivery["session"], first)  # the notice opened the new session
        presence = [e for e in self.events() if e["type"] == "presence"]
        self.assertEqual(presence[-1]["session"], delivery["session"])  # the screen followed it


    def test_a_screen_closed_right_after_the_move_leaves(self):
        """Review blocker: closed before its next line, the moved connection stayed in the new
        session, and the next notice was recorded as delivered to nobody."""
        async def scenario(daemon):
            _never_quiet(daemon)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            first = (await c.recv())["session"]
            self._age(daemon, first, 3 * 3600)
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="gap-2",
                                      summary="Scheduled note: bus", fields={"id": "s2"}, trust="user"))
            await c.until("notice")
            c.close()
            await asyncio.sleep(0.1)
            return [len(x.conns) for x in daemon.sessions.values()]

        self.assertEqual(self.run_with(fake_model("ok"), scenario), [0, 0])

    def test_notice_held_while_typing_after_a_move(self):
        """5a: the screen typed on the old session; the notice opened a new one with no presence
        row, and went out mid-sentence. The owner has one pair of hands: the newest row counts."""
        async def scenario(daemon):
            _never_quiet(daemon)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            first = (await c.recv())["session"]
            c.send({"type": "presence", "idle_s": 1})  # typing
            await asyncio.sleep(0.05)
            self._age(daemon, first, 3 * 3600)
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="gap-3",
                                      summary="Scheduled note: call", fields={"id": "s3"}, trust="user"))
            for _ in range(200):
                if daemon.outbox.queue or any(e["type"] == "delivery" for e in self.events()):
                    break
                await asyncio.sleep(0.01)
            held = [n.session for n in daemon.outbox.queue]
            c.send({"type": "presence", "idle_s": 999})  # stopped typing, now on the new session
            notice = (await c.until("notice"))[-1]
            c.close()
            return first, held, notice

        first, held, notice = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(len(held), 1)
        self.assertNotEqual(held[0], first)  # the notice's session is the new one
        self.assertIn("Scheduled note: call", notice["text"])

    def test_silent_triage_does_not_keep_a_session(self):
        """Review: every silenced signal records a `decision` in the session; only a delivered
        notice is conversation."""
        async def scenario(daemon):
            s = daemon._target_session()
            self._age(daemon, s.id, 3 * 3600)
            daemon.record("decision", session=s.id, origin="signal", signal_key="k", action="silence", why="seen")
            after_triage = daemon.state.latest_session(7200)
            daemon.record("assistant", session=s.id, origin="signal", text="a notice", final=True)
            return s.id, after_triage, daemon.state.latest_session(7200)

        sid, after_triage, after_notice = self.run_with(fake_model("ok"), scenario)
        self.assertIsNone(after_triage)
        self.assertEqual(after_notice, sid)

    def _old_session(self, daemon):
        daemon.record("session_start", session="old")
        daemon.record("user", session="old", turn="t1", origin="user", trust="user", text="old question")
        self._age(daemon, "old", 3 * 3600)

    async def _settle(self, daemon):  # let a job's thread finish before close() tears the state down
        for _ in range(300):
            if all(j.state not in ("running", "gated") for j in daemon.jobs_mgr.jobs.values()):
                break
            await asyncio.sleep(0.01)

    def test_say_after_a_job_gate_in_an_aged_session_lands_in_the_new_session(self):
        """D63 (live 2026-09-30): a job's gate made a session D41 had ended the newest again, and the
        owner's next message went into it with its old history. The phrase typed after the gate gets
        the harness line in the screen's session; said again, it reaches the model there."""
        def job_factory(cfg, control, tools):
            def step(messages):
                if not any(m.get("role") == "tool" for m in messages):
                    yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                           "arguments": {"path": str(self.home / "n.txt"), "content": "hi"}}])
                else:
                    yield ("text", "done")
            return step
        model = fake_model("ok")

        async def scenario(daemon):
            self._old_session(daemon)
            aged = daemon.state.session("old")["last_ts"]
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            screen = (await c.until("attached"))[-1]["session"]
            daemon.jobs_mgr.start("write a note", "old")
            gate = (await c.until("job.gate"))[-1]
            said = []
            for _ in range(2):
                c.send({"type": "say", "text": "응"})
                said.append("".join(m["text"] for m in await c.until("turn.end") if m["type"] == "delta"))
            seen = list(model.seen)
            c.send({"type": "approve", "id": gate["approval"]})
            await c.until("job.end")
            await self._settle(daemon)
            c.close()
            return screen, gate, said, seen, daemon.state.session("old")["last_ts"] == aged

        screen, gate, said, seen, still_aged = self.run_with(model, scenario, job_factory=job_factory)
        self.assertNotEqual(screen, "old")
        self.assertTrue(still_aged)  # the job's start, gate, approval and end moved nothing
        self.assertIn(f"/approve {gate['approval']} ", said[0])
        self.assertTrue(said[0].endswith(" - if you meant something else, say it again · 다른 뜻이었다면 다시 말해 주세요"))
        self.assertEqual(said[1], "ok ")
        self.assertEqual(len(seen), 1)  # only the phrase said again made a request
        self.assertNotIn("old question", json.dumps(seen[0], ensure_ascii=False))
        self.assertEqual([e["session"] for e in self.events() if e["type"] == "user" and e["text"] == "응"],
                         [screen, screen])

    def test_job_done_notice_lands_in_the_screens_session(self):
        """D63: a job from a session D41 ended reports where the screen is - the goal and the summary,
        not the old session's history."""
        def job_factory(cfg, control, tools):
            def step(messages):
                yield ("text", "found it")
            return step
        model = fake_model("ok")

        async def scenario(daemon):
            _never_quiet(daemon)
            self._old_session(daemon)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            screen = (await c.until("attached"))[-1]["session"]
            daemon.jobs_mgr.start("research", "old")
            notice = (await c.until("notice"))[-1]
            await self._settle(daemon)
            c.send({"type": "say", "text": "and then?"})
            await c.until("turn.end")
            c.close()
            return screen, notice

        screen, notice = self.run_with(model, scenario, job_factory=job_factory)
        self.assertNotEqual(screen, "old")
        self.assertTrue(notice["text"].startswith("Job done: research\nfound it"), notice["text"])
        self.assertEqual([e["session"] for e in self.events() if e["type"] in ("delivery", "assistant")
                          and e["origin"] == "signal"], [screen, screen])
        history = json.dumps(model.seen[-1], ensure_ascii=False)
        self.assertIn("found it", history)
        self.assertNotIn("old question", history)

    def test_notice_text_passes_clean(self):
        """D62, found at U25's review: every sense's summary reaches the screen and then history. It
        is cleaned once where the notice text is formed - live, rebuilt after a restart, and a job's
        worker text."""
        def job_factory(cfg, control, tools):
            def step(messages):
                yield ("text", "found \x1b[31mit\x1b[0m\u202e now")
            return step

        async def scenario(daemon):
            _never_quiet(daemon)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            screen = (await c.until("attached"))[-1]["session"]
            daemon.jobs_mgr.start("research", screen)
            live = (await c.until("notice"))[-1]
            await self._settle(daemon)
            c.send({"type": "presence", "idle_s": 0})  # typing: the next notice is held, then the daemon stops
            await asyncio.sleep(0.05)
            daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key="dirty-1", fields={"id": "s1"},
                                      summary="Scheduled note: \x1b[1mtrain\u202e", trust="user"))
            for _ in range(200):
                if any(e.get("signal_key") == "dirty-1" and e.get("action") == "speak" for e in self.events()):
                    break
                await asyncio.sleep(0.01)
            c.close()
            return live

        live = self.run_with(fake_model("ok"), scenario, job_factory=job_factory)
        self.assertTrue(live["text"].startswith("Job done: research\nfound it now"), live["text"])
        rebuilt = State(self.home / "rebuilt.db")  # a restart's state, rebuilt from the ledger
        rebuilt.catch_up(Ledger(self.home))
        held = [row["text"] for row in rebuilt.undelivered()]
        rebuilt.close()
        self.assertEqual(held, ["Scheduled note: train"])
        texts = [e["text"] for e in self.events() if e["type"] in ("decision", "assistant") and e.get("text")]
        self.assertTrue(texts)
        for text in texts:
            self.assertNotRegex(text, "[\x1b\u202e]")

    def test_a_notice_held_by_the_rate_limit_goes_out_later_clean(self):
        """U32's review: the release of a notice the rate limit held (`signal_ready`) carries the
        clean summaries recorded with its decision."""
        async def scenario(daemon):
            daemon.rules = dataclasses.replace(daemon.rules, quiet_hours="0-0", min_interval_s=1, burst_limit=999)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.until("attached")
            for i, summary in enumerate(["Scheduled note: one\n\ttwo", "Scheduled note: \x1b[31mred\u202e"]):
                daemon.push_signal(Signal(sense="schedule", kind="schedule_due", key=f"held-{i}", fields={"id": str(i)},
                                          summary=summary, trust="user"))
                await asyncio.sleep(0.05)
            texts = [(await c.until("notice"))[-1]["text"] for _ in range(2)]
            c.close()
            return texts

        texts = self.run_with(fake_model(""), scenario)
        self.assertEqual(texts, ["Scheduled note: one\n\ttwo", "Scheduled note: red"])
        held = [e for e in self.events() if e["type"] == "decision" and e["signal_key"] == "held-1"]
        self.assertEqual(held[0]["text"], "Scheduled note: red")


class AgendaTest(DaemonHarness):
    def test_every_turn_carries_the_agenda(self):
        """D42 (live: a 01:13 briefing said "no reminders" without looking)."""
        factory = fake_model("ok")

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "briefing"})
            await c.until("turn.end")
            c.close()

        with mock.patch("pathfinder.signals.context", return_value="AGENDA-LINE"):
            self.run_with(factory, scenario)
        second = factory.seen[0][1]["content"]
        self.assertTrue(second.startswith("Local time now:"))
        self.assertIn("\n\nAGENDA-LINE", second)


class SigtermTest(DaemonHarness):
    """`serve()` runs a real Daemon (not through `run_with`, which owns start()/close() itself -
    the SIGTERM handler lives inside `serve()`)."""

    def test_sigterm_closes_mcp_servers(self):
        """launchd's `service stop` sends SIGTERM - without a handler for it, `close()` never
        runs and every npx child is orphaned (m5's bench had to killpg by hand for this)."""
        config.set(self.home, "mcp_servers",
                   {"fake": {"transport": "stdio", "command": [sys.executable, str(FAKE_SERVER)]}})

        async def main():
            daemon = Daemon(self.home, step_factory=fake_model("ok"))
            serve_task = asyncio.ensure_future(daemon.serve())
            for _ in range(500):
                if daemon.mcp.loaded is not None:
                    break
                await asyncio.sleep(0.01)
            await daemon.mcp.loaded
            proc = daemon.mcp.servers._sessions["fake"]._transport._proc
            # an attached client (the TUI, all day): `async with server` waited for it forever
            reader, writer = await asyncio.open_unix_connection(str(daemon.home / SOCKET))
            writer.write(b'{"type": "attach"}\n')
            await writer.drain()
            await reader.readline()  # attached
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(serve_task, 5)
            writer.close()
            return proc, daemon.home / SOCKET

        proc, sock_path = asyncio.run(main())
        self.assertIsNotNone(proc.poll())  # the child is dead, not orphaned
        self.assertFalse(sock_path.exists())


if __name__ == "__main__":
    unittest.main()
