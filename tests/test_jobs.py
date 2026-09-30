"""`jobs.Manager` end to end against fake step factories - no daemon, no socket. `record`/`push`
here are the test's own, backed by a real Ledger+State on a temp home, so a "restart" is just a
second Manager built against the same home, exactly as a second Daemon would be."""
import dataclasses
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pathfinder import jobs
from pathfinder import rules as rules_mod
from pathfinder import tools as tools_mod
from pathfinder.ledger import Ledger
from pathfinder.llm import LLMError
from pathfinder.state import State
from pathfinder.tools import ToolSpec


def _wait_until(pred, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


def _steps(steps):
    """steps: a list of lists of (kind, payload) tuples, one per call to step(messages) -
    shaped like a real step_factory: (cfg, control, tools) -> step(messages)."""
    calls = iter(steps)

    def factory(cfg, control, tools):
        def step(messages):
            return iter(next(calls))
        return step
    return factory


class JobsHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.pushed = []
        self._managers = []

    def tearDown(self):
        for mgr in self._managers:
            mgr.close()
        self.state.close()
        self.tmp.cleanup()

    def record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        return event

    def push(self, signal):
        self.pushed.append(signal)

    def manager(self, step_factory, rules=None):
        mgr = jobs.Manager(self.home, self.record, self.push, step_factory, rules or rules_mod.Rules())
        self._managers.append(mgr)
        return mgr

    def events(self, type=None):
        evs = list(self.ledger.read())
        return [e for e in evs if type is None or e["type"] == type]


class JobRunsTest(JobsHarness):
    def test_job_runs_to_summary(self):
        calls = {"n": 0}

        def handler(control=None):
            calls["n"] += 1
            return {"ok": True}

        tools_mod.TOOLS["job_test_tool"] = ToolSpec(
            name="job_test_tool", description="", params={}, required=[], handler=handler)
        self.addCleanup(tools_mod.TOOLS.pop, "job_test_tool", None)

        factory = _steps([
            [("tool_calls", [{"id": "c1", "name": "job_test_tool", "arguments": {}}])],
            [("tool_calls", [{"id": "c2", "name": "job_test_tool", "arguments": {}}])],
            [("text", "here is the summary")],
        ])
        mgr = self.manager(factory)
        result = mgr.start("find X", "sess1")
        self.assertTrue(result.startswith("started job "))
        job_id = result.split()[-1]

        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state != "running"))
        self.assertEqual(mgr.jobs[job_id].state, "done")
        self.assertEqual(calls["n"], 2)

        types = [e["type"] for e in self.events()]
        self.assertEqual(types.count("tool_call"), 2)
        self.assertEqual(types.count("tool_result"), 2)
        self.assertTrue(all(e["origin"] == "job" and e["job"] == job_id for e in self.events("tool_call")))
        self.assertIn("job_start", types)
        self.assertIn("job_done", types)
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0].kind, "job_done")
        # the notice text is the summary line itself - the job's summary must be in it (live: a
        # blocked model would otherwise leave "Job done: <goal>" and nothing else)
        self.assertIn("here is the summary", self.pushed[0].summary)
        self.assertNotIn("summary", self.pushed[0].fields)  # not twice in the envelope

    def test_job_exhausted_is_done_with_flag(self):
        """A job that never stops calling tools hits its own step budget - `done`, not
        `failed`, with `exhausted: true` on the event (contract: never a fifth Job.state)."""
        bad_path = str(self.home / "does-not-exist.txt")

        def factory(cfg, control, tools):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "read_file", "arguments": {"path": bad_path}}])
            return step

        mgr = self.manager(factory, rules=dataclasses.replace(rules_mod.Rules(), job_steps=2))
        result = mgr.start("goal", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state != "running"))
        self.assertEqual(mgr.jobs[job_id].state, "done")
        done = self.events("job_done")
        self.assertTrue(done[-1].get("exhausted"))
        self.assertTrue(self.pushed[-1].fields["exhausted"])

    def test_job_llm_error_is_failed(self):
        def factory(cfg, control, tools):
            def step(messages):
                raise LLMError(402, "no credit")
                yield  # pragma: no cover
            return step

        mgr = self.manager(factory)
        result = mgr.start("goal", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state != "running"))
        self.assertEqual(mgr.jobs[job_id].state, "failed")
        failed = self.events("job_failed")
        self.assertEqual(failed[-1]["reason"], "model: no credit")


