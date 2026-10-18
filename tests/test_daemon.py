"""The daemon end to end over a real unix socket, against a fake model."""
import asyncio
import dataclasses
import json
import os
import shlex
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
from pathfinder.llm import Endpoint, LLMError, Response, anthropic
from pathfinder.mcp import pins as mcp_pins
from pathfinder.signals import Signal
from pathfinder.state import State
from pathfinder.tools import ToolSpec

REF = "anthropic:fake-model"
USAGE = {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
ENDPOINT = Endpoint("http://127.0.0.1:9", "k-fake", "fake-model", api="anthropic")


def text(t):
    return {"type": "text", "text": t}


def call(call_id, name, **args):
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


class FakeRouter:
    """The root's model, scripted: a step is a list of blocks, a Response, an exception to raise, or
    `async step(req, on_text, cancel)`; once the script is spent every step answers `reply`, streamed word
    by word. `seen` holds every Request: the daemon reaches a model only through the clone's router."""

    def __init__(self, reply="Hello there.", *script):
        self.reply, self.script, self.seen = reply, list(script), []

    async def complete(self, req, on_text=None, cancel=None):
        self.seen.append(req)
        step = self.script.pop(0) if self.script else [text(self.reply)]
        if isinstance(step, Exception):
            step.fallbacks = []  # what the real router attaches to every LLMError it raises
            raise step
        if isinstance(step, Response):
            return step
        if callable(step):
            return await step(req, on_text, cancel)
        for block in step:
            for word in block["text"].split(" ") if block["type"] == "text" and on_text else ():
                on_text(word + " ")
        stop = "tool_use" if any(b["type"] == "tool_use" for b in step) else "end_turn"
        return Response(list(step), stop, dict(USAGE), REF, [])


fake_model = FakeRouter


def said(req):
    """(role, text) of a Request's user and assistant messages, their text blocks joined."""
    return [(m["role"], "".join(b.get("text", "") for b in m["content"] if b["type"] == "text"))
            for m in req.messages if m["role"] != "system"]


def residents(req):
    return [m["content"] for m in req.messages if m["role"] == "system"]


def strip_marks(value):
    """A wire body without its cache_control markers (the history breakpoint moves forward by design)."""
    if isinstance(value, dict):
        return {k: strip_marks(v) for k, v in value.items() if k != "cache_control"}
    return [strip_marks(v) for v in value] if isinstance(value, list) else value


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

    def run_with(self, router, scenario):
        async def main():
            daemon = Daemon(self.home, router=router)
            await daemon.start()
            try:
                return await scenario(daemon)
            finally:
                await daemon.close()
        return asyncio.run(main())

    def events(self):
        return list(Ledger(self.home).read())

    def tool(self, name="slow_tool"):
        """A read tool that runs until `proceed` is set or its interrupt fires (at most 1 s); `started` is
        set once it runs."""
        started, proceed = threading.Event(), threading.Event()

        def handler(control=None):
            started.set()
            for _ in range(100):
                if proceed.is_set() or control.interrupt.is_set():
                    break
                time.sleep(0.01)
            return {"done": True}

        tools_mod.TOOLS[name] = ToolSpec(name=name, description="", params={}, required=[], handler=handler)
        self.addCleanup(tools_mod.TOOLS.pop, name, None)
        return started, proceed

    async def wait(self, cond):
        for _ in range(300):
            if cond(self.events()):
                return
            await asyncio.sleep(0.01)
        self.fail("timed out")

    async def say(self, c, words):
        c.send({"type": "say", "text": words})
        return await c.until("turn.end")


class DaemonTest(DaemonHarness):
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
            second = Daemon(self.home, router=fake_model())
            with self.assertRaises(RuntimeError):
                await second.start()
            second.state.close()

        self.run_with(fake_model(), scenario)


def _never_quiet(daemon):
    """Real wall-clock quiet hours (default 23-8) would make a signal-turn test flaky
    depending on when it happens to run - pin it open for the duration of the test."""
    daemon.rules = dataclasses.replace(daemon.rules, quiet_hours="0-0")


class RootTurnTest(DaemonHarness):
    """V11/V22 through the socket: the daemon records a say and hands it to the root clone, whose model is a
    FakeRouter; approve, deny and interrupt reach the clone; restore runs at start."""

    def test_daemon_round_trip(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = await c.recv()
            seen = await self.say(c, "hi")
            c.close()
            return attached, seen

        attached, seen = self.run_with(fake_model("Hello there."), scenario)
        self.assertEqual((attached["type"], attached["resumed"]), ("attached", False))
        self.assertEqual([m["type"] for m in seen], ["turn.start", "delta", "delta", "turn.end"])
        self.assertEqual("".join(m["text"] for m in seen if m["type"] == "delta"), "Hello there. ")
        self.assertEqual(seen[-1]["kind"], "answer")
        events = self.events()
        self.assertEqual([e["type"] for e in events],
                         ["spawn", "session_start", "user", "turn_start", "assistant", "turn_end"])
        user = events[2]
        self.assertEqual((user["origin"], user["trust"], user["text"], user["clone"]), ("user", "user", "hi", "root"))
        self.assertEqual({e["turn"] for e in events[2:]}, {user["turn"]})  # the say carries its item's turn id
        self.assertEqual(events[4]["model"], REF)

    def test_second_turn_sees_the_first(self):
        router = fake_model("ok")

        async def scenario(daemon):
            c = await Client.open(self.home)
            for words in ("first", "second"):
                await self.say(c, words)  # say without attach attaches implicitly
            c.close()

        self.run_with(router, scenario)
        self.assertEqual(said(router.seen[-1]), [("user", "first"), ("assistant", "ok"), ("user", "second")])

    def test_daemon_survives_model_402(self):
        """Every model blocked: the turn ends `error`, the lane lives, and the next say is answered."""
        async def scenario(daemon):
            c = await Client.open(self.home)
            first = await self.say(c, "hi")
            second = await self.say(c, "again")
            c.close()
            return first, second

        first, second = self.run_with(
            fake_model("back", LLMError(402, "this model is not included in your free usage")), scenario)
        self.assertEqual(first[-1]["kind"], "error")
        self.assertTrue(any(m["type"] == "error" and "free usage" in m["message"] for m in first))
        self.assertEqual(second[-1]["kind"], "answer")
        self.assertEqual([(e["status"], e["clone"]) for e in self.events() if e["type"] == "error"], [(402, "root")])

    def test_unset_model_is_an_error_not_a_crash(self):
        """A fresh install has no `models`: the real router says so, and the turn ends `error`."""
        async def scenario(daemon):
            c = await Client.open(self.home)
            seen = await self.say(c, "hi")
            c.close()
            return seen

        seen = self.run_with(None, scenario)  # None = the daemon's own Router, no config.json
        self.assertEqual(seen[-1]["kind"], "error")
        self.assertTrue(any(m["type"] == "error" and "models" in m["message"] for m in seen))

    def test_daemon_resumes_after_state_deleted(self):
        async def talk(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = await c.recv()
            await self.say(c, "remember the word plum")
            c.close()
            return attached

        first = self.run_with(fake_model("noted"), talk)
        for leftover in self.home.glob("state.db*"):
            leftover.unlink()  # the derived state is gone; the ledger is all that is left
        router = fake_model("plum")
        second = self.run_with(router, talk)
        self.assertEqual((second["resumed"], second["session"], second["turns"]), (True, first["session"], 1))
        self.assertEqual(said(router.seen[-1]), [("user", "remember the word plum"), ("assistant", "noted"),
                                                 ("user", "remember the word plum")])
        self.assertEqual([e["type"] for e in self.events()].count("spawn"), 1)  # once per home

    def test_gate_survives_restart_and_its_report_is_a_new_turn(self):
        """V22 end to end: the write is handed off at a gate and the turn ends as any turn; with state.db
        deleted the rebuilt index still lists the gate; approving its seq on a new daemon runs the call once
        and the root reports it in a new turn, which reaches the screen that approved."""
        note = self.home / "note.txt"

        async def gated(daemon):
            c = await Client.open(self.home)
            seen = await self.say(c, "please write a note")
            c.close()
            return seen

        seen = self.run_with(fake_model("I asked for approval.",
                                        [call("c1", "write_file", path=str(note), content="hi")]), gated)
        self.assertEqual(seen[-1]["kind"], "answer")  # a gate never ends a turn
        [line] = [m for m in seen if m["type"] == "gate"]
        [gate] = [e for e in self.events() if e["type"] == "gate"]
        self.assertEqual((line["id"], line["tool"], line["args"]), (gate["seq"], "write_file", gate["args"]))
        self.assertEqual(gate["args"]["content"], "hi")
        self.assertFalse(note.exists())
        for leftover in self.home.glob("state.db*"):
            leftover.unlink()
        state = State(self.home / "state.db")
        state.rebuild(Ledger(self.home))
        listed = [(g["seq"], g["decision"]) for g in state.open_gates()]
        state.close()
        self.assertEqual(listed, [(gate["seq"], None)])

        async def approve(daemon):
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": str(gate["seq"])})
            seen = await c.until("turn.end")
            c.close()
            return seen

        router = fake_model("Written.")
        report = self.run_with(router, approve)
        self.assertEqual(report[-1]["kind"], "answer")
        self.assertEqual("".join(m["text"] for m in report if m["type"] == "delta"), "Written. ")
        self.assertEqual(note.read_text(), "hi")
        events = self.events()
        self.assertEqual([(e["gate"], e["decision"], e["by"]) for e in events if e["type"] == "resume"],
                         [(gate["seq"], "approved", "socket")])
        self.assertEqual([e["ok"] for e in events if e["type"] == "result" and e.get("gate")], [True])
        [opening] = [e for e in events if e["type"] == "user" and e.get("gate")]
        self.assertEqual((opening["gate"], opening["origin"]), ([gate["seq"]], "system"))
        self.assertTrue(said(router.seen[0])[-1][1].startswith(f"gate {gate['seq']} (write_file) approved → "))
        self.assertEqual([e["kind"] for e in events if e["type"] == "turn_end"], ["answer", "answer"])

    def test_deny_reports_the_denial_in_a_new_turn(self):
        note = self.home / "note-denied.txt"
        router = fake_model("ok, not writing it", [call("c1", "write_file", path=str(note), content="hi")],
                            [text("Asked.")])

        async def scenario(daemon):
            c = await Client.open(self.home)
            [line] = [m for m in await self.say(c, "write a note") if m["type"] == "gate"]
            c.send({"type": "deny", "id": line["id"]})
            report = await c.until("turn.end")
            c.send({"type": "deny", "id": line["id"]})
            again = await c.recv()
            c.close()
            return line, report, again

        line, report, again = self.run_with(router, scenario)
        self.assertEqual(report[-1]["kind"], "answer")
        self.assertFalse(note.exists())
        self.assertEqual([(e["gate"], e["decision"]) for e in self.events() if e["type"] == "resume"],
                         [(line["id"], "denied")])
        self.assertEqual(said(router.seen[-1])[-1][1], f"gate {line['id']} (write_file) denied by the user")
        self.assertEqual(again, {"type": "error", "message": f"no pending approval: {line['id']}"})

    def test_an_approved_call_that_raises_still_gets_its_outcome(self):
        """U7 review note 2: a failure past the handler's own catch still records the gate's result and its
        report, so the gate is never left approved with no outcome."""
        router = fake_model("ok", [call("c1", "write_file", path=str(self.home / "n.txt"), content="x")],
                            [text("Asked.")])

        async def scenario(daemon):
            c = await Client.open(self.home)
            [line] = [m for m in await self.say(c, "write it") if m["type"] == "gate"]
            with mock.patch("pathfinder.broker.Broker.call", side_effect=RuntimeError("boom")):
                c.send({"type": "approve", "id": line["id"]})
                seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(router, scenario)
        self.assertEqual(seen[-1]["kind"], "answer")
        self.assertEqual([(e["ok"], e["body"]) for e in self.events() if e["type"] == "result" and e.get("gate")],
                         [(False, "RuntimeError: boom")])
        self.assertIn("approved → failed: ", said(router.seen[-1])[-1][1])

    def test_a_report_turn_reads_its_result_after_a_refused_turn(self):
        """U7 review note 1: the handed-off call's result is recorded under the gate's turn; when that turn
        ended `refused` it is out of the window, yet the report names its r:M, so read_result on it works."""
        note = self.home / "note.txt"

        async def reread(req, on_text, cancel):
            seq = [e for e in self.events() if e["type"] == "result" and e.get("gate")][-1]["seq"]
            return Response([call("c2", "read_result", handle=f"r:{seq}")], "tool_use", dict(USAGE), REF, [])

        router = fake_model("done", [call("c1", "write_file", path=str(note), content="hi")],
                            Response([], "refusal", dict(USAGE), REF, []), reread)

        async def scenario(daemon):
            c = await Client.open(self.home)
            first = await self.say(c, "write it")
            [line] = [m for m in first if m["type"] == "gate"]
            c.send({"type": "approve", "id": line["id"]})
            report = await c.until("turn.end")
            c.close()
            return first, report

        first, report = self.run_with(router, scenario)
        self.assertEqual((first[-1]["kind"], report[-1]["kind"]), ("refused", "answer"))
        self.assertEqual([e["ok"] for e in self.events() if e["type"] == "result" and e["tool"] == "read_result"],
                         [True])

    def test_interrupt_stops_the_running_tool(self):
        started, _ = self.tool("blocking_tool")

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "interrupt"})
            idle = await c.recv()
            c.send({"type": "say", "text": "block"})
            await c.until("turn.start")
            await asyncio.to_thread(started.wait, 2)
            c.send({"type": "interrupt"})
            seen = await c.until("turn.end")
            c.close()
            return idle, seen

        start = time.monotonic()
        idle, seen = self.run_with(fake_model("back", [call("c1", "blocking_tool")]), scenario)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(idle, {"type": "error", "message": "no turn is running"})
        self.assertEqual(seen[-1]["kind"], "interrupted")
        events = self.events()
        self.assertEqual([e["why"] for e in events if e["type"] == "interrupted"], ["user"])
        self.assertEqual([(e["ok"], e["body"]) for e in events if e["type"] == "result"],
                         [(False, "outcome unknown: interrupted by the user while it ran")])

    def test_a_say_during_a_turn_is_a_steer_recorded_at_the_step_boundary(self):
        """Rows 1 and 11: while the root runs this session's turn a say is handed to the clone unrecorded;
        the clone records it right before the next request, which carries it. Every request of the turn (a
        tool, the steer, a gate) extends the previous one on the wire (append-only, U7 review note 5)."""
        started, proceed = self.tool()
        router = fake_model("done", [call("c1", "slow_tool")],
                            [call("c2", "write_file", path=str(self.home / "n.txt"), content="x")])

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "go"})
            await asyncio.to_thread(started.wait, 2)
            c.send({"type": "say", "text": "also this"})
            await asyncio.sleep(0.1)
            early = [e for e in self.events() if e["type"] == "user" and e["text"] == "also this"]
            proceed.set()
            seen = await c.until("turn.end")
            c.close()
            return early, seen

        early, seen = self.run_with(router, scenario)
        self.assertEqual(early, [])  # enqueued for the clone, not recorded, while the tool ran
        self.assertEqual(seen[-1]["kind"], "answer")
        events = self.events()
        [go] = [e for e in events if e["type"] == "user" and e["text"] == "go"]
        [steer] = [e for e in events if e["type"] == "user" and e.get("steer")]
        self.assertEqual((steer["text"], steer["turn"], steer["clone"]), ("also this", go["turn"], "root"))
        self.assertIn("also this", said(router.seen[1])[-1][1])
        self.assertEqual(len(router.seen), 3)
        wires = [strip_marks(anthropic.to_wire(req, ENDPOINT)) for req in router.seen]
        for before, after in zip(wires, wires[1:]):
            self.assertEqual((after.get("system"), after["tools"]), (before.get("system"), before["tools"]))
            self.assertEqual(after["messages"][:len(before["messages"])], before["messages"])

    def test_two_says_queued_behind_a_busy_lane_are_two_turns(self):
        """U8a review: a say's opening `user` is recorded when the root takes it, so says queued while the
        lane is busy (here the resident block is slow to gather) never merge into one turn."""
        router = fake_model("ok")

        def slow_agenda(senses):
            time.sleep(0.2)
            return ""

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "one"})
            await self.wait(lambda events: any(e["type"] == "user" for e in events))
            c.send({"type": "say", "text": "two"})
            c.send({"type": "say", "text": "three"})
            for _ in range(3):
                await c.until("turn.end")
            c.close()

        with mock.patch("pathfinder.signals.context", side_effect=slow_agenda):
            self.run_with(router, scenario)
        self.assertEqual([said(req)[-1] for req in router.seen], [("user", "one"), ("user", "two"), ("user", "three")])
        self.assertEqual([len(residents(req)) for req in router.seen], [1, 2, 3])  # one copy per item
        events = self.events()
        turns = [e["turn"] for e in events if e["type"] == "turn_start"]
        self.assertEqual(len(set(turns)), 3)
        for turn in turns:
            self.assertEqual([e["type"] for e in events if e.get("turn") == turn and e["type"] in ("user", "turn_start")],
                             ["user", "turn_start"])

    def test_every_streamed_delta_is_in_the_ledger(self):
        """§11 invariant 2, on the root: whatever the ending - answer, budget, interrupt, gate, a mid-stream
        error, a fallback after a partial stream - the turn's assistant events hold every delta sent."""
        self.tool("noop")[1].set()  # returns at once
        config.set(self.home, "max_steps", 2)

        def stream(parts, *calls, fallbacks=()):
            async def step(req, on_text, cancel):
                for part in parts:
                    on_text(part)
                return Response([text(parts[-1]), *calls], "tool_use" if calls else "end_turn", dict(USAGE), REF,
                                list(fallbacks))
            return step

        async def hang(req, on_text, cancel):
            on_text("thinking ")
            await asyncio.sleep(5)  # until the interrupt cancels it

        async def broken(req, on_text, cancel):
            on_text("half an ans")
            error = LLMError(500, "the stream broke")
            error.fallbacks = []
            raise error

        noop = call("n", "noop")
        router = fake_model(
            "unused", stream(("Hi there.",)),
            stream(("working ",), noop), stream(("working ",), noop), stream(("no luck.",)),
            hang,
            stream(("I will write it. ",), call("w", "write_file", path=str(self.home / "n.txt"), content="x")),
            stream(("Asked.",)),
            broken,
            stream(("partial ", "full answer"), fallbacks=[{"model": "anthropic:other", "status": 529,
                                                            "message": "overloaded"}]))

        async def scenario(daemon):
            c = await Client.open(self.home)
            turns = []
            for words in ("hi", "loop", "stop", "write", "error", "fallback"):
                c.send({"type": "say", "text": words})
                seen = await c.until("delta") if words == "stop" else []
                if words == "stop":
                    c.send({"type": "interrupt"})
                turns.append(seen + await c.until("turn.end"))
            c.close()
            return turns

        turns = self.run_with(router, scenario)
        self.assertEqual([seen[-1]["kind"] for seen in turns],
                         ["answer", "budget", "interrupted", "answer", "error", "answer"])
        for seen in turns:
            turn = next(m for m in seen if m["type"] == "turn.start")["turn"]
            streamed = "".join(m["text"] for m in seen if m["type"] == "delta")
            recorded = "".join(e["text"] for e in self.events() if e["type"] == "assistant" and e["turn"] == turn)
            self.assertTrue(streamed)
            self.assertEqual(streamed, recorded)

    def test_report_turn_with_no_screen_lands_as_a_banner(self):
        """A report turn no screen sees (here: the gate approved after a restart from a connection that closes at
        once) is delivered as a scheduled turn is: one banner says it ran, a long report line clipped."""
        blocker = self.home / ("x" * 200)  # a file where the write needs a folder: the error holds the long path
        blocker.write_text("")
        note = blocker / "note.txt"
        banners = []

        async def gated(daemon):
            c = await Client.open(self.home)
            await self.say(c, "please write a note")
            c.close()

        self.run_with(fake_model("I asked for approval.",
                                 [call("c1", "write_file", path=str(note), content="hi")]), gated)
        [gate] = [e for e in self.events() if e["type"] == "gate"]

        async def approve(daemon):  # a new daemon: the gate survived the restart
            _never_quiet(daemon)
            daemon.outbox.banner = lambda text: banners.append(text) or True
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": gate["seq"]})
            c.close()  # gone long before the report turn ends
            await self.wait(lambda events: any(e["type"] == "delivery" for e in events))

        self.run_with(fake_model("Written."), approve)
        events = self.events()
        [opening] = [e for e in events if e["type"] == "user" and e.get("gate")]
        self.assertEqual([e["ok"] for e in events if e["type"] == "result" and e.get("gate")], [False])
        self.assertGreater(len(opening["text"]), 250)  # long enough to be clipped
        self.assertEqual(banners, [f"{opening['text'][:200]} (answer)"])
        self.assertLessEqual(len(banners[0]), 210)
        self.assertEqual([e["via"] for e in events if e["type"] == "delivery"], ["banner"])

    def test_report_turn_seen_by_the_approving_screen_sends_no_banner(self):
        """The connection that approves stays attached (the TUI, the CLI waiting for turn.end): it shows the
        report turn as it runs, so no banner."""
        note = self.home / "note.txt"
        banners = []

        async def gated(daemon):
            c = await Client.open(self.home)
            await self.say(c, "please write a note")
            c.close()

        self.run_with(fake_model("I asked for approval.",
                                 [call("c1", "write_file", path=str(note), content="hi")]), gated)
        [gate] = [e for e in self.events() if e["type"] == "gate"]

        async def approve(daemon):
            daemon.outbox.banner = lambda text: banners.append(text) or True
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": gate["seq"]})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(fake_model("Written."), approve)
        self.assertEqual(seen[-1]["kind"], "answer")
        self.assertEqual(note.read_text(), "hi")
        self.assertEqual(banners, [])
        self.assertEqual([e for e in self.events() if e["type"] == "delivery"], [])

    def test_close_mid_turn_records_nothing_for_the_root_and_restore_closes_it(self):
        """V12: a shutdown cancels the root's task and the clone records nothing; the next start's restore
        closes the turn as interrupted by a restart."""
        started, _ = self.tool()

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "go"})
            await asyncio.to_thread(started.wait, 2)
            c.close()
            return self.events()[-1]

        last = self.run_with(fake_model("unused", [call("c1", "slow_tool")]), scenario)
        self.assertEqual(last["type"], "assistant")  # the step whose call was running
        self.assertEqual([e for e in self.events() if e["seq"] > last["seq"]], [])  # close() wrote nothing
        self.run_with(fake_model(), lambda daemon: _noop())
        self.assertEqual([(e["type"], e.get("why") or e.get("kind")) for e in self.events() if e["seq"] > last["seq"]],
                         [("interrupted", "restart"), ("turn_end", "interrupted")])

    def test_two_gates_with_the_same_call_id_are_both_resolvable(self):
        """A model-chosen call id is not a key (partition 2 review): each gate is its own seq."""
        paths = [self.home / "a.txt", self.home / "b.txt"]
        router = fake_model("done", [call("call_0", "write_file", path=str(paths[0]), content="0")], [text("Asked.")],
                            [call("call_0", "write_file", path=str(paths[1]), content="1")], [text("Asked.")])

        async def scenario(daemon):
            c = await Client.open(self.home)
            ids = []
            for words in ("write a", "write b"):
                ids += [m["id"] for m in await self.say(c, words) if m["type"] == "gate"]
            for id in reversed(ids):  # the one the user is looking at first
                c.send({"type": "approve", "id": id})
                await c.until("turn.end")
            c.close()
            return ids

        ids = self.run_with(router, scenario)
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual([p.read_text() for p in paths], ["0", "1"])
        self.assertEqual(sorted(e["gate"] for e in self.events() if e["type"] == "result" and e.get("gate")),
                         sorted(ids))

    def test_approve_ids_are_ledger_seqs(self):
        """One namespace: a proposal's gate line carries its `approval_pending` seq (a root gate's line its
        `gate` seq, above), and `approve <seq>` resolves it."""
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            sid = (await c.recv())["session"]
            proposal = daemon.record("approval_pending", session=sid, origin="user", id="p1", call_id="p1",
                                     tool="edit_memory", messages=[],
                                     args={"target": "USER.md", "op": "add", "old": "", "new": "Likes tea.",
                                           "evidence": [1]})
            line = await c.recv()
            c.send({"type": "approve", "id": str(proposal["seq"])})
            reply = await c.recv()
            c.close()
            return proposal, line, reply

        proposal, line, reply = self.run_with(fake_model(), scenario)
        self.assertEqual((line["type"], line["id"]), ("gate", proposal["seq"]))
        self.assertEqual(reply, {"type": "turn.end", "turn": "", "kind": "resumed"})
        self.assertIn("Likes tea.", (self.home / "USER.md").read_text(encoding="utf-8"))

    def test_an_all_digit_v1_id_still_resolves(self):
        """U8a review: a v1 hex id can be all digits; no seq by that number means it is a v1 id."""
        async def scenario(daemon):
            daemon.record("approval_pending", session="s1", origin="memory", id="123456789012", call_id="x",
                          tool="edit_memory", messages=[],
                          args={"target": "USER.md", "op": "add", "old": "", "new": "Likes tea.", "evidence": [1]})
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": "123456789012"})
            reply = await c.recv()
            c.close()
            return reply

        self.assertEqual(self.run_with(fake_model(), scenario), {"type": "turn.end", "turn": "", "kind": "resumed"})
        self.assertIn("Likes tea.", (self.home / "USER.md").read_text(encoding="utf-8"))

    def test_resident_block_is_frozen_and_carries_time_user_md_map_and_agenda(self):
        """Piece 3, gathered once per item and replayed byte-identical; piece 1 the same across turns."""
        router = fake_model("ok")

        async def scenario(daemon):
            user_path = self.home / "USER.md"
            user_path.write_text(user_path.read_text(encoding="utf-8") + "\nLikes tea.\n", encoding="utf-8")
            (self.home / "wiki").mkdir(exist_ok=True)
            (self.home / "wiki" / "coffee.md").write_text("# Coffee\nGood in the morning.\n", encoding="utf-8")
            daemon.memory_mgr.index("wiki/coffee.md")
            c = await Client.open(self.home)
            for words in ("one", "two"):
                await self.say(c, words)
            c.close()

        with mock.patch("pathfinder.signals.context", return_value="AGENDA-LINE"):  # D42
            self.run_with(router, scenario)
        first, second = router.seen
        self.assertEqual(first.system, second.system)
        resident = residents(first)[0]
        self.assertEqual(residents(second)[0], resident)  # the first item's copy, as it was
        self.assertTrue(resident.startswith("Local time now:"), resident)
        for part in ("Likes tea.", "Coffee", "AGENDA-LINE"):
            self.assertIn(part, resident)

    def test_schedule_due_runs_as_a_root_item_and_can_gate(self):
        """The owner's scheduled note is a root item of their own, not a signal: the write it leads to is
        handed off, approved by seq and reported; with no screen, one banner says the item ran."""
        from pathfinder.senses import schedule
        note = self.home / "scheduled-note.txt"
        banners = []
        router = fake_model("done", [call("c1", "write_file", path=str(note), content="brief")],
                            [text("Asked for approval.")])

        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.outbox.banner = lambda text: banners.append(text) or True
            config.set(self.home, "schedules", [{"id": "b1", "at": "2026-09-22 09:00", "repeat": "none",
                                                 "text": "write the daily brief"}])
            for sig in schedule.poll({"_home": self.home}, time.time()):
                daemon.push_signal(sig)
            await self.wait(lambda events: any(e["type"] == "delivery" for e in events))
            gate = next(e for e in self.events() if e["type"] == "gate")
            c = await Client.open(self.home)
            c.send({"type": "approve", "id": gate["seq"]})
            await c.until("turn.end")
            c.close()

        self.run_with(router, scenario)
        events = self.events()
        user = next(e for e in events if e["type"] == "user")
        self.assertEqual((user["origin"], user["trust"], user["text"]),
                         ("user", "user", "[scheduled] write the daily brief"))
        self.assertEqual([(e["origin"], e["kind"]) for e in events if e["type"] == "turn_end"],
                         [("user", "answer"), ("user", "answer")])
        self.assertEqual(note.read_text(), "brief")
        self.assertEqual(banners, ["[scheduled] write the daily brief (answer)"])
        self.assertEqual([e["via"] for e in events if e["type"] == "delivery"], ["banner"])

    def test_schedule_turn_with_a_screen_attached_sends_no_banner(self):
        """An attached screen already shows the scheduled turn as it runs - no extra notice."""
        from pathfinder.senses import schedule
        banners = []

        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.outbox.banner = lambda text: banners.append(text) or True
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.until("attached")
            config.set(self.home, "schedules", [{"id": "b1", "at": "2026-09-22 09:00", "repeat": "none",
                                                 "text": "say hello"}])
            for sig in schedule.poll({"_home": self.home}, time.time()):
                daemon.push_signal(sig)
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(fake_model("hello"), scenario)
        self.assertEqual(seen[-1]["kind"], "answer")
        self.assertEqual(banners, [])
        self.assertEqual([e for e in self.events() if e["type"] == "delivery"], [])

    def test_a_spoken_signal_is_its_summary_without_a_model_request(self):
        """P1: a `speak` set goes out as the senses' summaries and reaches history; the router is never
        asked (no signal model turn until attention, P5)."""
        router = fake_model()

        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.outbox.banner = lambda text: True
            daemon.push_signal(Signal(sense="test", kind="schedule_due", key="sig-summary",
                                      summary="Scheduled note: ping",
                                      fields={"id": "s1", "text": "ping", "at": "2026-09-22 09:00"}, trust="user"))
            await self.wait(lambda events: any(e["type"] == "delivery" for e in events))

        self.run_with(router, scenario)
        delivery = next(e for e in self.events() if e["type"] == "delivery")
        state = State(self.home / "state.db")
        history = state.history(delivery["session"])
        state.close()
        self.assertEqual([m["content"] for m in history if m["role"] == "assistant"], ["Scheduled note: ping"])
        self.assertEqual(router.seen, [])

    def test_deliver_holds_while_turn_runs(self):
        """A notice for the session whose turn the root runs is held; it arrives after `turn.end`."""
        started, proceed = self.tool()

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "go"})
            await asyncio.to_thread(started.wait, 2)
            daemon.outbox.enqueue(Notice("n1", daemon.state.clone_row("root")["session"], "held text", "w", []))
            await asyncio.sleep(0.05)
            held = len(daemon.outbox.queue)
            proceed.set()
            seen = await c.until("turn.end")
            notice = await c.recv()  # arrives after turn.end, not before
            c.close()
            return held, seen, notice

        held, seen, notice = self.run_with(fake_model("done", [call("c1", "slow_tool")]), scenario)
        self.assertEqual(held, 1)  # still held while the turn ran
        self.assertEqual(seen[-1]["type"], "turn.end")
        self.assertEqual(notice, {"type": "notice", "id": "n1", "text": "held text", "why": "w"})

    def test_a_gated_edit_memory_runs_through_its_handler_on_approve(self):
        """B3 on the root: grants {"edit_memory": "ask"} hands the model's own edit_memory call off at a gate
        (not a Manager proposal); approving its seq writes through the handler, not memory_mgr.decide."""
        config.set(self.home, "grants", {"edit_memory": "ask"})
        router = fake_model("done", [call("c1", "edit_memory", target="wiki/Test.md", op="add", old="",
                                          new="# Test\nline one")], [text("Asked.")])

        async def scenario(daemon):
            c = await Client.open(self.home)
            [line] = [m for m in await self.say(c, "please remember this") if m["type"] == "gate"]
            c.send({"type": "approve", "id": line["id"]})
            await c.until("turn.end")
            c.close()
            return line

        line = self.run_with(router, scenario)
        self.assertEqual(line["tool"], "edit_memory")
        self.assertEqual([e for e in self.events() if e["type"] == "approval_pending"], [])
        self.assertIn("line one", (self.home / "wiki" / "Test.md").read_text(encoding="utf-8"))
        applies = [e for e in self.events() if e["type"] == "memory_apply"]
        self.assertEqual(len(applies), 1)
        self.assertIsNone(applies[0].get("approval"))  # written through the handler, not the approval box

    def test_mcp_tool_call_in_a_turn(self):
        config.set(self.home, "mcp_servers",
                   {"fake": {"transport": "stdio", "command": [sys.executable, str(FAKE_SERVER)]}})

        async def scenario(daemon):
            c = await Client.open(self.home)
            seen = await self.say(c, "echo hi")
            c.close()
            return seen

        config.set(self.home, "grants", {"mcp__fake__echo": "allow"})
        seen = self.run_with(fake_model("done", [call("c1", "mcp__fake__echo", text="hi")]), scenario)
        self.assertEqual(seen[-1]["kind"], "answer")
        self.assertTrue(any(e["tool"] == "mcp__fake__echo" and e["ok"] and "hi" in json.dumps(e["body"])
                            for e in self.events() if e["type"] == "result"))
        config.set(self.home, "grants", {})
        seen = self.run_with(fake_model("asked", [call("c1", "mcp__fake__echo", text="hi")]), scenario)
        self.assertEqual([m["tool"] for m in seen if m["type"] == "gate"], ["mcp__fake__echo"])


