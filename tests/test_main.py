"""The CLI's turn commands (`say`, `approvals`, `approve`, `deny`) against a real daemon and a fake model."""
import asyncio
import io
import json
from contextlib import redirect_stderr, redirect_stdout

from pathfinder import __main__ as cli
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
        self.assertEqual(listed, (0, f"{gate['seq']}  write_file  {json.dumps(gate['args'])}\n", ""))
        self.assertEqual(decided, (0, "Written. \n", ""))
        self.assertEqual(note.read_text(), "hi")
        self.assertEqual(after, (0, "", ""))  # decided and reported: no longer listed

    def test_deny_prints_the_report_turn(self):
        note, gate, said, listed, decided, after = self.gated("deny", "Not written, as you asked.")
        self.assertEqual(decided, (0, "Not written, as you asked. \n", ""))
        self.assertFalse(note.exists())
        self.assertEqual(after, (0, "", ""))
