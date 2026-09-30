"""Jobs: a worker that drives one goal to a summary, off the user's own turn. `start_job` (a
`ToolSpec` restricted to origin "user" - a worker cannot make a worker) launches a thread that
runs `loop.run` against `prompts/job.txt` + the goal; `Manager` is the one place a job's
messages, control and outcome live between steps - bound once by the daemon (`jobs.bind`), the
same way `signals.load` binds `HOME` onto a sense module.

A job's write or network call gates exactly like a session turn's (D24): the messages `loop.run`
had built travel with the `approval_pending` event, its `job` id set so the row is known to
belong here, and `approve`/`deny` calls `resume()`, which re-enters `loop.run(resume=True)` on
those messages in a new thread - `daemon.resume_turn`, replayed for a job instead of a session."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import clock, config
from . import loop
from . import tools as tools_mod
from .broker import Broker, Gate
from .llm import LLMError, resolve
from .llm import slot as slot_mod
from .loop import TurnControl
from .senses.jobs import TRUST
from .signals import Signal
from .state import State
from .tools import ToolSpec

JOB_PROMPT = (Path(__file__).parent / "prompts" / "job.txt").read_text(encoding="utf-8")
_clip = tools_mod.clip_for_ledger  # same 2000-char, head-only bound as the daemon's own turns


def default_job_step_factory(cfg, control, tools_for_origin):
    """A job's own step: `worker_model` if set, else `model`; priority "job" (D25) - a job
    yields the slot between steps, so a user turn waits at most one job step."""
    endpoint = resolve(cfg, cfg.get("worker_model") or cfg["model"])
    schemas = [spec.schema() for spec in tools_for_origin.values()]
    return slot_mod.stream_step(endpoint, schemas, control, "job")


@dataclass
class Job:
    id: str
    goal: str
    session: str
    control: TurnControl
    messages: list
    started_ts: float
    state: str = "running"  # running|done|failed|cancelled|gated
    steps: int = 0
    usage: dict | None = None


_manager: "Manager | None" = None


def bind(manager: "Manager") -> None:
    global _manager
    _manager = manager


def _start_job(goal, control=None):
    if _manager is None:
        raise RuntimeError("jobs not bound (no Manager)")
    # `control` is this turn's own TurnControl - the daemon stamps `.session` onto it for user
    # turns (the only origin start_job is ever offered to), so this is the one way the handler
    # learns which session to report the outcome back to.
    session = getattr(control, "session", None) or ""
    return _manager.start(goal, session)


TOOLS = {
    "start_job": ToolSpec(
        name="start_job",
        description="Start a background worker on one goal; it runs on its own and reports back "
                    "with a summary when done.",
        params={"goal": {"type": "string", "description": "what the worker should do and report on"}},
        required=["goal"],
        handler=_start_job,
        origins=frozenset({"user"}),
    ),
}
tools_mod.TOOLS.update(TOOLS)


class Manager:
    def __init__(self, home, record, push, step_factory, rules, send=lambda msg: None, job_state=None):
        self.home = Path(home)
        self._record = record             # the daemon's ledger-then-state door
        self._push = push                 # daemon.push_signal
        self._send = send                 # a line to every screen (daemon._send_all)
        self.step_factory = step_factory
        self.rules = rules                # the daemon's own Rules (jobs_per_day, job_steps)
        # Marshals every `record`/`push` call onto the event-loop thread, like the daemon does
        # its own tool events - a job runs on its own thread, and both `state.apply` and a
        # pushed signal's `queue.put_nowait` are event-loop-thread-only. The daemon swaps this
        # for `loop.call_soon_threadsafe` once its loop is running (`start()`); until then (this
        # constructor's own restart sweep, and any standalone use) it just calls straight through.
        self.dispatch = lambda fn: fn()
        self.state = State(self.home / "state.db")     # read-only from here: record() writes it
        # job.end reads the row through the writer's connection (daemon.state.job): this one is
        # shared with turn threads, and its snapshot can predate the write just applied.
        self._job_state = job_state or self.state.job
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        """Closes this Manager's own read-only State connection - the daemon's `close()` calls
        this too, and a standalone test that built its own Manager should call it in tearDown."""
        self.state.close()

    def record(self, type, **fields):
        """The daemon's door, then the `job.start`/`job.gate`/`job.end` line a screen is owed
        (live: a job's gate was silent - only `approvals` knew)."""
        def go():
            self._record(type, **fields)
            if type == "job_start":
                self._send({"type": "job.start", "id": fields["id"], "goal": fields["goal"]})
            elif type == "approval_pending":
                self._send({"type": "job.gate", "id": fields["job"], "approval": fields["id"], "tool": fields["tool"], "args": fields["args"]})
            elif type in ("job_done", "job_failed"):  # the row exists: job_start came through this door
                self._send({"type": "job.end", "id": fields["id"], "state": self._job_state(fields["id"])["state"]})
            # a job's tool_call/tool_result/steer: ledger only
        self.dispatch(go)  # a closing loop is the daemon's dispatch to absorb

    def push(self, signal):
        self.dispatch(lambda: self._push(signal))

    def sweep(self):
        """A `job_start` with no end event survived a crash. One waiting on an approval (its id
        is on a pending row) is not abandoned - it is reconstructed as `gated` so `resume()`
        still finds it; anything else really was still running mid-step and is closed now.
        Called by the daemon from `start()`, not from here: the failure it pushes opens a
        session lane, which needs the running loop (live: the push was swallowed at __init__)."""
        gated_messages = {}
        for p in self.state.pending():
            if p.get("job"):
                gated_messages[p["job"]] = self.state.approval(p["id"])["messages"]
        for row in self.state.running_jobs():
            if row["id"] in gated_messages:
                self.jobs[row["id"]] = Job(
                    id=row["id"], goal=row["goal"], session=row["session"], control=TurnControl(),
                    messages=gated_messages[row["id"]], started_ts=row["started_ts"], state="gated")
            else:
                job = Job(id=row["id"], goal=row["goal"], session=row["session"], control=TurnControl(),
                         messages=[], started_ts=row["started_ts"])
                self._fail(job, "daemon restarted")

    # -- starting, stopping, steering ---------------------------------------------------
    def start(self, goal: str, session: str) -> str:
        with self._lock:
            if any(j.state in ("running", "gated") for j in self.jobs.values()):
                raise ValueError("a job is already running; wait for it to finish or stop it first")
            if self.state.today().jobs >= self.rules.jobs_per_day:  # refusals are failed calls, not "ok"
                raise ValueError("the day's job budget is spent; ask again tomorrow or raise "
                                 "jobs_per_day in RULES.md")
            id = uuid.uuid4().hex[:8]
            messages = [{"role": "system", "content": JOB_PROMPT},
                       {"role": "system", "content": clock.now_line()},
                       {"role": "user", "content": f"Goal: {goal}"}]
            job = Job(id=id, goal=goal, session=session, control=TurnControl(),
                      messages=messages, started_ts=time.time())
            self.jobs[id] = job
            self.record("job_start", session=session, origin="job", id=id, goal=goal)
            threading.Thread(target=self._drive, args=(job,), daemon=True).start()
            return f"started job {id}"

    def stop(self, id: str) -> None:
        job = self.jobs.get(id)
        if job is None:
            return
        job.control.interrupt.set()
        if job.state == "gated":
            # No thread is watching the flag: close the pending row as denied (review: a later
            # `approve` would otherwise run the write the user just stopped) and end the job here.
            for p in self.state.pending():
                if p.get("job") == id:
                    self.record("approval", session=p["session"], turn=p["turn"], origin="user", id=p["id"],
                                decision="denied", job=id)
            self._fail(job, "stopped")

    def steer(self, id: str, text: str) -> None:
        job = self.jobs.get(id)
        if job is None:
            return
        # The user said this, not the job (D12's own rule: a steer is what the user said) -
        # origin "user", with `job` naming which job it was said to.
        self.record("user", session=job.session, origin="user", job=id, trust="user", text=text, steer=True)
        job.control.steers.append(text)
        if not job.control.in_tool:  # mirrors daemon.steer: no cut while a tool handler is running
            job.control.steer_cut.set()

    def resume(self, approval_id: str, decision: str) -> None:
        """`decision` is "approved" or "denied" - the daemon already recorded the `approval`
        event; this only continues the job's own loop from where the gate left it."""
        row = self.state.approval(approval_id)
        if row is None or not row.get("job"):
            return
        job = self.jobs.get(row["job"])
        if job is None or job.state != "gated" or job.control.interrupt.is_set():
            # Nothing to resume into (a stopped job, or a row the sweep did not rebuild): end it
            # rather than leave a `running` row that blocks every later start_job.
            if job is None:
                r = self.state.job(row["job"])
                job = Job(id=row["job"], goal=r["goal"] if r else "", session=row["session"],
                          control=TurnControl(), messages=[], started_ts=r["started_ts"] if r else 0.0)
            if job.state in ("running", "gated"):
                self._fail(job, "stopped" if job.control.interrupt.is_set() else "internal: no job to resume")
            return
        approved = {row["call_id"]: (row["tool"], row["args"])} if decision == "approved" else {}
        denied = {row["call_id"]} if decision == "denied" else frozenset()
        job.state = "running"
        threading.Thread(target=self._drive, args=(job,),
                         kwargs={"resume": True, "approved": approved, "denied": denied}, daemon=True).start()

    # -- driving the loop ----------------------------------------------------------------
    def _drive(self, job, *, resume=False, approved=None, denied=frozenset()):
        cfg = config.load(self.home)
        job_tools = tools_mod.for_origin("job")
        step = self.step_factory(cfg, job.control, job_tools)
        broker = Broker(job_tools, "job", cfg.get("grants", {}), control=job.control,
                        emit=self._emit(job), approved=approved, denied=denied)
        try:
            out = loop.run(step, job.messages, call_tool=broker.call, emit=self._emit(job),
                           budget=self.rules.job_steps, control=job.control, resume=resume,
                           final_step=self.step_factory(cfg, job.control, {}))  # D30: no tools
        except Gate as g:
            self._gate(job, g)
            return
        except LLMError as e:
            self._fail(job, f"model: {e.message}")
            return
        except Exception as e:  # noqa: BLE001 - a bug in one job must not take the daemon down
            self._fail(job, f"internal: {type(e).__name__}")
            return
        if out.kind == "cancelled":
            self._fail(job, "stopped")
        else:
            self._done(job, out)

    def _emit(self, job):
        def emit(kind, payload):
            if kind not in ("tool_call", "tool_result"):
                return  # "delta" text is not streamed anywhere for a job - only its own summary
            recorded = dict(payload)
            if "args" in recorded:
                recorded["args"] = _clip(recorded["args"])
            if "result" in recorded:
                recorded["result"] = _clip(recorded["result"])
            self.record(kind, session=job.session, origin="job", job=job.id, **recorded)
        return emit

    def _gate(self, job, g: Gate) -> None:
        approval_id = uuid.uuid4().hex[:12]
        self.record("approval_pending", session=job.session, origin="job", id=approval_id,
                    call_id=g.id, tool=g.name, args=g.arguments, messages=job.messages, job=job.id)
        job.state = "gated"  # after recording: a caller polling `job.state` must see the row too

    def _done(self, job, out) -> None:
        job.steps, job.usage = out.steps, out.usage
        summary = _clip(out.text)
        fields = {"id": job.id, "summary": summary, "steps": out.steps, "usage": out.usage}
        if out.kind == "exhausted":
            fields["exhausted"] = True
        self.record("job_done", session=job.session, origin="job", **fields)
        # The summary line IS the notice text (the model's added sentence may be missing): the
        # job's own summary rides in it, and not again in `fields` - the envelope would carry it twice.
        self.push(Signal(sense="jobs", kind="job_done", key=f"job:{job.id}:done",
                         summary=f"Job done: {job.goal}\n{summary}",
                         fields={"goal": job.goal, "exhausted": out.kind == "exhausted"},
                         trust=TRUST, exempt=True))
        job.state = "done"  # after recording/pushing: a caller polling `job.state` sees both too
        job.messages = []   # a finished job's context is in the ledger, not in memory for the daemon's life

    def _fail(self, job, reason: str) -> None:
        self.record("job_failed", session=job.session, origin="job", id=job.id, reason=reason)
        self.push(Signal(sense="jobs", kind="job_failed", key=f"job:{job.id}:failed",
                         summary=f"Job failed: {job.goal} — {reason}",
                         fields={"goal": job.goal, "reason": reason}, trust=TRUST, exempt=True))
        job.state = "cancelled" if reason == "stopped" else "failed"
        job.messages = []
