# Partition 4 — jobs: workers, start/done/failed signals, a shared budget

**"It runs" =** (1) "research X and summarize it" makes the model call `start_job`; the answer
"started job <id>" streams at once; a second `say` while the job runs is answered while the job's
`tool_call` events keep landing in the ledger (two flows interleaved); when the job ends a
`job_done` notice arrives on `pathfinder listen` with the summary; (2) `pathfinder jobs` lists the
running and finished jobs; `pathfinder job stop ID` kills a job's running shell command within a
second and the job ends `cancelled`; (3) a job's `write_file` gates: `pathfinder approvals` shows it
with `job: <id>`, `approve` resumes **the job** (not a session turn) and the job finishes; (4) the
daemon is killed with a job running: on restart the job is closed as `job_failed {reason: daemon
restarted}` and that speaks; (5) `pathfinder job steer ID "text"` reaches the job before its next
model call; (6) m7 and m7a are in HISTORY.

Decisions this partition rests on: D8, D16 (approval = end the turn, resume from the ledger),
D20 (a `speak` adds one model step), D23–D25 (this partition's three).

NOT in this partition: worker-to-worker messages, job priorities, saving results to files by
itself (a write is a gated tool call like any other), the catalog (6), memory (5), the TUI (7).

Line budgets (blueprint §6): `jobs.py` 350 (new). `daemon.py` is at 598 of 600: the job runner,
its gate handling and its signals live entirely in `jobs.py`; the daemon gains only the protocol
lines and the two hooks below, about 30 lines, ending near 630 — over its allocation and inside
the 1.3x line, reported at the end of the partition as §6 requires. `llm/` gains `slot.py`
(adapter budget). Expected: about 450 core lines plus two benches and tests.

## Entrance measurements

### m7a — what the free plan does with two overlapping requests (`bench/m7a_concurrency.py`)

Two streaming requests started 0.5 s apart on the default model, three times: record each one's
first-token latency, status, and whether the second waited for the first (queued), failed (429 or
other), or ran alongside. About twelve requests. This decides how strict D25's lock must be: if
the second queues or fails, the lock is load-bearing; if both run, it is cheap insurance and the
job may keep the slot for a whole step.

### m7 — does an evaluator pass earn its cost? (`bench/m7_evaluator.py`, open path from partition 2)

Ten research-shaped goals (find and compare, gather and summarize, check and report), each run
twice through the job loop of this partition's `jobs.py` (the bench imports it; mock network
tools return canned pages so the two conditions see the same world): **without** an evaluator,
and **with** one evaluator pass — a second model request that reads the goal and the summary,
answers closed JSON `{"adequate": bool, "missing": "..."}`, and when not adequate sends the job one
more round with `missing` as a steer (one round, never two). Twenty outputs saved to
`docs/measurements/m7-*.json` with the condition hidden behind a random key. `--grade` prints the
twenty summaries blind (goal + summary, shuffled) for a grader to mark 👍/👎 into a votes file; the
grader is an opus subagent (D7: a subagent is a grader, not a runtime), and the owner spot-checks
five pairs. `--score` unblinds and reports thumbs-up per condition, tokens per condition, and
seconds per condition. **D23:** the evaluator row is added to `jobs.py` only if the with-evaluator
condition earns at least three more 👍 out of ten; otherwise the evaluator code stays out and
the open path is closed in HISTORY with the numbers. Cost: about 100 requests, roughly 0.6M
tokens (m6 priced a job at about 29k input tokens).

## ledger.py

`TYPES` gains `job_start`, `job_done`, `job_failed`. A job's `tool_call`/`tool_result` events carry
`origin: "job"` and a `job` field. `approval_pending` gains an optional `job` field.

## llm/slot.py — one model call at a time, the user first (D25, ~40 lines)

