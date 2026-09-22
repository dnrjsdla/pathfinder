# ARCHITECTURE

**Only what the code does today.** Why it is shaped this way: `docs/blueprint/` and
`docs/decisions/DECISIONS.md`. What measurement changed: `HISTORY.md`. Every name here exists in
the tree. Korean mirror: `ARCHITECTURE.ko.md` (updated in the same commit).

Built so far: **partition 2 — hands.** The assistant talks (1), and now calls tools, keeps a
shell in a sandbox, asks before a write, takes a word mid-turn and stops when told (2). No
signals, jobs, memory or installer yet.

## 0. One sentence

Every fact is appended to one ledger first; everything else is derived from it and can be deleted.

```
client (nc -U, `pathfinder say|approve|deny|interrupt`)
   │  NDJSON over <home>/pathfinder.sock  (0600, no TCP)
   ▼
daemon.Daemon ── one lane (queue + worker) per session; lane items: say | resume | continue
   │  record(): event = ledger.append(...)  →  state.apply(event)      (event-loop thread only)
   ├─▶ ledger.Ledger   <home>/events/<UTC date>.jsonl   append-only, the truth
   ├─▶ state.State     <home>/state.db                  derived, rebuildable (messages, approvals)
   └─▶ loop.run(step, messages, call_tool, emit, control)   in a worker thread
            ├─ step = llm.openai_compat.stream(endpoint, messages, tools, cancel=control)
            └─ call_tool = broker.Broker.call ── coerce → normalize → policy.decide → handler
                                                   allow: run · ask: raise Gate · deny: DENIED_TEXT
                     tools.TOOLS: run_command (exec.py, Seatbelt) · read_file · write_file
```

## 1. Where things live

| Place | What | Written by | If deleted |
|---|---|---|---|
| `<home>/events/<date>.jsonl` | every event, one JSON object per line | `ledger.Ledger.append()` only | history is lost — this is the only truth |
| `<home>/state.db` | sessions, messages, cursor, approvals | `state.State.apply()` | `pathfinder reindex`, or just restart: the daemon catches up |
| `<home>/config.json` | model names, providers, `grants`, knobs | `config.set()` only | defaults (no model configured) |
| `<home>/workspace/` | the only place a sandboxed shell may write; cwd of every shell command; where a relative file path lands | tools, at the user's request | nothing of PATHFINDER's |
| `<home>/pathfinder.sock` | the daemon's socket | daemon | recreated at start |

`<home>` is `$PATHFINDER_HOME` or `~/.pathfinder` (mode 0700).

## 2. The ledger (`pathfinder/ledger.py`)

- `partition(ts)` maps a timestamp to a UTC date. It is the **only** place a time becomes a file
  name. Readers never compute dates; they iterate partitions in order and filter by `seq`.
- An event has the reserved fields `seq, ts, type, session, turn, origin, trust` plus data.
  `seq` is global and strictly increasing. `type`, `origin` and `trust` come from closed sets;
  anything else is rejected at `append()`.
- Two processes may append (the daemon, and the CLI running `config set`): each append holds a
  thread lock and an exclusive `flock` on `events/.lock`, and re-derives the last `seq` from the
  file tail when the newest partition changed size behind its back.
- Crash recovery: the last `seq` is found by walking partitions newest-first until one yields a
  good line; a fragment without a newline is closed with one before the next append. `read()`
  skips unparsable lines.
- Known ceiling: `flush()` without `fsync` — an OS crash can lose the last line.

Event types today: `session_start, user, assistant, tool_call, tool_result, turn_start, turn_end,
error, config_set, approval_pending, approval, interrupted`. `config_set` records the key, never
the value. A mid-turn steer is a `user` event with `steer: true`. `tool_call` args and
`tool_result` results are clipped to 2,000 characters; `approval_pending` carries the in-flight
messages whole (a resume needs them).

## 3. Derived state (`pathfinder/state.py`)

