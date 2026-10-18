import asyncio
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from pathfinder import assemble, clone as clone_mod, policy, results, router as router_mod, tools
from pathfinder.clone import BUDGET_NOTE, NOT_RUN, NOT_RUN_INTERRUPTED, REFUSED_LINE, SKIPPED, USER_UNKNOWN, Clone, Item
from pathfinder.ledger import Ledger
from pathfinder.llm import Endpoint, LLMError, Response, anthropic, openai_compat
from pathfinder.state import State
from pathfinder.tools import ToolSpec

REF = "anthropic:fake-model"
THINK = {"type": "thinking", "thinking": "plan", "signature": "sig-fake"}
PAGES = {"results": [{"id": str(uuid.UUID(int=i + 1, version=4)), "title": f"Title {i}"} for i in range(5)]}
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
ENDPOINTS = (Endpoint("http://127.0.0.1:9", "k-fake", "fake-model", api="anthropic"),
             Endpoint("http://127.0.0.1:9", "", "fake-model"))


def call(call_id, name, **args):
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def text(t):
    return {"type": "text", "text": t}


def resp(*blocks, stop=None, usage=None, fallbacks=()):
    stop = stop or ("tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn")
    return Response(list(blocks), stop, dict(usage or USAGE), REF, list(fallbacks))


def strip_marks(value):
    """A wire body without its cache_control markers (the history breakpoint moves forward by design)."""
    if isinstance(value, dict):
        return {k: strip_marks(v) for k, v in value.items() if k != "cache_control"}
    return [strip_marks(v) for v in value] if isinstance(value, list) else value


class FakeRouter:
    """Scripted steps: a Response, an exception to raise, or `async step(req, on_text, cancel)`."""

    def __init__(self, script):
        self.script, self.requests = list(script), []

    async def complete(self, req, on_text=None, cancel=None):
        self.requests.append(req)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return await step(req, on_text, cancel) if callable(step) else step


class CloneTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.sent, self.queue, self.written, self.started = [], [], [], threading.Event()

        def write(path, content, control=None):
            self.written.append((path, content))
            return "written"

        def slow(control=None):
            self.started.set()
            control.interrupt.wait(2)  # a shutdown leaves it running: bounded
            return "late"

        def fail(control=None):
            raise RuntimeError("boom")

        string = {"type": "string", "description": "a string"}
        fake = {"read_file": ToolSpec("read_file", "Read.", {"path": string}, ["path"], lambda path, control=None: PAGES),
                "write_file": ToolSpec("write_file", "Write.", {"path": string, "content": string}, ["path", "content"],
                                       write, write=True),
                "slow": ToolSpec("slow", "Slow.", {}, [], slow), "fail": ToolSpec("fail", "Fail.", {}, [], fail)}
        patcher = mock.patch.dict(tools.TOOLS, fake, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        return event

    async def _resident(self):
        return ["Local time 09:00"]

    def clone(self, *script) -> Clone:
        """A clone at daemon start (restore), as the daemon wires it."""
        self.router = FakeRouter(script)
        c = Clone("root", "root", home=self.home, state=self.state, record=self.record, router=self.router,
                  send=lambda session, msg: self.sent.append(msg), enqueue=self.queue.append, resident=self._resident)
        c.restore()
        return c

    def events(self, *types):
        return [e for e in self.ledger.read() if e["type"] in types]

    def config(self, **values):
        (self.home / "config.json").write_text(json.dumps(values))

    async def until(self, cond):
        for _ in range(500):
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail("timed out")

    def body(self, call_id):
        [event] = [e for e in self.events("result") if e["call_id"] == call_id]
        return event["body"]

    async def gated_turn(self, *first):
        """One turn whose write_file call is handed off at a gate; returns (clone, gate event)."""
        c = self.clone(resp(*first, call("c1", "write_file", path="/tmp/y", content="z")),
                       resp(text("I asked for approval.")))
        self.assertEqual(await c.run(Item("user", "Write it.", "s1")), "answer")
        return c, self.events("gate")[0]

    async def test_turn_table_and_append_only_history(self):
        """read_file → r:N → read_result → answer, then a second turn: every event recorded in order, and each
        adapter's wire body extends the previous one (system/tools exact, messages without cache marks)."""
        async def reread(req, on_text, cancel):
            return resp(call("c2", "read_result", handle=f"r:{self.events('result')[-1]['seq']}", query="Title 1"))

        async def answer(req, on_text, cancel):
            on_text("Found it.")
            return resp(text("Found it."))

        c = self.clone(resp(THINK, call("c1", "read_file", path="/tmp/x")), reread, answer, resp(text("Again.")))
        self.assertEqual(await c.run(Item("user", "Find the page.", "s1")), "answer")
        self.assertEqual(await c.run(Item("user", "Again?", "s1")), "answer")
        kinds = [e["type"] for e in self.ledger.read() if e["type"] != "spawn"]
        self.assertEqual(kinds, ["user", "turn_start", "assistant", "result", "assistant", "result", "assistant",
                                 "turn_end", "user", "turn_start", "assistant", "turn_end"])
        first = self.events("assistant")[0]
        self.assertEqual((first["model"], first["blocks"], first["final"]), (REF, [THINK, call("c1", "read_file",
                                                                                              path="/tmp/x")], False))
        r1, r2 = self.events("result")
        self.assertEqual((r1["body"], r1["trust"], r1["ok"]), (PAGES, "third_party", True))
        self.assertEqual((r2["tool"], r2["ok"], r2["body"]["matches"]), ("read_result", True, 1))
        end = self.events("turn_end")[0]
        self.assertEqual((end["kind"], end["steps"], end["usage"]["input_tokens"]), ("answer", 3, 30))
        self.assertIn({"type": "delta", "text": "Found it."}, self.sent)
        self.assertIn({"type": "tool", "name": "read_file", "ok": True}, self.sent)
        reqs = self.router.requests
        self.assertEqual([r.role for r in reqs], ["root"] * 4)
        for endpoint, wire in zip(ENDPOINTS, (anthropic.to_wire, openai_compat.to_wire)):
            bodies = [strip_marks(wire(r, endpoint)) for r in reqs]
            for a, b in zip(bodies, bodies[1:]):
                self.assertEqual((a.get("system"), a["tools"]), (b.get("system"), b["tools"]))
                self.assertEqual(b["messages"][:len(a["messages"])], a["messages"])
                self.assertGreater(len(b["messages"]), len(a["messages"]))

    async def test_a_call_refused_at_the_door_is_a_failed_result(self):
        """The broker records nothing (D69's emits left in U10b): the clone's `result` carries the refusal."""
        c = self.clone(resp(call("c1", "nope", x=1)), resp(text("No such tool.")))
        self.assertEqual(await c.run(Item("user", "Use nope.", "s1")), "answer")
        [r] = self.events("result")
        self.assertEqual((r["tool"], r["ok"], r["body"]), ("nope", False, "no such tool: nope"))
        c = self.clone(resp(call("c2", "n" * 3000)), resp(text("No such tool.")))
        self.assertEqual(await c.run(Item("user", "Use it.", "s1")), "answer")
        self.assertEqual(self.events("result")[-1]["tool"], "n" * 64)

    async def test_steer_mid_step_and_refused_turn_keeps_an_opening_message(self):
        async def steer_during(req, on_text, cancel):
            c.steer("s1", "also the date")
            return resp(call("c1", "read_file", path="/tmp/x"))

        async def refuse(req, on_text, cancel):
            c.steer("s1", "one more")
            return resp(stop="refusal")

        c = self.clone(steer_during, refuse, resp(text("ok")))
        self.assertEqual(await c.run(Item("user", "Find it.", "s1")), "refused")
        [steer] = [e for e in self.events("user") if e.get("steer")]
        [result] = self.events("result")
        self.assertGreater(steer["seq"], result["seq"])  # recorded at the step boundary, as the model saw it
        self.assertEqual((steer["text"], steer["turn"]), ("also the date", result["turn"]))
        one, two = self.router.requests
        self.assertEqual(two.messages[:len(one.messages)], one.messages)
        self.assertEqual(two.messages[-2]["content"][1:], [text("also the date")])
        self.assertIn({"type": "error", "message": REFUSED_LINE}, self.sent)
        [item] = self.queue  # said during the refused step: its own turn
        self.assertEqual((item.kind, item.text), ("continue", "one more"))
        self.assertEqual(await c.run(item), "answer")
        third = self.router.requests[2].messages
        self.assertEqual(third[0], {"role": "user", "content": [text("one more")]})
        self.assertNotIn("Find it.", json.dumps(third))

    async def test_steers_stay_in_their_session(self):
        async def says(req, on_text, cancel):
            c.steer("s2", "from B")
            c.steer("s1", "late A")
            return resp(text("ok"))

        c = self.clone(says, resp(text("hi B")))
        self.assertEqual(await c.run(Item("signal", "An envelope.", "s1", origin="signal")), "answer")
        self.assertEqual([e for e in self.events("user") if e.get("steer")], [])
        self.assertEqual(self.queue, [Item("continue", "late A", "s1")])  # the user's words: origin user
        self.assertEqual(await c.run(Item("user", "B starts.", "s2")), "answer")
        [steer] = [e for e in self.events("user") if e.get("steer")]
        b_turn = self.events("turn_start")[-1]["turn"]
        self.assertEqual((steer["session"], steer["turn"], steer["text"]), ("s2", b_turn, "from B"))
        self.assertIn("from B", json.dumps(self.router.requests[-1].messages))

    async def test_interrupt_while_gathering_the_resident_block(self):
        hold = asyncio.Event()

        async def resident():
            self.started.set()
            await hold.wait()

        c = self.clone()
        c.resident = resident
        task = asyncio.create_task(c.run(Item("user", "Hi.", "s1")))
        await self.until(self.started.is_set)
        c.interrupt()
        self.assertEqual(await task, "interrupted")
        self.assertEqual((self.router.requests, self.events("turn_end")[0]["kind"]), ([], "interrupted"))
        self.assertEqual(asyncio.current_task().cancelling(), 0)

    async def test_gate_status_rendered_from_gate_event_and_decided_once(self):
        c, gate = await self.gated_turn()
        n = gate["seq"]
        self.assertEqual({k: gate[k] for k in ("clone", "step", "call_id", "tool", "args", "effect", "brief")},
                         {"clone": "root", "step": 1, "call_id": "c1", "tool": "write_file",
                          "args": {"path": "/tmp/y", "content": "z"}, "effect": "write", "brief": "Write it."})
        self.assertEqual(self.events("result"), [])
        status = self.router.requests[1].messages[-2]["content"]
        self.assertEqual(status, [{"type": "tool_result", "tool_use_id": "c1",
                                   "content": f'{{"status":"waiting for approval","gate":{n}}}'}])
        self.assertIn({"type": "gate", "id": n, "tool": "write_file", "args": gate["args"]}, self.sent)
        self.assertEqual(c.state, "parked")
        self.assertFalse(c.decide(n, "approve", "socket"))  # not a decision: nothing recorded
        self.assertEqual(self.events("resume"), [])
        self.assertTrue(c.decide(n, "approved", "socket"))
        self.assertFalse(c.decide(n, "denied", "socket"))
        self.assertFalse(c.decide(n + 999, "approved", "socket"))
        await self.until(lambda: self.queue)
        self.assertEqual(self.written, [("/tmp/y", "z")])
        [done] = self.events("result")
        self.assertEqual((done["gate"], done["ok"], done["body"]), (n, True, "written"))
        [resume] = self.events("resume")
        self.assertEqual((resume["gate"], resume["decision"], resume["by"]), (n, "approved", "socket"))
        line = f"gate {n} (write_file) approved → " + results.render(done)
        self.assertEqual(self.queue, [Item("continue", line, "s1", gate=n)])
        self.router.script.append(resp(text("Written.")))
        self.assertEqual(await c.run(self.queue.pop()), "answer")
        report = self.events("user")[-1]
        self.assertEqual((report["gate"], report["origin"], report["text"]), ([n], "system", line))
        self.assertEqual(self.router.requests[-1].messages[-2], {"role": "user", "content": [text(line)]})
        self.assertTrue(self.state.gate(n)["reported"])

    async def test_reports_merged_never_with_a_say(self):
        c = self.clone(resp(call("c1", "write_file", path="/a", content="1"), call("c2", "write_file", path="/b",
                                                                                   content="2")),
                       resp(text("asked")), resp(text("hello")), resp(text("reported")))
        await c.run(Item("user", "Write both.", "s1"))
        g1, g2 = (g["seq"] for g in self.events("gate"))
        self.assertTrue(c.decide(g2, "denied", "chat"))
        self.assertTrue(c.decide(g1, "approved", "socket"))
        await self.until(lambda: len(self.queue) == 2)
        self.assertEqual(await c.run(Item("user", "hi", "s1")), "answer")
        self.assertEqual([e for e in self.events("user") if e.get("gate")], [])
        first, second = self.queue
        self.assertEqual(await c.run(first), "answer")
        [report] = [e for e in self.events("user") if e.get("gate")]
        self.assertEqual(report["gate"], [g1, g2])
        self.assertEqual(report["text"].split("\n")[1], f"gate {g2} (write_file) denied by the user")
        self.assertTrue(report["text"].startswith(f"gate {g1} (write_file) approved → "))
        count = len(list(self.ledger.read()))
        self.assertEqual(await c.run(second), "")  # merged into the first: nothing recorded
        self.assertEqual(len(list(self.ledger.read())), count)

    async def test_interrupt_leaves_gate_open(self):
        c = self.clone(resp(call("c1", "write_file", path="/a", content="1"), call("c2", "slow"),
                            call("c3", "read_file", path="/x")))
        task = asyncio.create_task(c.run(Item("user", "Go.", "s1")))
        await self.until(self.started.is_set)
        self.assertEqual(c.state, "running")
        c.interrupt()
        self.assertEqual(await task, "interrupted")
        self.assertEqual((self.body("c2"), self.body("c3")), (USER_UNKNOWN, NOT_RUN_INTERRUPTED))
        [stop] = self.events("interrupted")
        self.assertEqual((stop["why"], stop["step"]), ("user", 1))
        self.assertEqual(self.events("turn_end")[-1]["kind"], "interrupted")
        [gate] = self.state.open_gates()
        self.assertEqual((gate["call_id"], gate["decision"]), ("c1", None))
        self.assertEqual(c.state, "parked")
        self.assertEqual(asyncio.current_task().cancelling(), 0)

    async def test_partial_step_keeps_complete_text_only(self):
        async def anthropic_like(req, on_text, cancel):
            on_text("Hel")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:  # the adapter returns the blocks as they arrived
                return Response([{"type": "thinking", "thinking": "half", "signature": ""}, text("Hel"),
                                 call("c9", "read_file")], "interrupted", dict(USAGE), REF, [])

        async def thread_like(req, on_text, cancel):
            on_text("Hi th")
            await asyncio.Event().wait()

        c = self.clone(anthropic_like, thread_like, resp(text("ok")))
        for streamed in ("Hel", "Hi th"):
            task = asyncio.create_task(c.run(Item("user", "Talk.", "s1")))
            await self.until(lambda: {"type": "delta", "text": streamed} in self.sent)
            c.interrupt()
            self.assertEqual(await task, "interrupted")
            step = self.events("assistant")[-1]
            self.assertEqual((step["blocks"], step["stop_reason"], step["text"], step["final"]),
                             ([text(streamed)], "interrupted", streamed, True))
        self.assertEqual(await c.run(Item("user", "Go on.", "s1")), "answer")
        self.assertIn({"role": "assistant", "model": REF, "content": [text("Hel")]}, self.router.requests[-1].messages)
        self.assertNotIn("half", json.dumps(self.router.requests[-1].messages))

    async def test_shutdown_reraises_and_records_nothing(self):
        async def anthropic_like(req, on_text, cancel):
            on_text("Hel")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return Response([text("Hel")], "interrupted", dict(USAGE), REF, [])

        c = self.clone(anthropic_like)
        task = asyncio.create_task(c.run(Item("user", "Talk.", "s1")))
        await self.until(lambda: self.sent and self.sent[-1]["type"] == "delta")
        task.cancel()  # the daemon is shutting down, not interrupt()
        c.interrupt()  # a shutdown already under way wins
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events("assistant", "turn_end", "interrupted"), [])
        self.assertEqual(c.state, "running")

    async def test_restart_renders_outcome_unknown_and_strips_thinking(self):
        c = self.clone(resp(THINK, call("c1", "slow")))
        task = asyncio.create_task(c.run(Item("user", "Run it.", "s1")))
        await self.until(self.started.is_set)
        task.cancel()  # the daemon died mid-call
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events("result", "turn_end"), [])
        c = self.clone(resp(text("The outcome is unknown.")))  # the next daemon start
        [stop] = self.events("interrupted")
        self.assertEqual((stop["why"], self.events("turn_end")[0]["kind"], c.state), ("restart", "interrupted",
                                                                                     "parked"))
        self.assertEqual(await c.run(Item("user", "Did it finish?", "s1")), "answer")
        messages = self.router.requests[0].messages
        self.assertEqual(messages[2], {"role": "assistant", "model": REF, "content": [call("c1", "slow")]})
        self.assertEqual(messages[3]["content"][0], {"type": "tool_result", "tool_use_id": "c1", "is_error": True,
                                                     "content": assemble.RESTART_UNKNOWN})

    async def test_open_gate_survives_restart_and_returns_as_item(self):
        _, gate = await self.gated_turn(THINK)
        n = gate["seq"]
        self.state.close()
        for path in self.home.glob("state.db*"):
            path.unlink()
        self.state = State(self.home / "state.db")
        self.state.rebuild(self.ledger)
        c = self.clone(resp(text("Written.")))
        self.assertEqual([(g["seq"], g["decision"]) for g in self.state.open_gates()], [(n, None)])
        self.assertEqual((self.queue, self.written), ([], []))
        self.assertTrue(c.decide(n, "approved", "socket"))
        await self.until(lambda: self.queue)
        self.assertEqual(self.written, [("/tmp/y", "z")])
        self.assertEqual(self.events("result")[0]["gate"], n)
        self.assertEqual(await c.run(self.queue.pop()), "answer")
        self.assertEqual(self.events("user")[-1]["gate"], [n])
        self.assertNotIn('"thinking"', json.dumps(self.router.requests[-1].messages))
        self.assertEqual(self.written, [("/tmp/y", "z")])

    async def test_decided_gate_requeued_on_restart(self):
        c, gate = await self.gated_turn()
        self.assertTrue(c.decide(gate["seq"], "denied", "socket"))
        self.queue.clear()  # the lane died with the daemon
        self.clone()
        self.assertEqual(self.queue, [Item("continue", f"gate {gate['seq']} (write_file) denied by the user", "s1",
                                           gate=gate["seq"])])

    async def test_approved_gate_without_result_is_outcome_unknown(self):
        _, gate = await self.gated_turn()
        n = gate["seq"]
        self.record("resume", session="s1", turn=gate["turn"], origin="user", trust="user", clone="root", gate=n,
                    decision="approved", by="socket")  # the daemon died before the call ran
        self.clone()
        [done] = self.events("result")
        self.assertEqual((done["gate"], done["ok"], done["body"]), (n, False, assemble.RESTART_UNKNOWN))
        self.assertEqual(self.written, [])
        self.assertEqual(self.queue, [Item("continue", f"gate {n} (write_file) approved → failed: "
                                           + results.render(done), "s1", gate=n)])

    async def test_budget_note_keeps_tools(self):
        self.config(max_steps=2)
        c = self.clone(*[resp(call(f"c{i}", "read_file", path="/x")) for i in range(3)], resp(text("ok")))
        self.assertEqual(await c.run(Item("user", "Loop.", "s1")), "budget")
        one, _, last = self.router.requests
        self.assertEqual(last.tools, one.tools)
        self.assertEqual(last.messages[-2]["content"][-1], text(BUDGET_NOTE))
        self.assertEqual(self.body("c2"), NOT_RUN["budget"])
        self.assertEqual(self.events("turn_end")[0]["steps"], 3)
        self.assertEqual(await c.run(Item("user", "Again.", "s1")), "answer")  # a budget per item
        heavy = {**USAGE, "input_tokens": 99_000, "output_tokens": 500, "cache_creation_input_tokens": 600}
        cached = {**USAGE, "cache_read_input_tokens": 900_000}
        self.config()
        self.router.script += [resp(call("t1", "read_file", path="/x"), usage=cached),
                               resp(call("t2", "read_file", path="/x"), usage=heavy), resp(text("spent"))]
        self.assertEqual(await c.run(Item("user", "Read a lot.", "s1")), "budget")
        self.assertEqual(self.events("result")[-1]["ok"], True)  # cache reads are not spent
        self.assertEqual(self.events("turn_end")[-1]["steps"], 3)

    async def test_batch_stop(self):
        c = self.clone(resp(call("a", "fail"), call("b", "fail"), call("c", "read_file", path="/x")),
                       resp(call("d", "fail")), resp(call("e", "read_file", path="/x")),
                       resp(call("f", "write_file", path="/a", content="1"), call("g", "read_file", path="/x")),
                       resp(text("ok")))
        self.assertEqual(await c.run(Item("user", "Try.", "s1")), "error")
        self.assertEqual([self.body(x) for x in "abcde"], ["boom", "boom", SKIPPED, "boom", NOT_RUN["error"]])
        self.assertIn(clone_mod.FAILED_NOTE.format(tool="fail"), [e["text"] for e in self.events("user")])
        self.config(grants={"write_file": "deny"})
        self.assertEqual(await c.run(Item("user", "Write.", "s1")), "answer")
        self.assertEqual((self.body("f"), self.body("g")), (policy.DENIED_TEXT, SKIPPED))

    async def test_fold_trigger(self):
        blocked = LLMError(402, "out of credit")
        blocked.fallbacks = [{"model": REF, "status": 402, "message": "out of credit"}]  # as the router raises it
        c = self.clone(resp(call("c1", "read_file", path="/x")), resp(text("one")), resp(text("two")),
                       resp(text("Summary of turn one.")), resp(text("three")), blocked)
        with mock.patch.object(assemble, "HISTORY_BUDGET", 50), mock.patch.object(assemble, "FOLD_KEEP", 10):
            await c.run(Item("user", "First.", "s1"))
            self.assertEqual(self.events("summary"), [])  # the newest turn never folds
            await c.run(Item("user", "Second.", "s1"))
            fold = self.router.requests[-1]
            self.assertEqual((fold.role, fold.max_tokens), ("summarize", assemble.FOLD_MAX_TOKENS))
            [summary] = self.events("summary")
            r1 = self.events("result")[0]["seq"]
            self.assertEqual((summary["upto"], summary["first"]), (self.events("turn_end")[0]["seq"],
                                                                   self.events("user")[0]["seq"]))
            self.assertEqual(summary["handles"], [f"r:{r1}"])
            self.assertTrue(summary["text"].startswith(f"Summary of turn one.\nr:{r1} read_file "))
            self.assertEqual(summary["model"], REF)
            await c.run(Item("user", "Third.", "s1"))  # this fold fails: an error, no summary
        self.assertEqual(len(self.events("summary")), 1)
        self.assertEqual(self.events("error")[-1]["status"], 402)

    async def test_a_root_only_config_folds_on_roots_chain(self):
        """U12: `models` holds only root; through the real router the fold's summarize request runs on root's chain."""
        steps, seen = [resp(text("one")), resp(text("two")), resp(text("Summary of turn one."))], []

        async def adapter(endpoint, req, on_text=None, cancel=None):
            seen.append(req.role)
            return steps.pop(0)

        cfg = {"providers": {"local": {"base_url": "http://127.0.0.1:9/v1"}}, "models": {"root": ["local:fake-model"]}}
        c = Clone("root", "root", home=self.home, state=self.state, record=self.record,
                  router=router_mod.Router(lambda: cfg), send=lambda session, msg: self.sent.append(msg),
                  enqueue=self.queue.append, resident=self._resident)
        c.restore()
        with mock.patch.object(openai_compat, "complete", adapter), \
                mock.patch.object(assemble, "HISTORY_BUDGET", 50), mock.patch.object(assemble, "FOLD_KEEP", 10):
            await c.run(Item("user", "First.", "s1"))
            await c.run(Item("user", "Second.", "s1"))
        self.assertEqual(seen, ["root", "root", "summarize"])
        self.assertEqual(self.events("error"), [])
        [summary] = self.events("summary")
        self.assertEqual(summary["model"], "local:fake-model")

    async def test_a_blank_stream_before_an_error_records_no_text_block(self):
        """U8a review: what streamed is recorded, but a blank text block would be replayed in every later
        request (Anthropic: a 400), so the step keeps its text and no block."""
        async def blank(req, on_text, cancel):
            on_text("  ")
            error = LLMError(500, "the stream broke")
            error.fallbacks = []
            raise error

        c = self.clone(blank)
        self.assertEqual(await c.run(Item("user", "Hi.", "s1")), "error")
        [said] = self.events("assistant")
        self.assertEqual((said["text"], said["blocks"]), ("  ", []))

    async def test_shutdown_mid_fold_records_no_summary(self):
        """U8a review: the Anthropic adapter answers a cancel with stop_reason `interrupted`; mid-fold that is a
        shutdown, so the fold re-raises instead of recording a half summary (and close() is not held up)."""
        started = asyncio.Event()

        async def folding(req, on_text, cancel):
            started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:  # what llm.anthropic.complete does
                return resp(text("half a summ"), stop="interrupted")

        c = self.clone(resp(text("one")), resp(text("two")), folding)
        with mock.patch.object(assemble, "HISTORY_BUDGET", 50), mock.patch.object(assemble, "FOLD_KEEP", 10):
            await c.run(Item("user", "First.", "s1"))
            task = asyncio.create_task(c.run(Item("user", "Second.", "s1")))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.events("summary"), [])

    async def test_refused_or_empty_fold_is_a_failed_fold(self):
        c = self.clone(resp(text("one")), resp(text("two")), resp(stop="refusal"), resp(text("three")),
                       resp(text("  ")))
        with mock.patch.object(assemble, "HISTORY_BUDGET", 50), mock.patch.object(assemble, "FOLD_KEEP", 10):
            for said in ("First.", "Second.", "Third."):
                await c.run(Item("user", said, "s1"))
        self.assertEqual(self.events("summary"), [])
        self.assertEqual([e["message"] for e in self.events("error")],
                         ["fold failed: the summarize step ended refusal without text",
                          "fold failed: the summarize step ended end_turn without text"])
        self.assertIn("First.", json.dumps(self.router.requests[3].messages))  # the window is unchanged

    async def test_clone_transitions_in_ledger(self):
        rows = []

        async def look(step):
            async def step_fn(req, on_text, cancel):
                rows.append(self.state.clone_row("root"))
                return step
            return step_fn

        c = self.clone(await look(resp(call("c1", "write_file", path="/a", content="1"))), await look(resp(text("ok"))))
        self.assertEqual(c.state, "parked")
        await c.run(Item("user", "Write.", "s1"))
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[1], rows[0])  # the gate between the two steps changed no clone state
        self.assertEqual(c.state, "parked")
        moves = [e["type"] for e in self.ledger.read() if e["type"] in ("spawn", "turn_start", "turn_end",
                                                                        "interrupted")]
        self.assertEqual(moves, ["spawn", "turn_start", "turn_end"])
        rebuilt = State(self.home / "rebuilt.db")
        rebuilt.rebuild(self.ledger)
        self.assertEqual(rebuilt.clone_row("root"), self.state.clone_row("root"))
        rebuilt.close()

    async def test_max_tokens_and_errors_per_attempt(self):
        fell = {"model": "anthropic:a", "status": 429, "message": "slow down"}
        blocked = LLMError(429, "slow down")
        blocked.fallbacks = [fell]
        bad = LLMError(400, "bad request")
        bad.fallbacks = []
        c = self.clone(resp(call("c1", "read_file", path="/x"), stop="max_tokens"),
                       resp(text("hi"), fallbacks=[fell]), blocked, bad)
        self.assertEqual(await c.run(Item("user", "Long.", "s1")), "max_tokens")
        self.assertEqual(self.body("c1"), NOT_RUN["max_tokens"])
        self.assertEqual(await c.run(Item("user", "Hi.", "s1")), "answer")
        self.assertEqual(await c.run(Item("user", "Hi.", "s1")), "error")
        self.assertEqual(await c.run(Item("user", "Hi.", "s1")), "error")
        errors = [(e["model"], e["status"], e["step"]) for e in self.events("error")]
        self.assertEqual(errors, [("anthropic:a", 429, 1), ("anthropic:a", 429, 1), ("", 400, 1)])
        self.assertIn({"type": "error", "message": "bad request"}, self.sent)


if __name__ == "__main__":
    unittest.main()
