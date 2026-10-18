import ast
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from pathfinder import assemble, daemon, results, tools  # daemon: the native tools register on import
from pathfinder.ledger import Ledger
from pathfinder.llm import Endpoint
from pathfinder.llm import anthropic, openai_compat
from pathfinder.state import State

REF = "anthropic:fake-model"
THINK = {"type": "thinking", "thinking": "plan", "signature": "sig-fake"}
TOOLS = [{"name": "write_file", "description": "Write a file.", "parameters": {"type": "object", "properties": {}}},
         {"name": "read_file", "description": "Read a file.", "parameters": {"type": "object", "properties": {}}}]
PAGES = {"results": [{"id": str(uuid.UUID(int=i + 1, version=4)), "title": f"Secret Title {i}"} for i in range(5)]}


def call(call_id, name, **args):
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def text(t):
    return {"type": "text", "text": t}


class AssembleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.view = assemble.View(id="root", role="root", session="s1", epoch=0)

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def ev(self, type, turn, **data):
        event = self.ledger.append(type, session="s1", turn=turn, origin="user", clone="root", **data)
        self.state.apply(event)
        return event

    def say(self, turn, said, resident="Local time 09:00", **extra):
        self.ev("user", turn, text=said, **extra)
        self.ev("turn_start", turn, item="user", resident=resident)

    def step(self, turn, *blocks):
        return self.ev("assistant", turn, step=1, model=REF, blocks=list(blocks), stop_reason="end_turn", usage={},
                       text="", final=False)

    def result(self, turn, call_id, tool, body, ok=True, **extra):
        return self.ev("result", turn, step=1, call_id=call_id, tool=tool, ok=ok, body=body, **extra)

    def turn(self, turn, said, body=None, thinking=False):
        """One whole turn: a say, a read_file call and its result (when `body`), an answer, the turn's end."""
        self.say(turn, said)
        found = None
        if body is not None:
            self.step(turn, call(f"{turn}c", "read_file", path="/tmp/x"))
            found = self.result(turn, f"{turn}c", "read_file", body)
        self.step(turn, *([THINK] if thinking else []), text("ok"))
        return found, self.ev("turn_end", turn, kind="answer", steps=1, usage={})

    def messages(self, view=None):
        return list(assemble.assemble(self.state, view or self.view, TOOLS).messages)

    def test_exact_messages_for_a_two_turn_session(self):
        """A tool call, a read_result, a gate, then the report as a continue item."""
        self.say("t1", "Find the page.", resident="Local time A")
        self.step("t1", THINK, call("c1", "read_file", path="/tmp/x"))
        r1 = self.result("t1", "c1", "read_file", PAGES)
        self.step("t1", call("c2", "read_result", handle=f"r:{r1['seq']}", query="Title 1"))
        out = results.read(self.state, assemble.window_handles(self.state, self.view),
                           {"handle": f"r:{r1['seq']}", "query": "Title 1"})[1]
        self.result("t1", "c2", "read_result", out)
        self.step("t1", call("c3", "write_file", path="/tmp/y", content="z"))
        gate = self.ev("gate", "t1", step=3, call_id="c3", tool="write_file", args={"path": "/tmp/y"},
                       effect="write", brief="write it")
        self.step("t1", text("I asked for approval."))
        self.ev("turn_end", "t1", kind="answer", steps=4, usage={})
        self.ev("resume", "t1", gate=gate["seq"], decision="approved", by="socket")
        done = self.result("t1", "c3", "write_file", "written", gate=gate["seq"])
        report = f"gate {gate['seq']} (write_file) approved → " + results.render(done)
        self.say("t2", report, resident="Local time B", gate=[gate["seq"]])
        req = assemble.assemble(self.state, self.view, TOOLS)
        a, b = {"role": "system", "content": "Local time A", "item": "t1"}, {"role": "system", "content": "Local time B",
                                                                             "item": "t2"}
        self.assertEqual(list(req.messages), [
            {"role": "user", "content": [text("Find the page.")]}, a,
            {"role": "assistant", "model": REF, "content": [THINK, call("c1", "read_file", path="/tmp/x")]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                          "content": results.render({**r1, "body": PAGES})}]}, a,
            {"role": "assistant", "model": REF, "content": [call("c2", "read_result", handle=f"r:{r1['seq']}",
                                                                 query="Title 1")]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c2",
                                          "content": json.dumps(out, ensure_ascii=False, separators=(",", ":"))}]}, a,
            {"role": "assistant", "model": REF, "content": [call("c3", "write_file", path="/tmp/y", content="z")]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c3",
                                          "content": f'{{"status":"waiting for approval","gate":{gate["seq"]}}}'}]}, a,
            {"role": "assistant", "model": REF, "content": [text("I asked for approval.")]},
            {"role": "user", "content": [text(report)]}, b])
        self.assertEqual((req.role, req.clone, req.turn, req.model, req.system), ("root", "root", "t2", "", assemble.SYSTEM))
        self.assertEqual([t["name"] for t in req.tools], ["read_result", "read_file", "write_file"])
        self.assertIn(done["seq"], assemble.window_handles(self.state, self.view))  # r:M reached through the report

    def test_append_only_and_deterministic(self):
        """Each assemble's messages start with the previous one's; a rebuilt state gives the same bytes."""
        sent = []
        self.say("t1", "Read x.")
        sent.append(self.messages())
        self.step("t1", THINK, call("c1", "read_file", path="/tmp/x"))
        self.result("t1", "c1", "read_file", "one\ntwo")
        sent.append(self.messages())
        self.step("t1", text("done"))
        self.ev("turn_end", "t1", kind="answer", steps=2, usage={})
        self.say("t2", "Again.", resident="Local time 09:05")
        sent.append(self.messages())
        for a, b in zip(sent, sent[1:]):
            self.assertEqual(b[:len(a)], a)
        rebuilt = State(self.home / "rebuilt.db")
        rebuilt.rebuild(self.ledger)
        self.assertEqual(json.dumps(assemble.assemble(rebuilt, self.view, TOOLS).__dict__),
                         json.dumps(assemble.assemble(self.state, self.view, TOOLS).__dict__))
        rebuilt.close()

    def test_static_system_frozen(self):
        """Piece 1 and both adapters' system/tools stay byte-identical across steps and turns while the resident
        (time, USER.md, wiki) changes."""
        self.assertEqual(assemble.SYSTEM, (Path(assemble.__file__).parent / "prompts" / "root.txt").read_text(encoding="utf-8"))
        self.assertIn(assemble.HEADER.rstrip(":"), assemble.SYSTEM)  # the prompt names the summary's header
        ep_a = Endpoint(base_url="https://api.example.com", api_key="sk-fake", model="fake-model", api="anthropic")
        ep_o = Endpoint(base_url="http://localhost:1/v1", api_key="", model="fake-model")
        wires = []
        for n, resident in enumerate(["Local time 09:00", "Local time 10:00\n\nUSER.md: terse", "wiki: - Trip"]):
            self.say(f"t{n}", "Read x.", resident=resident)
            wires.append(assemble.assemble(self.state, self.view, TOOLS))
            self.step(f"t{n}", call(f"c{n}", "read_file", path="/tmp/x"))
            self.result(f"t{n}", f"c{n}", "read_file", "body")
            wires.append(assemble.assemble(self.state, self.view, TOOLS))
            self.step(f"t{n}", text("ok"))
            self.ev("turn_end", f"t{n}", kind="answer", steps=2, usage={})
        a = {json.dumps([anthropic.to_wire(r, ep_a)[k] for k in ("system", "tools")]) for r in wires}
        o = {json.dumps([(w := openai_compat.to_wire(r, ep_o))["messages"][0], w["tools"]]) for r in wires}
        self.assertEqual((len(a), len(o), {r.system for r in wires}), (1, 1, {assemble.SYSTEM}))

    def test_history_has_handles_not_bodies(self):
        """A 20 KB body's mid-body marker reaches a request only inside the read_result call's tool_result."""
        lines = [f"line {i} of the filler text" for i in range(800)]
        lines[400] = "MARKER-QX the needle"
        body = "\n".join(lines)
        self.assertGreater(len(body), 20000)
        self.say("t1", "Find the needle.")
        self.step("t1", call("c1", "read_file", path="/tmp/x"))
        r1 = self.result("t1", "c1", "read_file", body)
        reqs = [assemble.assemble(self.state, self.view, TOOLS)]
        seen = assemble.window_handles(self.state, self.view)
        self.assertIn(r1["seq"], seen)
        self.step("t1", call("c2", "read_result", handle=f"r:{r1['seq']}", query="needle"))
        self.result("t1", "c2", "read_result", results.read(self.state, seen, {"handle": f"r:{r1['seq']}",
                                                                                "query": "needle"})[1])
        reqs.append(assemble.assemble(self.state, self.view, TOOLS))
        self.step("t1", text("found it"))
        self.ev("turn_end", "t1", kind="answer", steps=3, usage={})
        self.say("t2", "Thanks.")
        reqs.append(assemble.assemble(self.state, self.view, TOOLS))
        for req in reqs:
            self.assertNotIn("MARKER", req.system + json.dumps(req.tools))
            for m in req.messages:
                for b in m["content"] if isinstance(m["content"], list) else [m["content"]]:
                    if "MARKER" in json.dumps(b):
                        self.assertEqual((b["type"], b["tool_use_id"]), ("tool_result", "c2"))
        self.assertIn("MARKER", json.dumps(reqs[-1].messages))

    def test_refused_turn_epoch_strip_and_restart_cut(self):
        self.say("t1", "Something declined.")
        self.step("t1", call("c1", "read_file", path="/tmp/x"))
        r1 = self.result("t1", "c1", "read_file", "x")
        self.step("t1")
        self.ev("turn_end", "t1", kind="refused", steps=2, usage={})
        self.say("t2", "Run it.")
        thought = self.step("t2", THINK, call("c2", "run_command", command="sleep 30"))
        self.ev("interrupted", "t2", step=1, why="restart")
        self.ev("turn_end", "t2", kind="interrupted", steps=1, usage={})
        self.say("t3", "Did it finish?")
        self.assertNotIn(r1["seq"], assemble.window_handles(self.state, self.view))
        msgs = self.messages(assemble.View(id="root", role="root", session="s1", epoch=thought["seq"]))
        self.assertNotIn("declined", json.dumps(msgs))
        self.assertEqual(msgs[2], {"role": "assistant", "model": REF, "content": [call("c2", "run_command",
                                                                                         command="sleep 30")]})
        self.assertEqual(msgs[3]["content"], [{"type": "tool_result", "tool_use_id": "c2", "is_error": True,
                                               "content": assemble.RESTART_UNKNOWN}, text("Did it finish?")])
        self.assertIn(THINK, self.messages()[2]["content"])  # epoch 0: kept

    def test_budgets(self):
        self.assertLessEqual(len(assemble.SYSTEM), 6000)
        self.assertLessEqual(len(assemble.FOLD_PROMPT), 1500)
        native = [spec.schema()["function"] for spec in tools.TOOLS.values()]
        req = assemble.assemble(self.state, self.view, native)
        self.assertLessEqual(len(json.dumps(req.tools, ensure_ascii=False)), 8000)  # native + core (Deviation 7)
        self.assertEqual(assemble.resident(["a" * 3000, "", "b" * 3000]), "a" * 3000 + "\n\n" + "b" * 998)
        self.say("t1", "Five calls.")
        self.step("t1", *[call(f"c{i}", "read_file", path=f"/tmp/{i}") for i in range(5)])
        made = [self.result("t1", f"c{i}", "read_file", "error text " * 190, ok=False) for i in range(5)]
        step = self.messages()[-2]["content"]
        self.assertEqual([len(b["content"]) > 1900 for b in step], [True] * 4 + [False])  # past 8,000: handle only
        self.assertEqual(json.loads(step[4]["content"])["handle"], f"r:{made[4]['seq']}")
        self.assertLessEqual(sum(len(b["content"]) for b in step[:4]), 8000)

    def test_fold_point(self):
        self.assertIsNone(assemble.fold_point(self.state, self.view))
        self.say("t1", "w " * 13000)  # one huge turn: the newest item never folds
        self.assertIsNone(assemble.fold_point(self.state, self.view))
        end1 = self.ev("turn_end", "t1", kind="answer", steps=0, usage={})
        self.say("t2", "Read three.")
        for i in range(3):  # a tool round: the cut never falls inside it
            self.step("t2", call(f"c{i}", "read_file", path="/tmp/x"))
            self.result("t2", f"c{i}", "read_file", "w " * 3000)
        self.step("t2", text("ok"))
        self.ev("turn_end", "t2", kind="answer", steps=4, usage={})
        self.say("t3", "Short.")
        self.step("t3", text("ok"))
        self.ev("turn_end", "t3", kind="answer", steps=1, usage={})
        self.assertEqual(assemble.fold_point(self.state, self.view), end1["seq"])  # a turn's end, not inside t2

    def test_fold_keeps_handles(self):
        """A fold over 3 turns and 4 handles keeps them and the previous summary's, with no title in the lines."""
        r0, end0 = self.turn("t0", "Earlier.", body=PAGES)
        self.ev("summary", "", first=1, upto=end0["seq"], text="The user asked earlier.", handles=[f"r:{r0['seq']}"],
                model=REF, usage={})
        handles = [r0["seq"]]
        for n in (1, 2, 3):
            self.say(f"t{n}", f"turn {n} " + "w " * (6500 if n == 3 else 3000))  # only t4 fits in 12,000
            self.step(f"t{n}", *[call(f"t{n}c{k}", "read_file", path="/tmp/x") for k in range(1 + (n == 2))])
            handles += [self.result(f"t{n}", f"t{n}c{k}", "read_file", PAGES)["seq"] for k in range(1 + (n == 2))]
            self.step(f"t{n}", text("ok"))
            end = self.ev("turn_end", f"t{n}", kind="answer", steps=2, usage={})
        self.turn("t4", "Newest.", thinking=True)
        view = self.view
        upto = assemble.fold_point(self.state, view)
        self.assertEqual(upto, end["seq"])
        req = assemble.fold_request(self.state, view, upto)
        said = req.messages[0]["content"][0]["text"]
        self.assertEqual((req.role, req.system, req.tools, req.max_tokens), ("summarize", assemble.FOLD_PROMPT, (), 2000))
        for part in ("The user asked earlier.", "user: turn 1", "user: turn 3", "call read_file", "result: {\"handle\""):
            self.assertIn(part, said)
        self.assertNotIn("Newest.", said)
        lines = assemble.handle_lines(self.state, view, upto)
        self.assertEqual(lines[0], f"r:{r0['seq']} read_file list 5")
        self.assertEqual([line.split()[0] for line in lines], [f"r:{h}" for h in handles])
        self.assertNotIn("Title", "\n".join(lines))
        summary = self.ev("summary", "", first=end0["seq"] + 1, upto=upto, text="Model\u200b text.\n" + "\n".join(lines),
                          handles=[line.split()[0] for line in lines], model=REF, usage={})
        after = assemble.View(id="root", role="root", session="s1", epoch=summary["seq"])
        msgs = self.messages(after)
        self.assertTrue(msgs[0]["content"][0]["text"].startswith(assemble.HEADER + "\nModel text.\nr:"))  # cleaned
        self.assertEqual(msgs[0]["content"][1], text("Newest."))
        self.assertNotIn(THINK, msgs[2]["content"])  # kept turns lose their thinking: the epoch is the summary
        seen = assemble.window_handles(self.state, after)
        self.assertLessEqual(set(handles), seen)
        self.assertTrue(results.read(self.state, seen, {"handle": f"r:{handles[2]}", "range": [0, 1]})[0])

    def test_single_assembler(self):
        """AST: only assemble.py imports `Request` from the llm package, calls anything ending in `Request(`
        (urllib's aside) or calls `dataclasses.replace` under any alias (`str.replace` is another call)."""
        pkg, found = Path(assemble.__file__).parent, set()
        for path in pkg.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
            modules = {a.asname or a.name for n in imports if isinstance(n, ast.Import) for a in n.names
                       if a.name == "dataclasses"}
            replaces = {a.asname or a.name for n in imports if isinstance(n, ast.ImportFrom)
                        and n.module == "dataclasses" for a in n.names if a.name == "replace"}
            for node in ast.walk(tree):
                f = node.func if isinstance(node, ast.Call) else None
                if (isinstance(node, ast.ImportFrom) and any(a.name == "Request" for a in node.names)
                        and (node.module in ("llm", "pathfinder.llm")
                             or (node.level and node.module is None and path.parent.name == "llm"))
                        or isinstance(f, ast.Name) and (f.id == "Request" or f.id in replaces)
                        or isinstance(f, ast.Attribute) and (
                            f.attr == "Request" and ast.unparse(f) != "urllib.request.Request"
                            or f.attr == "replace" and ast.unparse(f.value) in modules)):
                    found.add(str(path.relative_to(pkg)))
        self.assertEqual(found, {"assemble.py"})


if __name__ == "__main__":
    unittest.main()