class JobBudgetTest(JobsHarness):
    def test_job_refuses_over_budget(self):
        rules = dataclasses.replace(rules_mod.Rules(), jobs_per_day=0)
        mgr = self.manager(_steps([[("text", "x")]]), rules=rules)
        with self.assertRaisesRegex(ValueError, "budget"):
            mgr.start("goal", "s1")
        self.assertEqual(len(self.events("job_start")), 0)

    def test_start_job_refusal_is_a_failed_call(self):
        """U23: a refused start raises, so the model gets `ok: false` and the tool record says
        `failed`, not `ok` for a job that never started."""
        from pathfinder.broker import Broker
        from pathfinder.state import RECORD_HEADER
        self.addCleanup(jobs.bind, jobs._manager)
        jobs.bind(self.manager(_steps([[("text", "x")]]), rules=dataclasses.replace(rules_mod.Rules(), jobs_per_day=0)))
        emitted = []
        broker = Broker(jobs.TOOLS, "user", {"start_job": "allow"}, control=None,
                        emit=lambda kind, payload: emitted.append((kind, payload)))
        out = broker.call("c1", "start_job", {"goal": "research"})
        self.assertFalse(out["ok"])
        self.assertIn("the day's job budget is spent", out["error"])
        self.record("session_start", session="s1")
        for kind, payload in emitted:
            self.record(kind, session="s1", turn="t1", origin="user", **payload)
        record = self.state.history("s1")[-1]["content"]
        self.assertTrue(record.startswith(RECORD_HEADER))
        self.assertEqual(record.splitlines()[-1], 'start_job {"goal":"research"} failed')

    def test_job_one_at_a_time(self):
        block = threading.Event()
        self.addCleanup(block.set)

        def factory(cfg, control, tools):
            def step(messages):
                block.wait(2)
                yield ("text", "done")
            return step

        mgr = self.manager(factory)
        r1 = mgr.start("goal 1", "s1")
        self.assertTrue(r1.startswith("started"))
        with self.assertRaisesRegex(ValueError, "already running"):
            mgr.start("goal 2", "s1")

        block.set()
        job_id = r1.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "done"))


class JobLinesTest(JobsHarness):
    def test_record_builds_the_three_job_lines(self):
        """U4: `record` records, then sends the line a screen is owed; a steer is ledger only."""
        lines = []
        mgr = jobs.Manager(self.home, self.record, self.push, None, rules_mod.Rules(), send=lines.append)
        self._managers.append(mgr)
        mgr.record("job_start", session="s1", origin="job", id="j1", goal="research")
        mgr.record("user", session="s1", origin="user", job="j1", trust="user", text="faster", steer=True)
        mgr.record("approval_pending", session="s1", origin="job", id="a1", call_id="c1", tool="search",
                   args={"q": "x"}, messages=[], job="j1")
        mgr.record("job_done", session="s1", origin="job", id="j1", summary="found", steps=1, usage={})
        self.assertEqual(lines, [
            {"type": "job.start", "id": "j1", "goal": "research"},
            {"type": "job.gate", "id": "j1", "approval": "a1", "tool": "search", "args": {"q": "x"}},
            {"type": "job.end", "id": "j1", "state": "done"}])
        self.assertEqual([e["type"] for e in self.events()], ["job_start", "user", "approval_pending", "job_done"])

    def test_job_end_reads_the_passed_state(self):
        """U4 review: job.end's state comes from the writer's getter, not this Manager's own
        connection (shared with turn threads, its snapshot went stale under load)."""
        lines = []
        mgr = jobs.Manager(self.home, self.record, self.push, None, rules_mod.Rules(), send=lines.append,
                           job_state=lambda id: {"state": "from-the-writer"})
        self._managers.append(mgr)
        mgr.record("job_start", session="s1", origin="job", id="j1", goal="research")
        mgr.record("job_failed", session="s1", origin="job", id="j1", reason="stopped")
        self.assertEqual(lines[-1], {"type": "job.end", "id": "j1", "state": "from-the-writer"})