`slot(priority)` is a context manager around one streaming request: a `threading.Condition`;
a `user` caller waits only for the current holder; a `job` caller waits while any user is waiting
or holding. `openai_compat.stream` is unchanged; the daemon's step factory wraps it for user and
signal turns with `priority="user"`, `jobs.py` wraps it with `priority="job"`. Because a job
yields between steps (the slot is released when its stream ends), a user turn waits at most one
job step. If m7a shows both requests run freely, the slot stays: it costs nothing and it is what
keeps a job from doubling the day's rate when partition 5's consolidation pass joins.

## tools.py — registries per origin (~20 lines)

`for_origin(origin) -> dict` filters `TOOLS` by a new `ToolSpec.origins` (default: all four).
`start_job` declares `origins={"user"}`: the job's and the signal turn's registries do not contain
it, so "a worker cannot make a worker" and "a mail cannot start a job" are structure, not policy.
The broker and the step factory both take the filtered dict (the model only sees what it may
call).

## jobs.py — the worker (~300 lines)

- `Job` dataclass: `id, goal, session, control: TurnControl, messages, started_ts, steps,
  usage, state: running|done|failed|cancelled|gated`.
- `Manager(home, record, push, step_factory)`, bound once by the daemon (`jobs.bind(manager)`
  like `signals.load` binds `HOME`). `record` is the daemon's ledger-then-state door (called on
  the loop thread through `call_soon_threadsafe`, like tool events today); `push` is
  `daemon.push_signal`.
