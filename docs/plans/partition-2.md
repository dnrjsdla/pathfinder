# Partition 2 — hands: tools, policy, sandbox, approval gate, steer, interrupt

**"It runs" =** (1) a write is gated: the gate question streams, the turn ends `gated`, the daemon
is killed and restarted, `pathfinder approvals` still lists it, `pathfinder approve <id>` performs
the write and the answer continues; (2) a `say` during a streaming answer is reflected in the
continuation without losing the thread; (3) `interrupt` kills a running shell command within a
second, the ledger has `interrupted`, the turn ends `cancelled`; (4) deleting `state.db` and
`reindex` brings the pending approval back.

Decisions this partition rests on: D11 (native tool calling), D12 + D17 (steer; a mid-turn `say` is
a steer), D16 (a gate ends the turn; approval starts a new one), D18 (m2 on two families), D19
(after m2: steer in both forms; the mid-stream steer travels in an envelope).

NOT in this partition: calendar/reminder/mail tools (partition 3, with their senses), the
`local_datetime` argument kind (lands with the first tool that takes a time, partition 3), signals
and rules (3), jobs (4), memory (5), MCP/foreign tools and `grants` for them (6), TUI (7).

Standard library only. Line budgets (blueprint §6): broker + policy 350, exec 300, tools 300,
daemon 600 total (about 340 left), loop 400 total (about 330 left). Expected this partition:
about 900 lines.

## ledger.py

`TYPES` gains `approval_pending`, `approval`, `interrupted`. A steer is recorded as a `user` event
with `steer: true` (it is something the user said, so it is history). `tool_call` and
`tool_result` are now actually written.

## config.py

`DEFAULTS` gains `"grants": {}` — tool name -> `"allow" | "ask" | "deny"`, the owner's word over
the policy table for their own turns. `_check` accepts a dict.

## tools.py — what the hands are (~150 lines)

- `@dataclass ToolSpec(name, description, params: dict, required: list, handler, write=False,
  network=False, sandboxed=False)`; `schema()` returns the OpenAI tool dict.
- `coerce(spec, args) -> (args, error)` — casts each declared property to its JSON-schema type
  (string / integer / number / boolean), drops undeclared keys, reports a missing required key or
  an uncastable value as an error string. Transplanted from Mark.5 `tools.coerce`.
- Native registry, three tools:
  - `run_command(command: str, escalate: bool = false)` — `write=True, sandboxed=True`. Handler
    is `exec.run_command`.
  - `read_file(path: str)` — reads a UTF-8 file, clipped to 8,000 characters (head and tail kept,
    the middle dropped). Refuses paths under `exec.SECRET_PATHS` (the same list the sandbox
    profile denies): reading is done by our own process, outside the sandbox, and a secret in the
    context is a secret one `curl` away.
  - `write_file(path: str, content: str)` — `write=True`. Creates parent directories. Not
    sandboxed, therefore gated for a user turn.
- `HANDLERS` receive a `control` (below) so a long tool can be killed.

## policy.py — who may call what (~100 lines)

`decide(spec, origin, args, grants) -> "allow" | "ask" | "deny"`. No model, no shell parsing:
the only argument it reads is `escalate`.

| origin \ tool | local read | sandboxed shell | `network` | `write` (incl. `escalate`) |
|---|---|---|---|---|
| `user` | allow | allow | allow | ask |
| `signal` | allow | ask | ask | ask |
| `job` | allow | ask | ask | ask |
| `system` | allow | deny | deny | deny |

- A sandboxed tool with `escalate` true is a write. A sandboxed tool on a machine where
  `exec.available()` is false is a write too (never run bare because the sandbox is missing).
- `grants[name]` wins for origin `user` only. Signal, job and system rows are fixed: the owner's
  "do not ask me about X" is permission for their own words, not for a mail subject that opened
  the turn.
- `DENIED_TEXT` is distinct from a failure: it says it is policy, not an error, and that retrying
  or working around it is pointless.

## exec.py — the shell in a box (~200 lines)

Transplanted from Mark.5 `exec/sandbox.py` (the shell profile, not the executor profile) and
`exec/shell.py`; the design and its measurements are in HISTORY.