`apply(event)` is the single projection, used live and by rebuild alike. It is idempotent
(ignores `seq <= cursor`) under `BEGIN IMMEDIATE` plus a lock, so a second process or a worker
thread cannot count a turn twice. A `gated` turn end does not count as an exchange.

**Only what was said becomes history:** a `user` event counts when `origin == "user"` (a steer
included), an `assistant` event when `final` is true. Table `approvals(id, call_id, session, turn,
tool, args_json, messages_json, ts, decision)`: `approval_pending` inserts, `approval` sets the
decision; `pending()` and `approval(id)` read it. `catch_up(ledger)` runs at daemon start;
`rebuild(ledger)` drops and replays — approvals come back with everything else.

## 4. The model layer (`pathfinder/llm/`)

- `resolve(cfg, "provider:model")` splits on the first colon and returns an `Endpoint`. Model
  names exist only in `config.json`; an unset model is a clear error.
- `openai_compat.stream(endpoint, messages, tools, cancel)` speaks the OpenAI-compatible
  chat-completions SSE protocol and yields `text | reasoning | tool_calls | usage | cancelled`.
  `cancel` is anything with `is_set()`, checked between chunks. Every failure is an `LLMError`;
  `blocked` is true for 402/429. The daemon's step factory always passes the tool schemas: the
  model must see its hands (the first live run found it did not - `test_default_step_factory_sends_tool_schemas`).

## 5. Tools, policy, sandbox, broker

**`tools.py`** — `ToolSpec(name, description, params, required, handler, write, network,
sandboxed, normalize)`; `schema()` is the OpenAI tool dict; `coerce(spec, args)` casts each
declared argument to its JSON-schema type (the string `"false"` must not sail through as a
truthy escalation), drops undeclared keys, reports a missing required one. `normalize` runs after
coerce and before policy, so **the gate shows what will actually happen**: for `read_file` and
`write_file` a relative path becomes `<workspace>/path` and `~` expands. Three tools:
`run_command(command, escalate)` (write, sandboxed), `read_file(path)`, `write_file(path,
content)` (write). Both file tools refuse `exec.SECRET_PATHS`.

**`policy.py`** — `decide(spec, origin, args, grants) -> allow | ask | deny`, no model, no shell
parsing; the only argument it reads is `escalate`. A sandboxed tool is judged as `sandboxed`
unless it escalates or the machine has no sandbox — then it is a `write`.

| origin \ tool | read | sandboxed | network | write |
|---|---|---|---|---|
| `user` | allow | allow | allow | **ask** |
| `signal` | allow | ask | ask | ask |
| `job` | allow | ask | ask | ask |
| `system` | allow | deny | deny | deny |

`config.json` `grants` (`{tool: allow|ask|deny}`) wins over the table for `user` turns only, never
for a sandboxed tool that left its box (escalate, or no sandbox), and only with one of the three
words: config refuses anything else and policy ignores it — a typo must not switch the gate off.
`DENIED_TEXT` says it is policy, not a failure, so the model does not route around it.

**`exec.py`** — the Mark.5 Seatbelt profile (HISTORY has its measurements): `(deny default)`,
process exec/fork, read everything except `SECRET_PATHS` under HOME (`.ssh .aws .gnupg .config/gh
.docker Library/Keychains Library/Cookies .netrc .npmrc .git-credentials` the shell rc files and
`.env*`), network open, writes only under the workspace, TMPDIR, `/private/tmp` and the dev nodes.
The child environment is an allow-list (`PATH HOME LANG LC_ALL LC_CTYPE TZ TMPDIR`).
`run_command(command, escalate, control, timeout=60)`: `Popen` in its own session, a watcher
thread kills the whole process group the moment `control.interrupt` is set, output clipped to
8,000 characters, a non-zero exit is an answer, a "not permitted" gets a hint that the sandbox
blocked it. `available()` is darwin plus `/usr/bin/sandbox-exec`.

