"""The daemon end to end over a real unix socket, against a fake model."""
import asyncio
import dataclasses
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import config
from pathfinder import tools as tools_mod
from pathfinder.daemon import SOCKET, Daemon
from pathfinder.deliver import Notice
from pathfinder.ledger import Ledger
from pathfinder.llm import LLMError
from pathfinder.loop import TurnControl
from pathfinder.signals import Signal
from pathfinder.state import State
from pathfinder.tools import ToolSpec


def fake_model(reply="Hello there."):
    def factory(cfg, cancel=None, tools=None):
        factory.cancels.append(cancel)
        def step(messages):
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

    def run_with(self, factory, scenario, job_factory=None):
        async def main():
            daemon = Daemon(self.home, step_factory=factory, job_step_factory=job_factory)
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
        self.assertEqual(said[0]["text"], "working " * 8)  # the user saw 8 steps of it; history must too

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
        self.assertEqual(len(seen_messages), 2)
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


class JobSignalTest(DaemonHarness):
    def test_job_done_speaks_without_budget(self):
        """job_done's trust is "user" (D8 sec 8): the outcome of what the user started spends
        no budget and honours no quiet hours, exactly like the user's own schedule_due."""
        async def scenario(daemon):
            daemon.rules = dataclasses.replace(daemon.rules, quiet_hours="0-24", daily_budget=0)
            daemon.push_signal(Signal(sense="jobs", kind="job_done", key="job-budget-test",
                                      summary="Job done: research\nfound it",
                                      fields={"goal": "research", "exhausted": False},
                                      trust="user"))
            for _ in range(200):
                if any(e["type"] == "decision" for e in Ledger(self.home).read()):
                    break
                await asyncio.sleep(0.01)
            return list(Ledger(self.home).read())

        events = self.run_with(fake_model("ok"), scenario)
        decisions = [e for e in events if e["type"] == "decision"]
        self.assertTrue(any(d["action"] == "speak" for d in decisions))


if __name__ == "__main__":
    unittest.main()
