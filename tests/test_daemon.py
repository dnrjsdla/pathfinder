"""The daemon end to end over a real unix socket, against a fake model."""
import asyncio
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pathfinder import tools as tools_mod
from pathfinder.daemon import SOCKET, Daemon
from pathfinder.ledger import Ledger
from pathfinder.llm import LLMError
from pathfinder.loop import TurnControl
from pathfinder.state import State
from pathfinder.tools import ToolSpec


def fake_model(reply="Hello there."):
    def factory(cfg, cancel=None):
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


def blocked_model(cfg, cancel=None):
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

    def run_with(self, factory, scenario):
        async def main():
            daemon = Daemon(self.home, step_factory=factory)
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
        roles = [(m["role"], m["content"].strip()) for m in factory.seen[-1][1:]]
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
        def talkative(cfg, cancel=None):
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
        history = [m["content"].strip() for m in factory.seen[-1][1:]]
        self.assertEqual(history, ["remember the word plum", "noted", "remember the word plum"])

    def test_daemon_gate_survives_restart(self):
        note_path = str(self.home / "note.txt")

        def gate_model(cfg, control=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": "hi"}}])
            return step

        def answer_model(cfg, control=None):
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

        def gate_model(cfg, control=None):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": "hi"}}])
            return step

        def ack_model(cfg, control=None):
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

        def factory(cfg, control=None):
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

        def factory(cfg, control=None):
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

        def gate_model(cfg, control=None):
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

        def gate_model(cfg, control=None):
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
        def late_steer_model(cfg, control=None):
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
        list(daemon.default_step_factory(cfg, control)([]))
        self.assertEqual({t["function"]["name"] for t in seen["tools"]}, set(tools_mod.TOOLS))
        self.assertIs(seen["cancel"], control)


if __name__ == "__main__":
    unittest.main()
