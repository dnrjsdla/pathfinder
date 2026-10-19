"""The CLI's turn commands (`say`, `approvals`, `approve`, `deny`) against a real daemon and a fake model."""
import asyncio
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pathfinder import __main__ as cli
from pathfinder import config
from pathfinder import tools as tools_mod
from pathfinder.tools import ToolSpec
from tests.test_daemon import DaemonHarness, call, fake_model, text


class CliTest(DaemonHarness):
    def cli(self, fn, *args):
        """(exit code, stdout, stderr) of one CLI function run against this home."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(self.home, *args)
        return code, out.getvalue(), err.getvalue()

    def gated(self, decision, reply):
        """say a write (gated), list the approvals, decide the gate: each command's output."""
        note = self.home / "note.txt"
        router = fake_model(reply, [call("c1", "write_file", path=str(note), content="hi")], [text("I asked for approval.")])

        async def scenario(daemon):
            said = await asyncio.to_thread(self.cli, cli.say, "please write a note")
            [gate] = [e for e in self.events() if e["type"] == "gate"]
            listed = self.cli(cli.approvals)
            decided = await asyncio.to_thread(self.cli, cli.decide, decision, str(gate["seq"]))
            return gate, said, listed, decided, self.cli(cli.approvals)

        return (note, *self.run_with(router, scenario))

    def test_say_reads_past_the_gate_and_approve_prints_the_report_turn(self):
        note, gate, said, listed, decided, after = self.gated("approve", "Written.")
        self.assertEqual(said[:2], (0, "I asked for approval. \n"))  # a gate never ends the turn (V22)
        self.assertIn(f"pathfinder approve {gate['seq']}", said[2])
        self.assertEqual(listed, (0, f"{gate['seq']}  write_file  write  {json.dumps(gate['args'])}\n", ""))
        self.assertEqual(decided, (0, "Written. \n", ""))
        self.assertEqual(note.read_text(), "hi")
        self.assertEqual(after, (0, "", ""))  # decided and reported: no longer listed

    def test_deny_prints_the_report_turn(self):
        note, gate, said, listed, decided, after = self.gated("deny", "Not written, as you asked.")
        self.assertEqual(decided, (0, "Not written, as you asked. \n", ""))
        self.assertFalse(note.exists())
        self.assertEqual(after, (0, "", ""))

    def test_approve_flags(self):
        """`approve N --always tool` grants the gated write and says from when; `--always server` on a native,
        `--grade` on a graded gate, `--always` on an unknown gate and a grade that is not one are one-line
        refusals that record nothing and leave the gate open; `--grade read` answers an unknown gate into the
        effect map and runs the call once; a malformed flag is a usage error."""
        note = self.home / "note.txt"
        op_spec = ToolSpec(name="mcp__fake__op", description="", params={"action": {"type": "string"}}, required=[],
                           handler=lambda action, control=None: {"done": action})  # no map, no annotations: unknown
        self.addCleanup(tools_mod.TOOLS.pop, "mcp__fake__op", None)
        router = fake_model("Done.", [call("c1", "write_file", path=str(note), content="hi"),
                                      {"type": "tool_use", "id": "c2", "name": "call_tool",  # `call` takes no `name` arg
                                       "input": {"name": "mcp__fake__op", "arguments": {"action": "frob"}}}],
                            [text("I asked for approval.")])

        async def scenario(daemon):
            await daemon.mcp.loaded  # the load drops every mcp__ name it did not attach
            tools_mod.TOOLS[op_spec.name] = op_spec
            await asyncio.to_thread(self.cli, cli.say, "write a note and frob")
            write, op = [e for e in self.events() if e["type"] == "gate"]
            refused = [self.cli(cli.decide, "approve", str(write["seq"]), "server"),
                       self.cli(cli.decide, "approve", str(write["seq"]), "", "read"),
                       self.cli(cli.decide, "approve", str(op["seq"]), "tool"),
                       self.cli(cli.decide, "approve", str(op["seq"]), "", "harmless")]
            recorded = [e["type"] for e in self.events() if e["type"] in ("resume", "config_set")]
            listed = self.cli(cli.approvals)
            granted = await asyncio.to_thread(self.cli, cli.decide, "approve", str(write["seq"]), "tool")
            graded = await asyncio.to_thread(self.cli, cli.decide, "approve", str(op["seq"]), "", "read")
            return write, op, refused, recorded, listed, granted, graded

        write, op, refused, recorded, listed, granted, graded = self.run_with(router, scenario)
        self.assertEqual([r[:2] for r in refused], [(1, "")] * 4)
        self.assertEqual([r[2].count("\n") for r in refused], [1] * 4)  # one line each
        self.assertIn("write_file is a native tool", refused[0][2])
        self.assertIn("is graded write", refused[1][2])
        self.assertIn("is unknown: no --always covers it", refused[2][2])
        self.assertIn("--grade takes one of", refused[3][2])
        self.assertEqual(recorded, [])  # nothing recorded: both gates still open
        self.assertEqual(listed[1].splitlines(), [f"{write['seq']}  write_file  write  {json.dumps(write['args'])}",
                                                  f"{op['seq']}  mcp__fake__op  unknown  {json.dumps(op['args'])}"])
        self.assertEqual(granted[0], 0)
        self.assertEqual(granted[2], "pathfinder: writes by write_file run without asking from the next turn\n")
        self.assertEqual(graded[:2], (0, "Done. \n"))
        cfg = config.load(self.home)
        self.assertEqual(cfg["grants"]["write_file"], "allow")
        self.assertEqual(cfg["tools"], {"fake": {"op": {"effect": "read"}}})
        self.assertEqual([(e["gate"], e.get("scope"), e.get("grade")) for e in self.events() if e["type"] == "resume"],
                         [(write["seq"], "tool", None), (op["seq"], None, "read")])
        self.assertEqual(note.read_text(), "hi")
        self.assertEqual([e["ok"] for e in self.events() if e["type"] == "result" and e.get("gate") == op["seq"]], [True])
        with mock.patch.dict(os.environ, {"PATHFINDER_HOME": str(self.home)}), redirect_stderr(io.StringIO()):
            for argv in (["approve", "1", "--always"], ["deny", "1", "--always", "tool"],
                         ["approve", "1", "--always", "tool", "--always", "session"], ["approve", "1", "--scope", "tool"]):
                self.assertEqual(cli.main(argv), 2, argv)