- `SECRET_PATHS` — the one list of home-relative secrets (`.ssh`, `.aws`, `.gnupg`, `.config/gh`,
  `.docker`, `Library/Keychains`, `Library/Cookies`, `.netrc`, `.npmrc`, `.git-credentials`, shell
  rc files, `.env*`). The profile is generated from it; `read_file` checks against it.
- Seatbelt profile: `(deny default)`, `allow process-exec process-fork`, `allow file-read*` minus
  `SECRET_PATHS`, `allow network*`, the two mach-lookups, `allow file-write*` only under
  `WORKSPACE`, `TMPDIR`, `/private/tmp` and the dev nodes. Passed inline with `-p`, paths via `-D`.
- `CHILD_ENV_KEYS` allow-list (`PATH HOME LANG LC_ALL LC_CTYPE TZ TMPDIR`).
- `available()` — darwin and `/usr/bin/sandbox-exec` exists.
- `WORKSPACE = home / "workspace"` (created on first use, 0700). `cwd` is always the workspace.
- `run_command(command, escalate=False, *, control=None, timeout=60) -> dict` —
  `Popen(..., start_new_session=True)`; a watcher thread kills the whole process group when
  `control.interrupt` is set; `TimeoutExpired` kills it too. Returns `{exit_code, stdout, stderr,
  sandboxed, truncated?, killed?, hint?}` — a non-zero exit is an answer, not a failure; output
  is clipped to 8,000 characters; `hint` when the sandbox blocked something ("not permitted").
  `escalate` without an approval is impossible here because policy gates it first; the function
  itself just runs bare when asked.

## broker.py — the one door (~120 lines)

- `class Gate(Exception)`: `id, name, args` — raised when a call needs approval. The loop lets it
  propagate (its contract: exceptions propagate).
- `Broker(tools, origin, grants, *, control, emit, approved=frozenset(), denied=frozenset())`.
  `call(id, name, args) -> dict` is what the loop uses as `call_tool` (the signature grows by the
  call id):
  1. unknown name → `{"ok": false, "error": "no such tool: X"}` (worded unlike a denial);
  2. `coerce` → `{"ok": false, "error": why}`;
  3. `id in denied` → `{"ok": false, "denied": true, "error": "denied by the user"}`;
     `id in approved` → skip policy;
  4. `decide` → deny: `{"ok": false, "denied": true, "error": DENIED_TEXT}`; ask: raise `Gate`;
  5. `emit("tool_call", {id, name, args})`, run the handler, `emit("tool_result", {id, name, ok,
     result})`, return `{"ok": true, "result": ...}` or `{"ok": false, "error": str(e)}`.
- The broker never writes the ledger itself: it emits, and the daemon records on the event-loop
  thread (the single-writer rule of partition 1 holds).

## loop.py — steer and resume (+~80 lines)

- `class TurnControl`: `interrupt: threading.Event`, `steer_cut: threading.Event`,
  `steers: collections.deque[str]`, `in_tool: bool`; `is_set()` = interrupt or steer_cut, so the
  adapter's `cancel=` takes the control object unchanged.
- `run(step, messages, *, call_tool, emit, budget=8, control=None, resume=False) -> Outcome`
  - Before every model call: drain `control.steers` into `{"role": "user"}` messages.
  - `("cancelled", None)` from the step: if `control.interrupt` is set →
    `Outcome("cancelled")`; else (a steer cut) → append `{"role": "assistant", "content":
    partial}` if any text, clear `steer_cut`, drain steers **inside the envelope**
    (`STEER_ENVELOPE + text`: "[The user interrupted mid-answer with the instruction below.
    Apply it and continue from exactly where the answer above stopped; do not repeat what was
    already written.]" - the exact string measured in m2 run 3, one constant in `loop.py`),
    continue with the next step. A steer drained at a tool boundary is a plain user message
    (no envelope). A steer cut counts as a step (the budget is a cost ceiling).
  - `settle(messages, call_tool, emit, control)` executes the tool calls of the trailing
    assistant message that have no `tool` result yet (matched by `tool_call_id`), in order. `run`
    calls it after every step; with `resume=True` it calls it first, before any model call. A
    `Gate` propagates out of both, leaving `messages` with the results obtained so far.
  - `control.in_tool` is true while a handler runs. Steers that arrive then wait in the deque and
    are drained at the boundary; the daemon does not set `steer_cut` while `in_tool`.
- `Outcome.kind` gains no value: a gate is an exception, not an outcome.