class JobControlTest(JobsHarness):
    def test_job_stop_kills_tool(self):
        def blocking_handler(control=None):
            if control is not None:
                control.interrupt.wait(2)
            return {"stopped": True}

        tools_mod.TOOLS["job_blocking_tool"] = ToolSpec(
            name="job_blocking_tool", description="", params={}, required=[], handler=blocking_handler)
        self.addCleanup(tools_mod.TOOLS.pop, "job_blocking_tool", None)

        def factory(cfg, control, tools):
            def step(messages):
                if control.interrupt.is_set():
                    yield ("cancelled", None)
                    return
                if not any(m.get("role") == "tool" for m in messages):
                    yield ("tool_calls", [{"id": "c1", "name": "job_blocking_tool", "arguments": {}}])
                else:
                    yield ("text", "back")
            return step

        mgr = self.manager(factory)
        result = mgr.start("goal", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].control.in_tool))

        start_t = time.monotonic()
        mgr.stop(job_id)
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state != "running"))
        self.assertLess(time.monotonic() - start_t, 2.0)
        self.assertEqual(mgr.jobs[job_id].state, "cancelled")
        self.assertEqual(self.events("job_failed")[-1]["reason"], "stopped")

    def test_job_steer_reaches_next_step(self):
        seen = []
        proceed = threading.Event()
        self.addCleanup(proceed.set)

        def handler(control=None):
            proceed.wait(2)
            return {"ok": True}

        tools_mod.TOOLS["job_steer_tool"] = ToolSpec(
            name="job_steer_tool", description="", params={}, required=[], handler=handler)
        self.addCleanup(tools_mod.TOOLS.pop, "job_steer_tool", None)

        def factory(cfg, control, tools):
            def step(messages):
                seen.append([dict(m) for m in messages])
                if len(seen) == 1:
                    yield ("tool_calls", [{"id": "c1", "name": "job_steer_tool", "arguments": {}}])
                else:
                    yield ("text", "done")
            return step

        mgr = self.manager(factory)
        result = mgr.start("goal", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].control.in_tool))

        mgr.steer(job_id, "focus on Y")
        proceed.set()
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "done"))

        last_user = [m for m in seen[1] if m.get("role") == "user"]
        self.assertEqual(last_user[-1]["content"], "focus on Y")
        steer_events = [e for e in self.events("user") if e.get("steer")]
        self.assertEqual(len(steer_events), 1)
        self.assertEqual((steer_events[0]["origin"], steer_events[0]["job"]), ("user", job_id))