**`broker.py`** — `Broker(tools, origin, grants, control, emit, approved, denied)`;
`call(id, name, args)` is the loop's `call_tool`: unknown tool → error (worded unlike a denial);
coerce → error; a denied id → `USER_DENIED_TEXT`; an approved id whose `(name, args)` match what
the user approved runs once and is consumed; otherwise `policy.decide`: deny → `DENIED_TEXT`,
ask → `raise Gate(id, name, arguments)`, allow → `emit("tool_call")`, run the handler with
`control`, `emit("tool_result")`. The broker never writes the ledger. `Gate.arguments`, not
`.args`: `BaseException.args` turns a dict into a tuple of its keys.

## 6. The loop (`pathfinder/loop.py`)

`run(step, messages, *, call_tool, emit, budget, control, resume)` drives one turn to
`Outcome("answer" | "exhausted" | "cancelled")`; a gate is an exception, not an outcome.
`TurnControl` is the one handle: `interrupt` and `steer_cut` events, a `steers` deque, `in_tool`;
`is_set()` is either event, so the adapter's `cancel=` takes it unchanged.

- Before every model call: drain queued steers into one plain user message, clear `steer_cut`,
  and return `cancelled` if `interrupt` is set — the real adapter only notices `cancel` after
  the request went out, so this check is what keeps an interrupt from spending a request.
- A stream that ends `cancelled` with `steer_cut` set is a **mid-stream steer**: the partial
  text becomes an assistant message, the steers are appended inside `STEER_ENVELOPE` (the exact
  string measured in m2), and the loop continues. With `interrupt` set it is a cancel.
- `settle(messages, call_tool, control)` runs the calls of the trailing assistant message that
  have no result yet, in order, with `in_tool` set around each; `resume=True` settles first.
  A `Gate` out of `settle` carries `said` (the text streamed before it) and propagates.
- Every outcome carries all text streamed in the turn. Reasoning is never echoed into history.

## 7. The daemon (`pathfinder/daemon.py`)

- Protocol, one JSON object per line. In: `attach {session?}`, `say {text}`, `interrupt`,
  `approve {id}`, `deny {id}`. Out: `attached`, `turn.start`, `delta`, `tool {name, ok}`,
  `gate {id, tool, args}`, `turn.end {kind}`, `error`. A malformed or unknown line, an unknown
  or already decided approval id, an interrupt with no running turn: an `error` line, the
  connection stays open.
- A session is a queue plus one worker. Lane items: `("say", text)`, `("resume", approval_id)`,
  `("continue", steer_text)`. `attach` resumes the latest session younger than `session_resume_s`.
- **A `say` while a turn is running is a steer** (D17): recorded as `user {steer: true}`, appended
  to `control.steers`, and `steer_cut` is set unless a tool handler is running (`in_tool`) — a
  stream is cut, a tool is not. A steer that arrives after the model's last chunk is not lost:
  when the turn ends with steers still queued, the lane runs a `continue` turn that appends them
  as the trailing user message without recording them again.
- **Interrupt** sets `control.interrupt`: the shell watcher kills the process group, the adapter
  stops at the next chunk, the loop returns `cancelled`; the daemon records `interrupted`, then
  `assistant {final}` (what was said), then `turn_end {kind: "cancelled"}`.
- **Gate** (D16): the `Gate` propagates out of the worker; the daemon records the text said so
  far as `assistant {final}`, drains leftover steers into the messages, records
  `approval_pending {id, call_id, tool, args, messages}` under an id of its own (the model's
  call id repeats across turns and is a string the model chose), sends `gate`, records
  `turn_end {kind: "gated"}`. The lane is free. `approve`/`deny` need a pending id in state,
  record `approval {id, decision}`, and put a `resume` item on that approval's session lane;
  the resumed turn replays the stored messages with `Broker(approved={call_id: (tool, args)})`
  or `denied={call_id}` and `loop.run(resume=True)`. A resumed turn can gate again.