## state.py (+~40 lines)

Table `approvals(id PRIMARY KEY, session, turn, tool, args_json, messages_json, ts, decision)`.
`approval_pending` inserts (decision NULL); `approval` sets `decision`. `pending() -> list[dict]`
(decision is NULL, oldest first) and `approval(id) -> dict | None` including `messages`. Rebuild
brings them back because both are ledger events.

## daemon.py (+~150 lines)

- Protocol in: `interrupt`, `approve {id}`, `deny {id}`. Out: `gate {id, tool, args}`,
  `turn.end {kind: "gated"}` in addition to the existing kinds.
- `Session.cancel` becomes `Session.control: TurnControl | None` (None between turns). The
  step factory signature is unchanged; it receives the control as `cancel`.
- `say` while a turn is running (`control is not None`): record `user {steer: true}`, append to
  `control.steers`, set `steer_cut` unless `control.in_tool`. Otherwise queue a turn as before.
- `interrupt`: set `control.interrupt`; the shell watcher kills the process group; the adapter
  stops; the loop returns `cancelled`; the daemon records `interrupted` then the usual
  `assistant {final}` (what was said) and `turn_end {kind: "cancelled"}`. Without a running turn:
  an `error` line, connection kept.
- Gate: `Gate` out of the thread → record `approval_pending {id, tool, args, messages}`, send
  `gate`, record `turn_end {kind: "gated"}`, send `turn.end`. The lane is free.
- `approve` / `deny`: the id must be pending in state, else `error`. Record
  `approval {id, decision}`, then put a resume item on the session's lane. A lane item is now
  `("say", text)` or `("resume", approval_id)`. The resume turn: `turn_start`, messages from the
  pending row, `Broker(approved={id})` or `Broker(denied={id})`, `loop.run(..., resume=True)`,
  then the same ending as any turn. A resumed turn can gate again (the next write).
- `emit` kinds from the worker thread: `delta`, `tool_call`, `tool_result` — the event-loop
  thread records the last two (`tool_call` / `tool_result` events with the tool result clipped to
  2,000 characters) and sends a `tool {name, ok}` line.
- The system prompt tells the model that a gated call returns after the user's decision and that
  `DENIED_TEXT` is final.

## __main__.py

`pathfinder approvals` (list pending: id, tool, args) · `pathfinder approve ID` · `pathfinder deny
ID` (attach, send, print until `turn.end`) · `pathfinder interrupt` (attach to the latest session,
send, print the reply). `say` learns nothing new: sending a second `say` while one streams is the
steer.

## Tests that must exist

- `test_policy_table` — every cell of the table above, plus `escalate`, missing sandbox, and
  `grants` winning for `user` only.
- `test_coerce` — casts, drops undeclared keys, missing required, uncastable value.
- `test_exec_blocks_write_outside_workspace`, `test_exec_denies_secret_read` (skipped unless
  `exec.available()`), `test_exec_interrupt_kills_process_group` (a `sleep 30` dies within 1 s),
  `test_exec_timeout`, `test_read_file_refuses_secret_paths`.
- `test_broker_unknown_deny_ask_allow` — four outcomes; `approved` skips policy; `denied` returns
  the user denial.
- `test_loop_steer_at_tool_boundary` — a steer queued during a fake tool call becomes a user
  message before the next step. `test_loop_steer_mid_stream` — a fake step that yields text then
  `cancelled` with `steer_cut` set: partial kept as assistant, steer appended, next step runs.
  `test_loop_interrupt_is_cancelled`. `test_loop_resume_settles_outstanding_calls` — a trailing
  assistant message with two calls, one already answered: only the other runs.
- `test_daemon_gate_survives_restart` — say → `gate` line → `turn.end gated` → close daemon →
  new `Daemon` on the same home → `state.pending()` has it → `approve` → tool ran → answer →
  ledger has `approval_pending`, `approval`, `tool_call`, `tool_result`.
  `test_daemon_deny_resumes_with_denial`. `test_daemon_say_during_turn_steers` (fake model that
  streams slowly; the second `say` shows up in the messages of the next step and as a `user
  {steer}` event). `test_daemon_interrupt_kills_tool` (fake tool blocks on `control.interrupt`).
  `test_reindex_restores_pending_approval`.
- `test_invariants` still passes: `exec.py` names no date, no model, no TCP.