class JobGateTest(JobsHarness):
    def test_job_gate_pauses_and_resumes_approve(self):
        note_path = str(self.home / "job-note.txt")

        def factory(cfg, control, tools):
            def step(messages):
                if any(m.get("role") == "tool" for m in messages):
                    yield ("text", "wrote it")
                else:
                    yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                            "arguments": {"path": note_path, "content": "hi"}}])
            return step

        mgr = self.manager(factory)
        result = mgr.start("write a note", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "gated"))

        pending = self.state.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["job"], job_id)
        approval_id = pending[0]["id"]

        self.record("approval", session="s1", origin="user", id=approval_id, decision="approved")
        mgr.resume(approval_id, "approved")
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "done"))
        self.assertEqual(Path(note_path).read_text(), "hi")

    def test_job_gate_pauses_and_resumes_deny(self):
        note_path = str(self.home / "job-note-denied.txt")

        def factory(cfg, control, tools):
            def step(messages):
                if any(m.get("role") == "tool" for m in messages):
                    yield ("text", "not writing it")
                else:
                    yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                            "arguments": {"path": note_path, "content": "hi"}}])
            return step

        mgr = self.manager(factory)
        result = mgr.start("write a note", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "gated"))

        approval_id = self.state.pending()[0]["id"]
        self.record("approval", session="s1", origin="user", id=approval_id, decision="denied")
        mgr.resume(approval_id, "denied")
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "done"))
        self.assertFalse(Path(note_path).exists())

    def test_job_stop_while_gated_closes_row_and_never_writes(self):
        """Review blocker: a gated job has no thread to see `interrupt`; `stop` must close the
        pending row itself, or a later `approve` runs the write the user just stopped."""
        note_path = str(self.home / "job-note-stopped.txt")

        def factory(cfg, control, tools):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": "hi"}}])
            return step

        mgr = self.manager(factory)
        job_id = mgr.start("write a note", "s1").split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "gated"))
        approval_id = self.state.pending()[0]["id"]
        mgr.stop(job_id)
        self.assertEqual(mgr.jobs[job_id].state, "cancelled")
        self.assertEqual(self.state.pending(), [])  # the row is closed (denied), not left pending
        self.assertEqual([e["job"] for e in self.ledger.read() if e["type"] == "approval"], [job_id])  # D63
        self.assertEqual(self.state.job(job_id)["state"], "cancelled")
        self.assertEqual([s.kind for s in self.pushed], ["job_failed"])
        mgr.resume(approval_id, "approved")  # a stale approve does nothing
        time.sleep(0.05)
        self.assertFalse(Path(note_path).exists())
        next_id = mgr.start("next", "s1").split()[-1]  # the slot is free again
        self.assertTrue(_wait_until(lambda: mgr.jobs[next_id].state == "gated"))  # settle before tearDown

    def test_job_steer_is_not_chat_history(self):
        """Review should-fix: a steer aimed at a worker must not open the next session turn
        as a dangling user message."""
        block = threading.Event()

        def factory(cfg, control, tools):
            def step(messages):
                block.wait(2)
                yield ("text", "done")
            return step

        mgr = self.manager(factory)
        job_id = mgr.start("goal", "s1").split()[-1]
        mgr.steer(job_id, "also check the other thing")
        block.set()
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "done"))
        self.assertEqual(self.state.history("s1"), [])
        self.assertEqual(len([e for e in self.events("user") if e.get("steer")]), 1)

    def test_job_gate_survives_restart(self):
        note_path = str(self.home / "job-note-restart.txt")

        def gate_factory(cfg, control, tools):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": note_path, "content": "hi"}}])
            return step

        mgr1 = self.manager(gate_factory)
        result = mgr1.start("write a note", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr1.jobs[job_id].state == "gated"))

        def answer_factory(cfg, control, tools):
            def step(messages):
                yield ("text", "done")
            return step

        # "restart": a fresh Manager against the same home, as a fresh Daemon would build
        mgr2 = self.manager(answer_factory)
        mgr2.sweep()
        self.assertEqual(mgr2.jobs[job_id].state, "gated")  # reconstructed, not failed
        self.assertEqual(len(self.events("job_failed")), 0)

        approval_id = [p["id"] for p in self.state.pending() if p["job"] == job_id][0]
        self.record("approval", session="s1", origin="user", id=approval_id, decision="approved")
        mgr2.resume(approval_id, "approved")
        self.assertTrue(_wait_until(lambda: mgr2.jobs[job_id].state == "done"))
        self.assertEqual(Path(note_path).read_text(), "hi")


class JobRestartSweepTest(JobsHarness):
    def test_restart_closes_running_jobs(self):
        block = threading.Event()
        self.addCleanup(block.set)

        def factory(cfg, control, tools):
            def step(messages):
                block.wait(2)
                yield ("text", "done")
            return step

        mgr1 = self.manager(factory)
        result = mgr1.start("goal", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr1.jobs[job_id].control is not None))

        # a fresh Manager on the same home, as if the daemon had crashed mid-step (no gate)
        self.manager(_steps([[("text", "x")]])).sweep()
        failed = self.events("job_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"], "daemon restarted")
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0].kind, "job_failed")

        block.set()  # let mgr1's own thread finish before tearDown closes the state under it
        self.assertTrue(_wait_until(lambda: mgr1.jobs[job_id].state == "done"))


class ReindexRestoresJobsTest(JobsHarness):
    def test_reindex_restores_jobs_and_job_approvals(self):
        def factory(cfg, control, tools):
            def step(messages):
                yield ("tool_calls", [{"id": "c1", "name": "write_file",
                                        "arguments": {"path": str(self.home / "n.txt"), "content": "x"}}])
            return step

        mgr = self.manager(factory)
        result = mgr.start("goal", "s1")
        job_id = result.split()[-1]
        self.assertTrue(_wait_until(lambda: mgr.jobs[job_id].state == "gated"))

        self.state.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.home / "state.db") + suffix)
            if p.exists():
                p.unlink()
        self.state = State(self.home / "state.db")
        self.state.rebuild(self.ledger)

        row = self.state.job(job_id)
        self.assertEqual((row["state"], row["goal"]), ("gated", "goal"))  # paused at its gate
        pending = self.state.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["job"], job_id)


if __name__ == "__main__":
    unittest.main()