class DeliverTest(DaemonHarness):
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
            daemon.push_signal(Signal(sense="test", kind="schedule_due", key="k-hold",
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

    def test_judge_stub_silences_an_unsettled_signal(self):
        """v2 partition 1 (Q2 = a): no model triage until partition 5 - a signal no rule settles gets the
        stub's judgment and is silenced, and no request reaches the router."""
        async def scenario(daemon):
            _never_quiet(daemon)
            daemon.push_signal(Signal(sense="mail", kind="kind_with_no_rules_section",
                                      key="stub-test", summary="s", fields={}, trust="third_party"))
            for _ in range(200):
                if any(e["type"] == "decision" for e in Ledger(self.home).read()):
                    break
                await asyncio.sleep(0.01)
            return list(Ledger(self.home).read())

        model = fake_model()
        events = self.run_with(model, scenario)
        triage = [(e["type"], e.get("speak"), e.get("reason"), e.get("action"), e.get("why")) for e in events
                  if e["type"] in ("judgment", "decision") and e.get("signal_key") == "stub-test"]
        self.assertEqual(triage, [("judgment", False, "model triage returns in partition 5", None, None),
                                  ("decision", None, None, "silence", "judged")])
        self.assertEqual(model.seen, [])


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
            daemon.push_signal(Signal(sense="test", kind="schedule_due", key="k-hold",
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
            await daemon.triage(s, bundle)
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
            daemon = Daemon(self.home, router=fake_model("ok"))
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
        """`jobs` is no sense (v2 partition 1 removed it), so a `signal jobs ...` line from
        outside is refused like any unknown or non-push sense."""
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


class MemoryDaemonTest(DaemonHarness):
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

    def test_socket_refuses_signal_memory(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "signal", "sense": "memory", "fields": {}})
            reply = await c.recv()
            c.close()
            return reply

        reply = self.run_with(fake_model(), scenario)
        self.assertEqual(reply["type"], "error")


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

    def test_secret_missing_records_one_error(self):
        config.set(self.home, "mcp_servers", {"gh": {
            "transport": "stdio", "command": ["true"], "env": {"TOKEN": "keychain:bot"}}})

        with mock.patch("pathfinder.keychain.subprocess.run", side_effect=FileNotFoundError):
            snapshot = self.run_with(fake_model("ok"), _snapshot)

        self.assertFalse(any(name.startswith("mcp__gh__") for name in snapshot))
        errors = [e for e in self.events() if e["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("secret missing", errors[0]["message"])


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
            proposal = daemon.record("approval_pending", session=sid, origin="user", id="a1", call_id="a1",
                                     tool="install_server", args=cand, messages=[])
            line = await c.recv()
            c.close()
            return line, proposal["seq"]

        line, seq = self.run_with(fake_model("ok"), scenario)
        self.assertEqual((line["type"], line["id"], line["tool"]), ("gate", seq, "install_server"))
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

    def test_gate_line_shows_what_the_approval_holds(self):
        """A root gate's line carries its recorded args, a proposal's too; both match what `approve <seq>`
        resolves."""
        router = fake_model("asked", [call("c1", "write_file", path=str(self.home / "note.txt"), content=self.TOKEN)])

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            sid = (await c.recv())["session"]
            seen = await self.say(c, "please write a note")
            daemon.record("approval_pending", session=sid, origin="user", id="a2", call_id="a2",
                          tool="write_file", args={"content": self.TOKEN}, messages=[])
            seen.append(await c.recv())
            c.close()
            return seen

        seen = self.run_with(router, scenario)
        gates = [m for m in seen if m["type"] == "gate"]
        self.assertEqual([g["args"]["content"] for g in gates], ["[redacted]", "[redacted]"])
        self.assertEqual(self.items, {})  # no Keychain call for a call's arguments
        self.assertNotIn(self.TOKEN, json.dumps(seen))
        state = State(self.home / "state.db")
        self.addCleanup(state.close)
        for gate in gates:
            self.assertEqual(state.approval_item(gate["id"])["args"], gate["args"])

    def test_steer_is_kept_as_recorded(self):
        """A steer is recorded by the clone through the ledger's door, so the vault keeps the token and the
        next request carries its handle."""
        started, proceed = self.tool()
        router = fake_model("done", [call("c1", "slow_tool")])

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "go"})
            await asyncio.to_thread(started.wait, 2)
            c.send({"type": "say", "text": f"use {self.TOKEN}"})
            await asyncio.sleep(0.1)
            proceed.set()
            await c.until("turn.end")
            c.close()

        self.run_with(router, scenario)
        [steer] = [e["text"] for e in self.events() if e["type"] == "user" and e.get("steer")]
        self.assertRegex(steer, r"^use \[secret:pasted-\d+\]$")
        self.assertIn(steer, said(router.seen[1])[-1][1])
        self.assertNotIn(self.TOKEN, json.dumps([said(req) for req in router.seen]))


class SecretAskTest(DaemonHarness):
    """U16, D53's main path: the harness asks every screen for a missing secret, and the masked
    answer goes to the Keychain through the daemon - never the ledger, the state or another line.
    `security` is a fake over a dict at the keychain module's `subprocess.run`."""
    VALUE = "hunter2-" + "v" * 12  # not token-shaped: a leak would show raw, not as a handle

    def setUp(self):
        super().setUp()
        self.items = {}
        patch = mock.patch("pathfinder.keychain.subprocess.run", side_effect=self._security)
        patch.start()
        self.addCleanup(patch.stop)

    def _security(self, argv, input=None, **kw):
        if argv[1] == "-i":  # add-generic-password -U -s "..." -a "..." -w "..."
            cmd = shlex.split(input)
            self.items[(cmd[cmd.index("-s") + 1], cmd[cmd.index("-a") + 1])] = cmd[cmd.index("-w") + 1]
            return mock.Mock(returncode=0, stdout="", stderr="")
        key = (argv[argv.index("-s") + 1], argv[argv.index("-a") + 1])
        found = key in self.items
        return mock.Mock(returncode=0 if found else 44, stdout=self.items.get(key, "") + "\n", stderr="")

    @staticmethod
    def _stopped(daemon, names=("NOTION_TOKEN", "WORKSPACE")):
        """What `check` hands `propose` at D40's stop. Its id is `notion-2`, not what `_server_id`
        would give now: the ask stores under the check's own id."""
        daemon.installer.propose({"outcome": "install_failed", "name": "io.example/notion", "verification": {
            "names": list(names), "server_id": "notion-2", "failed_step": "secrets",
            "reason": "needs NOTION_TOKEN, WORKSPACE - store each with /secret notion-2 NAME in the screen"}}, "user")

    def test_secret_ask_reaches_every_screen(self):
        async def scenario(daemon):
            a, b = await Client.open(self.home), await Client.open(self.home)
            for c in (a, b):
                c.send({"type": "attach"})
                await c.recv()
            daemon.installer.propose_by_name = lambda *a, **k: {"id": "", "line": ""}
            self._stopped(daemon)
            asks = [(await a.until("secret.ask"))[-1], (await b.until("secret.ask"))[-1]]
            a.send({**{k: asks[0][k] for k in ("server_id", "name", "server")}, "type": "secret", "value": self.VALUE})
            stored = [(await a.until("secret.stored"))[-1], (await b.until("secret.stored"))[-1]]
            a.close()
            b.close()
            return asks, stored

        asks, stored = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(asks, [{"type": "secret.ask", "server_id": "notion-2",
                                 "name": "NOTION_TOKEN", "server": "io.example/notion"}] * 2)
        # every screen hears the answer, so the one that did not answer leaves its entry
        self.assertEqual(stored, [{"type": "secret.stored", "server_id": "notion-2", "name": "NOTION_TOKEN",
                                   "server": "io.example/notion"}] * 2)
        self.assertEqual(self.items, {("pathfinder-mcp-notion-2", "NOTION_TOKEN"): self.VALUE})

    def test_secret_ask_without_a_screen_falls_back_to_the_line(self):
        async def scenario(daemon):
            def propose(name, origin="user", session="", registry=None):
                daemon.installer.record("install_failed", id="", name=name, names=["NOTION_TOKEN"], step="secrets",
                                        reason="needs NOTION_TOKEN - store it with /secret notion NOTION_TOKEN")
                return {"error": "needs NOTION_TOKEN - store it with /secret notion NOTION_TOKEN"}
            daemon.installer.propose_by_name = propose
            c = await Client.open(self.home)  # `pathfinder install`: a client that never attached
            c.send({"type": "install", "name": "io.example/notion"})
            reply = await c.recv()
            c.send({"type": "nothing"})
            after = await c.recv()
            c.close()
            return reply, after

        reply, after = self.run_with(fake_model("ok"), scenario)
        ask = {"type": "secret.ask", "server_id": "notion", "name": "NOTION_TOKEN", "server": "x"}
        self.assertIn("/secret notion NOTION_TOKEN in the screen", tui.render(ask))  # what `say` prints
        self.assertIn("/secret mail", tui.render({**ask, "server_id": "", "name": "mail"}))
        self.assertEqual(reply["type"], "error")
        self.assertIn("/secret notion NOTION_TOKEN", reply["message"])
        self.assertEqual(after["type"], "error")  # the next line is the next reply: no ask came between

    def test_secret_answer_is_checked_and_mail_needs_no_name(self):
        """Review: a field missing or of the wrong type is refused before anything is stored; `mail`
        with no name is the mail password, and the reply names what it stored."""
        config.set(self.home, "senses", {"mail": {"host": "imap.example.invalid", "user": "mailbox-user"}})

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.recv()
            replies = []
            for bad in ({"server_id": "notion", "name": "N"}, {"server_id": "notion", "name": 5, "value": "x"},
                        {"name": "N", "value": "x"}):
                c.send({"type": "secret", **bad})
                replies.append((await c.until("error"))[-1])
            c.send({"type": "secret", "server_id": "mail", "value": self.VALUE})
            replies.append((await c.until("secret.stored"))[-1])
            c.close()
            return replies

        *errors, stored = self.run_with(fake_model("ok"), scenario)
        self.assertTrue(all(e["message"].startswith("secret needs") for e in errors))  # refused first; nothing stored
        self.assertEqual(stored, {"type": "secret.stored", "server_id": "mail", "name": None, "server": ""})
        self.assertEqual(tui.render(stored), "저장됨 · mail password stored")
        self.assertEqual(self.items, {("pathfinder-mail", "mailbox-user"): self.VALUE})

    def test_stop_during_a_reproposal_cancels_it_promptly(self):
        """Review: a stop does not wait out the re-proposal's trial, nor close state.db under it."""
        release = threading.Event()

        async def main():
            daemon = Daemon(self.home, router=fake_model("ok"))
            await daemon.start()

            def propose(name, origin="user", session="", registry=None):
                release.wait(5)  # a slow trial
                return daemon.state.installed_names()
            daemon.installer.propose_by_name = propose
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.recv()
            self._stopped(daemon)
            c.send({"type": "secret", "server_id": "notion-2", "name": "NOTION_TOKEN", "value": self.VALUE})
            await c.until("secret.stored")
            c.close()
            began = time.monotonic()
            await daemon.close()
            took = time.monotonic() - began
            release.set()
            return took, daemon.installer._again.cancelled()

        took, cancelled = asyncio.run(main())
        self.assertLess(took, 2)
        self.assertTrue(cancelled)

    def test_a_bad_answer_errs_only_to_its_sender(self):
        """Round 2: only `secret.stored` reaches every screen; an error is the sender's alone, so
        another screen - or a `say` in flight - is never told of it."""
        async def scenario(daemon):
            a, b = await Client.open(self.home), await Client.open(self.home)
            for c in (a, b):
                c.send({"type": "attach"})
                await c.recv()
            a.send({"type": "secret", "server_id": "notion", "name": "NOTION_TOKEN"})  # no value
            to_a = (await a.until("error"))[-1]
            b.send({"type": "nothing"})
            to_b = await b.recv()
            a.close()
            b.close()
            return to_a, to_b

        to_a, to_b = self.run_with(fake_model("ok"), scenario)
        self.assertTrue(to_a["message"].startswith("secret needs"))
        self.assertEqual(to_b, {"type": "error", "message": "unknown type: 'nothing'"})

    def test_a_secret_for_a_pending_install_proposes_it_again(self):
        """Round 2: `/secret <id> NAME` typed in the screen after its ask was dropped reaches the same
        path: the install waiting on that id is proposed again, once."""
        async def scenario(daemon):
            proposed = []
            daemon.installer.propose_by_name = lambda name, origin="user", session="", registry=None: \
                proposed.append((name, session)) or {}
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            sid = (await c.recv())["session"]
            self._stopped(daemon)
            await c.until("secret.ask")
            replies = []
            for value in ("v4lue-one", "v4lue-two"):  # what the screen's `/secret notion-2 NOTION_TOKEN` sends
                c.send({"type": "secret", "server_id": "notion-2", "name": "NOTION_TOKEN", "value": value})
                replies.append((await c.until("secret.stored"))[-1])
                await daemon.installer._again
            c.close()
            return sid, proposed, replies

        sid, proposed, replies = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(proposed, [("io.example/notion", sid)])  # once: the second finds nothing waiting
        self.assertEqual([r["server"] for r in replies], ["io.example/notion", ""])

    def test_missing_mail_password_is_asked_at_start(self):
        """U26: at a start no screen is attached, so a screen is asked as it attaches while the mail
        sense is on and its item is missing; once stored, it is not asked again."""
        config.set(self.home, "senses", {"mail": {"host": "imap.example.invalid", "user": "mailbox-user"}})

        async def scenario(daemon):
            seen = []
            for _ in range(2):
                c = await Client.open(self.home)
                c.send({"type": "attach"})
                await c.recv()
                c.send({"type": "nothing"})
                seen.append(await c.until("error"))
                c.close()
                self.items[("pathfinder-mail", "mailbox-user")] = "stored-" + "x" * 8
            return seen

        first, second = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(first[0], {"type": "secret.ask", "server_id": "", "name": "mail", "server": ""})
        self.assertNotIn("secret.ask", [m["type"] for m in second])

    def test_secret_value_never_reaches_ledger_or_state(self):
        async def scenario(daemon):
            proposed = []

            def propose(name, origin="user", session="", registry=None):
                proposed.append((name, origin, session))
                daemon.installer.record("approval_pending", session=session, origin="user", id="a1", call_id="a1",
                                        tool="write_file", args={"path": "x"}, messages=[])
                return {"id": "a1", "line": "x"}
            daemon.installer.propose_by_name = propose
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            sid = (await c.recv())["session"]
            self._stopped(daemon)
            ask = (await c.until("secret.ask"))[-1]
            c.send({**{k: ask[k] for k in ("server_id", "name", "server")}, "type": "secret", "value": self.VALUE})
            seen = await c.until("gate")
            c.close()
            return sid, proposed, seen

        sid, proposed, seen = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(self.items, {("pathfinder-mcp-notion-2", "NOTION_TOKEN"): self.VALUE})
        self.assertIn({"type": "secret.stored", "server_id": "notion-2", "name": "NOTION_TOKEN",
                       "server": "io.example/notion"}, seen)
        self.assertEqual(proposed, [("io.example/notion", "user", sid)])  # proposed again once, in this screen's session
        self.assertNotIn(self.VALUE, json.dumps(seen))
        for path in self.home.rglob("*"):  # the ledger, state.db and its journal, every other file
            if path.is_file():
                self.assertNotIn(self.VALUE.encode(), path.read_bytes(), path.name)


class ChatApprovalTest(DaemonHarness):
    """D52 part 2 (m10's T68): "I approve" typed into chat was taken by the model for an approval. On the
    root, an open gate is its own `gate` event (listed by seq) or a v1 proposal (by its id)."""

    def gate_model(self, reply="ok"):
        """A turn whose write_file call is handed off at a gate, then `reply` for every later step."""
        return fake_model(reply, [call("c1", "write_file", path=str(self.home / "n.txt"), content="x")],
                          [text("Asked.")])

    async def _say(self, c, words):
        return [m for m in await self.say(c, words) if m["type"] != "gate"]

    async def _attach(self):
        c = await Client.open(self.home)
        c.send({"type": "attach"})
        return c, (await c.recv())["session"]

    def test_chat_approval_answered_without_a_model_call(self):
        router = self.gate_model()

        async def scenario(daemon):
            c, sid = await self._attach()
            [gate] = [m["id"] for m in await self.say(c, "write it") if m["type"] == "gate"]
            daemon.record("approval_pending", session=sid, turn="t0", origin="memory", id="a1", call_id="a1",
                          tool="edit_memory", args={"target": "USER.md"}, messages=[])
            requests = len(router.seen)
            seen = await self._say(c, "I'll approve.")
            c.close()
            return gate, requests, seen, daemon.state.gate(gate)["decision"]

        gate, requests, seen, decision = self.run_with(router, scenario)
        self.assertEqual([m["type"] for m in seen], ["turn.start", "delta", "turn.end"])
        self.assertEqual(seen[1]["text"].splitlines(), [
            f"승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve {gate} or /deny {gate} "
            f"(write_file; shell: pathfinder approve|deny {gate})",
            "승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve a1 or /deny a1 (edit_memory; "
            "shell: pathfinder approve|deny a1) - if you meant something else, say it again · 다른 뜻이었다면 다시 "
            "말해 주세요"])
        self.assertEqual(len(router.seen), requests)  # no model request
        self.assertIsNone(decision)  # still open
        turn = [e for e in self.events() if e.get("turn") == seen[0]["turn"]]
        self.assertEqual([e["type"] for e in turn], ["user", "turn_start", "assistant", "turn_end"])
        self.assertFalse(any("clone" in e for e in turn))  # the harness's line, never the root's window
        self.assertEqual((turn[0]["text"], turn[2]["text"], turn[3]["kind"]), ("I'll approve.", seen[1]["text"], "answer"))

    def test_chat_approval_needs_an_open_gate_newer_than_the_last_user_message(self):
        router = fake_model("ok", [text("ok")], [call("c1", "write_file", path=str(self.home / "a.txt"), content="x")],
                            [text("Asked.")], [text("ok")], [text("ok")],
                            [call("c2", "write_file", path=str(self.home / "b.txt"), content="y")], [text("Asked.")])

        async def scenario(daemon):
            c, sid = await self._attach()
            no_gate = await self._say(c, "yes")
            old = [m["id"] for m in await self.say(c, "write a") if m["type"] == "gate"]
            await self._say(c, "something else first")
            talked_past = await self._say(c, "yes")
            new = [m["id"] for m in await self.say(c, "write b") if m["type"] == "gate"]
            answered = await self._say(c, "yes")
            c.close()
            return old, new, no_gate, talked_past, answered

        [old], [new], no_gate, talked_past, answered = self.run_with(router, scenario)
        self.assertEqual(len(router.seen), 7)  # the answered "yes" made no request
        for seen in (no_gate, talked_past):
            self.assertEqual([m["text"] for m in seen if m["type"] == "delta"], ["ok "])
        self.assertIn(f"/approve {new} ", answered[1]["text"])
        self.assertNotIn(f"/approve {old} ", answered[1]["text"])

    def test_longer_message_still_reaches_the_model(self):
        router = self.gate_model()

        async def scenario(daemon):
            c, sid = await self._attach()
            [gate] = [m["id"] for m in await self.say(c, "write it") if m["type"] == "gate"]
            seen = await self._say(c, "yes, and also add X")
            c.close()
            return seen, daemon.state.gate(gate)["decision"]

        seen, decision = self.run_with(router, scenario)
        self.assertEqual(said(router.seen[-1])[-1], ("user", "yes, and also add X"))
        self.assertEqual([m["text"] for m in seen if m["type"] == "delta"], ["ok "])
        self.assertIsNone(decision)

    def test_an_approval_phrase_behind_a_queued_say_reaches_the_model(self):
        """U8a review: the phrase is checked when the lane takes it, after every say queued ahead of it is
        recorded, so a "yes" behind "do X instead" has talked past the gate (as in v1)."""
        router = self.gate_model()
        judging = threading.Event()

        def slow_judge(sig, cfg):
            judging.set()
            time.sleep(0.3)
            return False, "not now"

        async def scenario(daemon):
            _never_quiet(daemon)
            c, sid = await self._attach()
            await self.say(c, "write it")  # the gate is open
            daemon.push_signal(Signal(sense="mail", kind="kind_with_no_rules_section", key="busy", summary="s",
                                      fields={}, trust="third_party"))
            await asyncio.to_thread(judging.wait, 2)  # the lane is busy triaging
            c.send({"type": "say", "text": "do X instead"})
            c.send({"type": "say", "text": "yes"})
            turns = [await c.until("turn.end") for _ in range(2)]
            c.close()
            return turns

        with mock.patch("pathfinder.daemon.rules_mod.judge", side_effect=slow_judge):
            first, second = self.run_with(router, scenario)
        self.assertTrue(judging.is_set())
        self.assertEqual([m["text"] for m in second if m["type"] == "delta"], ["ok "])
        self.assertEqual(said(router.seen[-1])[-1], ("user", "yes"))
        self.assertFalse(any("/approve" in e.get("text", "") for e in self.events() if e["type"] == "assistant"))

    def test_steer_is_never_intercepted(self):
        """D17: a `say` while the owner's turn runs is a steer, with a gate open or not."""
        started, proceed = self.tool()
        router = fake_model("done", [call("c1", "write_file", path=str(self.home / "n.txt"), content="x"),
                                     call("c2", "slow_tool")])

        async def scenario(daemon):
            c, sid = await self._attach()
            c.send({"type": "say", "text": "go"})
            await asyncio.to_thread(started.wait, 2)  # the gate is open by now: newer than "go"
            c.send({"type": "say", "text": "yes"})
            await asyncio.sleep(0.1)
            proceed.set()
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(router, scenario)
        self.assertEqual([m["text"] for m in seen if m["type"] == "delta"], ["done "])
        self.assertIn("yes", said(router.seen[1])[-1][1])
        self.assertEqual([e["text"] for e in self.events() if e["type"] == "user" and e.get("steer")], ["yes"])
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
            daemon.push_signal(Signal(sense="test", kind="schedule_due", key="gap-1",
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
            daemon.push_signal(Signal(sense="test", kind="schedule_due", key="gap-2",
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
            daemon.push_signal(Signal(sense="test", kind="schedule_due", key="gap-3",
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

    def test_notice_text_passes_clean(self):
        """D62, found at U25's review: every sense's summary reaches the screen and then history. It
        is cleaned once where the notice text is formed - live and rebuilt after a restart."""
        async def scenario(daemon):
            _never_quiet(daemon)
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            await c.until("attached")
            daemon.push_signal(Signal(sense="test", kind="schedule_due", key="dirty-0", fields={"id": "s0"},
                                      summary="Scheduled note: found \x1b[31mit\x1b[0m\u202e now", trust="user"))
            live = (await c.until("notice"))[-1]
            c.send({"type": "presence", "idle_s": 0})  # typing: the next notice is held, then the daemon stops
            await asyncio.sleep(0.05)
            daemon.push_signal(Signal(sense="test", kind="schedule_due", key="dirty-1", fields={"id": "s1"},
                                      summary="Scheduled note: \x1b[1mtrain\u202e", trust="user"))
            for _ in range(200):
                if any(e.get("signal_key") == "dirty-1" and e.get("action") == "speak" for e in self.events()):
                    break
                await asyncio.sleep(0.01)
            c.close()
            return live

        live = self.run_with(fake_model("ok"), scenario)
        self.assertTrue(live["text"].startswith("Scheduled note: found it now"), live["text"])
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
                daemon.push_signal(Signal(sense="test", kind="schedule_due", key=f"held-{i}", fields={"id": str(i)},
                                          summary=summary, trust="user"))
                await asyncio.sleep(0.05)
            texts = [(await c.until("notice"))[-1]["text"] for _ in range(2)]
            c.close()
            return texts

        texts = self.run_with(fake_model(""), scenario)
        self.assertEqual(texts, ["Scheduled note: one\n\ttwo", "Scheduled note: red"])
        held = [e for e in self.events() if e["type"] == "decision" and e["signal_key"] == "held-1"]
        self.assertEqual(held[0]["text"], "Scheduled note: red")


class SigtermTest(DaemonHarness):
    """`serve()` runs a real Daemon (not through `run_with`, which owns start()/close() itself -
    the SIGTERM handler lives inside `serve()`)."""

    def test_sigterm_closes_mcp_servers(self):
        """launchd's `service stop` sends SIGTERM - without a handler for it, `close()` never
        runs and every npx child is orphaned (m5's bench had to killpg by hand for this)."""
        config.set(self.home, "mcp_servers",
                   {"fake": {"transport": "stdio", "command": [sys.executable, str(FAKE_SERVER)]}})

        async def main():
            daemon = Daemon(self.home, router=fake_model("ok"))
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