- `emit` kinds from the worker thread: `delta`, `tool_call`, `tool_result`; the event-loop thread
  records the last two and sends `tool`. All ledger and state writes happen on that thread.
- The daemon survives a blocked model: `LLMError` becomes an `error` event, an `error` line and
  `turn.end {kind: "error"}`. The socket is born 0600; a live one is refused.

## 8. Invariants and the tests that hold them

| Invariant | Test |
|---|---|
| One ledger writer, one partition function, one config writer, no date math elsewhere | `test_invariants.test_single_writer` |
| Closed vocabulary; 13-day session reads back whole; two processes never share a `seq`; `seq` survives a truncated newest partition | `test_ledger.*` |
| State is rebuildable, apply is idempotent, a turn is counted once by two processes; only what was said is remembered | `test_state.*` |
| Conversation and **pending approvals** survive deleting `state.db` | `test_daemon.test_daemon_resumes_after_state_deleted`, `test_reindex_restores_pending_approval` |
| The daemon survives a blocked or unset model | `test_daemon.test_daemon_survives_model_402`, `test_unset_model_is_an_error_not_a_crash` |
| The real step factory sends the tool schemas and the control | `test_daemon.RealStepFactoryTest` |
| A gate survives a daemon restart; deny resumes with the denial; two gates with the same call id are both resolvable and the gate text reaches history | `test_daemon.test_daemon_gate_survives_restart`, `test_daemon_deny_resumes_with_denial`, `ReviewRegressionsTest` |
| An approval is single-use and exact | `test_broker.test_broker_approval_is_single_use_and_exact` |
| Policy table cell by cell; a grant never covers leaving the sandbox; a typo fails closed | `test_policy.*`, `test_config` |
| A steer at a tool boundary is a plain user message; mid-stream it keeps the partial and wears the envelope; one that lands after the last chunk gets its own turn | `test_loop.test_loop_steer_*`, `test_daemon.test_daemon_say_during_turn_steers`, `test_steer_after_the_last_chunk_gets_its_own_turn` |
| An interrupt kills the process group within a second (bare and sandboxed) and spends no further request | `test_exec.test_exec_interrupt_*`, `test_loop.test_loop_interrupt_before_a_step_spends_no_request`, `test_daemon.test_daemon_interrupt_kills_tool` |
| The sandbox blocks a write outside the workspace and a secret read; `read_file`/`write_file` refuse secret paths | `test_exec.*`, `test_tools.*` |
| What was streamed reaches history even when the budget runs out; mid-stream failures are `LLMError`; reasoning never echoed | `test_daemon.test_exhausted_turn_still_records_what_was_said`, `test_llm.*`, `test_loop.test_loop_does_not_echo_reasoning` |
| Socket 0600, unix only, no model IDs or TCP in source | `test_daemon.test_socket_perms`, `test_invariants.test_no_model_ids_or_tcp_in_source` |

## 9. How to count

```bash
python3 -m unittest discover -s tests            # all tests, no network (84)
wc -l pathfinder/*.py                            # core; budgets: ledger 400, state 600, daemon 600, loop 400, broker+policy 350, exec 300, tools 300
python3 scripts/scan_personal.py --all           # personal-data scan over tracked files
```

## 10. Running it

```bash
python3 -m pathfinder config set model provider:model-name
python3 -m pathfinder daemon
python3 -m pathfinder say "hello"                 # a second `say` while one streams is a steer
python3 -m pathfinder approvals                   # pending gates, read from state.db
python3 -m pathfinder approve ID | deny ID        # resumes the gated turn and prints its answer
python3 -m pathfinder interrupt                   # stops the running turn of the latest session
python3 -m pathfinder config set grants '{"write_file": "allow"}'
python3 -m pathfinder reindex                     # refuses while a daemon is serving
```