- `start(goal, session) -> id`: refuses with a plain string when `today.jobs >= jobs_per_day`
  ("the day's job budget is spent; ask again tomorrow or raise jobs_per_day in RULES.md") or when
  a job is already running (one at a time: the free plan has one slot, and D25 already
  serializes calls; a queue of jobs is not asked for). Records `job_start {id, goal}`, spawns a
  thread that runs `loop.run(step, messages, call_tool=broker.call, emit, budget=job_steps,
  control)` with messages = `prompts/job.txt` (the worker's own resident text: "you are a worker
  with one goal; there is no user to ask; when done, answer with a summary the user can act on;
  you cannot start jobs") + the local-time line + `Goal: ...`. `Broker(origin="job",
  tools=tools.for_origin("job"))`. Model: `worker_model or model`. `step` wraps the adapter
  stream in `slot("job")`.
- Outcome `answer` → `job_done {id, summary: text clipped to 2,000}`; `exhausted` → `job_done`
  with the text so far and `exhausted: true`; `cancelled` → `job_failed {reason: stopped}`;
  `LLMError` → `job_failed {reason: model: ...}`; any other exception → `job_failed {reason:
  internal: <type>}`. Every end pushes the matching signal (below) and frees the slot for the next
  `start`.
- Gate (D24 = a): `Gate` out of the loop → `approval_pending {id, job, call_id, tool, args,
  messages, session}`, `job_start`'s state becomes `gated`, the thread ends. `approve`/`deny`
  on an approval whose row has `job` set calls `Manager.resume(approval_id)`: a new thread runs
  `loop.run(..., resume=True)` on the stored messages with `approved={call_id: (tool, args)}` or
  `denied`, exactly as `resume_turn` does for a session turn. A resumed job can gate again.
- `stop(id)`: sets `control.interrupt` (the shell watcher kills the process group; the adapter
  stops between chunks). `steer(id, text)`: `control.steers.append(text)` and `steer_cut` unless
  `in_tool` — partition 2's primitive, unchanged; recorded as `user {steer: true, job: id}`.
- On `Manager` construction: every `job_start` in state with no end event is closed with
  `job_failed {reason: daemon restarted}` and pushed as a signal (a crashed daemon does not lose
  the fact that a job was running; its messages are not kept — restarting the job is the user's
  call).
- The evaluator row, if m7 earns it: after `answer`, one request `{"adequate", "missing"}` on the
  same model with no tools; when not adequate, one more `loop.run` round with `missing` drained
  as a steer; the second answer is final. Off by default until measured.

## senses/jobs.py (push sense, ~40 lines)

Kinds `job_done` (fields `goal, summary, exhausted`) and `job_failed` (`goal, reason`), summary
lines `Job done: <goal>` / `Job failed: <goal> — <reason>`, TRUST `user` (the user asked for the
job; §8: the outcome of what the user started spends no budget and honours no quiet hours),
registers no condition words. Shipped `RULES.md` sections: both `always → speak`. The signal
turn runs as for any speak (D20): the notice is the summary line plus the job's summary, and the
model may add its one step (typically "want it saved?" — a write it cannot do without a gate).
Not a socket push sense: the daemon calls `push_signal` from `Manager` directly, so the socket
door refuses `signal jobs ...` from outside.

## rules.py / RULES.md

Frontmatter gains `jobs_per_day: 10` and `job_steps: 20`. `state.today()` gains `jobs` (count of
`job_start` today, local day). No new condition words.

## state.py (+~40 lines)

Table `jobs (id, session, goal, started_ts, ended_ts, state, summary, reason, steps, usage)`
projected from the three job events; `approvals` gains a `job` column; `jobs()` (list, newest
first), `job(id)`, `running_jobs()` (for the restart sweep).

## daemon.py (+~30 lines)

Protocol in: `job.stop {id}`, `job.steer {id, text}`; out: `job.start {id, goal}`, `job.end {id,
state}` to the job's session connections. `approve`/`deny`: if the approval row has `job`, hand
it to `jobs.resume` instead of the session lane. The step factory takes the origin's tool dict and
wraps the stream in `slot("user")`. `push_signal` is passed to the `Manager` at init.

## __main__.py

`pathfinder jobs` (id, state, started, goal), `pathfinder job stop ID`, `pathfinder job steer ID
TEXT`. `approvals` prints `job: <id>` when set.

## prompts/job.txt

The worker's resident text (English, byte-stable across a job). Says: one goal, no user to ask,
answer with a summary the user can act on, read tools run, writes and network go to the gate and
the gate's answer comes back to you, you cannot start jobs, never invent facts you did not read.

## Tests that must exist

- `test_slot_user_first` (a job holding the slot, a user waiting, another job waiting: order user
  → job), `test_slot_released_between_steps`.
- `test_tools_for_origin` (`start_job` in `user` only; the job registry has the read tools and
  the write tools, never `start_job`).
- `test_job_runs_to_summary` (fake step: two tool calls then an answer → `job_start`, `tool_call`
  ×2 with `origin: job`, `job_done`, the `job_done` signal pushed with the summary),
  `test_job_refuses_over_budget`, `test_job_one_at_a_time`, `test_job_stop_kills_tool` (a fake
  tool blocking on `control.interrupt`), `test_job_steer_reaches_next_step`,
  `test_job_gate_pauses_and_resumes` (gate → `approval_pending {job}` → approve → the write runs
  → `job_done`; deny → the job continues with the denial), `test_job_gate_survives_restart`,
  `test_restart_closes_running_jobs` (a `job_start` with no end → `job_failed {daemon restarted}`
  → signal), `test_job_llm_error_is_failed` (no daemon death), `test_job_exhausted_is_done_with_flag`.
- `test_daemon_answers_while_job_runs` (fake job step sleeps; a `say` gets its answer first).
- `test_job_done_speaks_without_budget` (budget spent, quiet hours → still speak: trust user).
- `test_reindex_restores_jobs_and_job_approvals`.
- `test_invariants`: `jobs.py` <= 350; `senses/jobs.py` <= 150; the socket refuses `signal jobs`;
  only `ledger.append` writes events; no date maths outside `ledger`/`clock`.

## Build order

1. `bench/m7a_concurrency.py` → run (twelve requests) → HISTORY → decide how strict `slot` is.
2. `llm/slot.py`, `tools.for_origin`, `ledger`, `state`, `jobs.py` without the evaluator,
   `senses/jobs.py`, `prompts/job.txt`, daemon hooks, CLI, tests → live check (1)–(5).
3. `bench/m7_evaluator.py` on top of the working `jobs.py` (mock tools) → run → grade (opus
   subagent, blind) → owner's five → score → HISTORY; add the evaluator row only if D23's margin
   is met.
4. Reviewer pass → scan → commit → push → draft PR #5 on `partition-3`.
