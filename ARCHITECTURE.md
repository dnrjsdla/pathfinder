# ARCHITECTURE

**Only what the code does today.** Why it is shaped this way: `docs/blueprint/` and
`docs/decisions/DECISIONS.md`. What measurement changed: `HISTORY.md`. Every name here exists in
the tree. Korean mirror: `ARCHITECTURE.ko.md` (updated in the same commit).

Built so far: **partition 3 (step 2 of its build order) — the world speaks.** The assistant
talks (1) and calls tools (2); now a schedule the user set, or a shell command that failed or
finished, becomes a `Signal`; cheap rules (`RULES.md`) decide speak/silence/defer/ask_model for
free, and only a `speak` opens a short model turn whose notice waits for the session to be free
before it goes out. Calendar, mail, the `attach`/`claude_code` senses and the fenced-judgment
model call are the next step; jobs, the memory vault and the TUI are later partitions still.

## 0. One sentence

Every fact is appended to one ledger first; everything else is derived from it and can be deleted.

```
client (nc -U, `pathfinder say|listen|signal|react|approve|deny|interrupt`)
   │  NDJSON over <home>/pathfinder.sock  (0600, no TCP)
   ▼
daemon.Daemon ── one lane (queue + worker) per session; lane items: say | resume | continue | signal
   │  record(): event = ledger.append(...)  →  state.apply(event)      (event-loop thread only)
   ├─▶ ledger.Ledger   <home>/events/<UTC date>.jsonl   append-only, the truth
   ├─▶ state.State     <home>/state.db                  derived, rebuildable (messages, approvals,
   │                                                     signals, decisions, judgments, deliveries)
   ├─▶ loop.run(step, messages, call_tool, emit, control)   in a worker thread (user and signal turns)
   │        ├─ step = llm.openai_compat.stream(endpoint, messages, tools, cancel=control)
   │        └─ call_tool = broker.Broker.call ── coerce → normalize → policy.decide → handler
   │                                               allow: run · ask: raise Gate · deny: DENIED_TEXT
   │                 tools.TOOLS: run_command · read_file · write_file · schedule's add/list/remove
   ├─▶ signals.SenseLoop ── polls senses/schedule.py on its cadence; senses/shell.py pushes instead
   │        push_signal → dedupe (state.seen) → lane's "signal" item → rules.decide → speak? → the
   │        one model turn above (origin "signal", the Signal-turns clause, no history, budget 4)
   └─▶ deliver.Outbox ── holds a notice while the turn is running or presence says "typing";
            released after every turn.end and every presence update; socket if attached, else banner
```

## 1. Where things live

| Place | What | Written by | If deleted |
|---|---|---|---|
| `<home>/events/<date>.jsonl` | every event, one JSON object per line | `ledger.Ledger.append()` only | history is lost — this is the only truth |
| `<home>/state.db` | sessions, messages, cursor, approvals | `state.State.apply()` | `pathfinder reindex`, or just restart: the daemon catches up |
| `<home>/config.json` | model names, providers, `grants`, `senses`, `schedules`, knobs | `config.set()` only | defaults (no model configured) |
| `<home>/workspace/` | the only place a sandboxed shell may write; cwd of every shell command; where a relative file path lands | tools, at the user's request | nothing of PATHFINDER's |
| `<home>/pathfinder.sock` | the daemon's socket | daemon | recreated at start |
| `<home>/RULES.md` | the speak/silence/defer/ask_model rules, one person can read | created from `pathfinder/prompts/RULES.md` on first start; edited by hand | rewritten from the shipped default on next start |
| `<home>/USER.md` | `USER.xxx` lists RULES.md conditions read; empty for now | created empty on first start; partition 5 fills it | an empty frontmatter |

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
error, config_set, approval_pending, approval, interrupted, signal, decision, judgment, delivery,
reaction, presence`. `config_set` records the key, never the value. A mid-turn steer is a `user`
event with `steer: true`. `tool_call` args and `tool_result` results are clipped to 2,000
characters; `approval_pending` carries the in-flight messages whole (a resume needs them). A
`signal` event's `trust` is the sense's own declaration (`ledger.TRUST`), never the data's own
claim; `decision` carries `signal_key, action, why` and, only for a `speak` that made it into a
notice, `notice_id, text, signal_keys` too (what `Outbox` rebuilds from after a restart).

## 3. Derived state (`pathfinder/state.py`)

`apply(event)` is the single projection, used live and by rebuild alike. It is idempotent
(ignores `seq <= cursor`) under `BEGIN IMMEDIATE` plus a lock, so a second process or a worker
thread cannot count a turn twice. A `gated` turn end does not count as an exchange.

**Only what was said becomes history:** a `user` event counts when `origin == "user"` (a steer
included), an `assistant` event when `final` is true. Table `approvals(id, call_id, session, turn,
tool, args_json, messages_json, ts, decision)`: `approval_pending` inserts, `approval` sets the
decision; `pending()` and `approval(id)` read it. `catch_up(ledger)` runs at daemon start;
`rebuild(ledger)` drops and replays — approvals come back with everything else.

Partition 3 adds `signals(key, ts, sense, kind)` (the 24 h dedupe: `seen(key, since)`),
`decisions(signal_key, ts, action, why, notice_id, session, text, signal_keys)`, `judgments(ts)`,
`deliveries(id, ts, session, via)`, `reactions(ts, delivery_id, vote)`, `presence(session, ts,
idle_s)` and `signal_turns(ts)` (one row per signal-origin `turn_start`, for the rate limit).
`today(now)` (`clock.day_start`-scoped) returns the counters `rules.decide` needs: `spoken`,
`judgments`, `last_signal_ts`, `burst` (signal turns in the last 60 s) and `kind_counts` (today's
decisions per kind, for `count_over`). `undelivered()` finds a `decision speak` with no matching
`delivery` — what `deliver.Outbox.rebuild()` restores after a crash. `rebuild(ledger)` drops and
replays these tables too.

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
sandboxed, reversible, normalize)`; `schema()` is the OpenAI tool dict; `coerce(spec, args)` casts
each declared argument to its JSON-schema type (the string `"false"` must not sail through as a
truthy escalation), drops undeclared keys, reports a missing required one. `normalize` runs after
coerce and before policy, so **the gate shows what will actually happen**: for `read_file` and
`write_file` a relative path becomes `<workspace>/path` and `~` expands; a param declaring
`"format": "local_datetime"` is canonicalized by `normalize_local_datetime(*keys)`, which parses
with `clock.parse_local` and lets its `ValueError` (a `Z` or an offset) become a tool error the
model can act on. Core tools: `run_command(command, escalate)` (write, sandboxed),
`read_file(path)`, `write_file(path, content)` (write) — both file tools refuse
`exec.SECRET_PATHS`. A sense may add more (schedule's `add_schedule`/`list_schedules`/
`remove_schedule` — see §8), merged into this same registry by `signals.load`.

**`policy.py`** — `decide(spec, origin, args, grants) -> allow | ask | deny`, no model, no shell
parsing; the only argument it reads is `escalate`. A sandboxed tool is judged as `sandboxed`
unless it escalates or the machine has no sandbox — then it is a `write`; a plain write that
declares `reversible=True` (D22) is judged as `reversible` instead of `write`.

| origin \ tool | read | sandboxed | network | write | reversible |
|---|---|---|---|---|---|
| `user` | allow | allow | allow | **ask** | **allow** |
| `signal` | allow | ask | ask | ask | ask |
| `job` | allow | ask | ask | ask | ask |
| `system` | allow | deny | deny | deny | deny |

`config.json` `grants` (`{tool: allow|ask|deny}`) wins over the table for `user` turns only, never
for a sandboxed tool that left its box (escalate, or no sandbox), and only with one of the three
words: config refuses anything else and policy ignores it — a typo must not switch the gate off.
`DENIED_TEXT` says it is policy, not a failure, so the model does not route around it. `reversible`
means a write the user can undo by deleting a row it made (`add_schedule`, `remove_schedule`) —
it runs by itself for the user, but a signal-opened turn (a mail that tries to plant one) still
asks, same as any other write.

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
  `approve {id}`, `deny {id}`, `signal {sense, fields}` (a push door — the sense must be loaded
  and push-capable, i.e. export `build(fields)`), `presence {idle_s}`, `react {vote, id?}`. Out:
  `attached`, `turn.start`, `delta`, `tool {name, ok}`, `gate {id, tool, args}`, `turn.end {kind}`,
  `notice {id, text, why}`, `error`. A malformed or unknown line, an unknown or already decided
  approval id, an interrupt with no running turn: an `error` line, the connection stays open.
- A session is a queue plus one worker (`_Lane`, a deque-backed queue — plain `asyncio.Queue` has
  no peek). Lane items: `("say", text)`, `("resume", approval_id)`, `("continue", steer_text)`,
  `("signal", Signal)`. `attach` resumes the latest session younger than `session_resume_s`.
- **The resident system prompt is `prompts/system.txt` + `signals.SIGNAL_TURNS_CLAUSE`, byte-stable
  across every turn, user and signal alike** (cache); a second system message, `clock.now_line()`
  ("Local time now: ..."), carries the only thing that changes turn to turn.
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

## 8. Signals, rules, delivery, senses (partition 3)

The signal flow, start to finish:

```
sense.poll(options, now) -> [Signal]           (schedule.py, every 30 s)
sense.build(fields, now) -> Signal             (shell.py, pushed by a hook via `signal`)
        │
        ▼
Daemon.push_signal ── record "signal" ── state.seen(key, 24h)? ── yes: "decision silence, seen"
        │ no                                                          (dropped, nothing queued)
        ▼
session lane: ("signal", Signal) — every consecutive one behind it drains into one bundle
        │
        ▼
Daemon.run_signal_turn ── signals.hold_for (rate limit) ── rules.decide per signal:
        defer / silence ──▶ recorded, done, free
        ask_model ──▶ judgment_cap? defer : rules.judge (stub) ──▶ recorded "judgment", then decided
        speak (all of them) ──▶ Daemon._signal_model_turn
                │
                ▼
        one model turn: origin "signal", Broker(origin="signal"), no history, budget 4,
        messages = system_prompt + now_line + signals.render_envelope(bundle)
        delivered text = sense summaries + the model's sentence (or summaries alone on
        LLMError/empty/Gate) ──▶ recorded "decision speak" (now with notice_id/text) ──▶
        deliver.Outbox.enqueue(Notice)
                │
                ▼
        held while the target session's turn runs or presence says "typing"; released after
        every turn.end and presence update ──▶ socket `notice` if attached, else a macOS banner
        ──▶ recorded "delivery" and "assistant {origin: signal, final: true}" (so history and a
        later `pathfinder listen` see it)
```

- **`clock.py`** — the only module besides `ledger.py` allowed to compute a date (`test_invariants`
  gives it no limit, everywhere else zero). `now_line()`, `local_day(ts)`, `day_start(ts)`,
  `hour(ts)`, `parse_local(text) -> (ts, canonical)` (refuses a `Z` or offset), `format_local(ts)`,
  `minutes_until`, `next_repeat`. Local time is always a naive wall-clock string; `time.mktime`
  turns it into the same epoch every other timestamp already uses.
- **`signals.py`** — `Signal(sense, kind, key, summary, fields, trust)`; `SIGNAL_ENVELOPE` and
  `SIGNAL_TURNS_CLAUSE`, copied verbatim from `bench/m3_injection.py` (m3, HISTORY.md: this exact
  wording is what keeps the model reading a signal without acting on an instruction hidden inside
  it — 0/20 injected, 5/5 controls). `render_envelope(signals)` is the envelope plus one
  `render_block` per signal (kind/trust/key/summary/fields-as-JSON) — the block's `trust` is
  written by the sense's own declaration, never by the data. `load(cfg, home)` imports each
  configured `pathfinder.senses.<name>`, checks it exports `OPTIONS`, a valid `TRUST`, and `KINDS`
  whose every row defaults to `silence` (a sense with no "stay quiet" case is refused), merges its
  `TOOLS` into the shared registry, and injects `home` into its options as `_home`. `SenseLoop`
  runs each loaded poll sense on its own `CADENCE_S` in a thread, catching a raising sense instead
  of dying. `seen_recently`/`hold_for` are the dedupe and rate-limit reads from `state`.
- **`rules.py`** — transplanted from Mark.5 `brain/rules.py` (`prepare` dropped, `model` renamed
  `ask_model`). `RULES.md`: frontmatter (`quiet_hours, daily_budget, min_interval_s, burst_limit,
  judgment_cap, idle_threshold_s`) plus one `## <kind>` table per kind, first match wins. The
  condition vocabulary is closed: `always, count_over, minutes_until_under, minutes_over` (core)
  plus whatever the loaded senses register; a section whose kind belongs to no loaded sense is
  parsed but not checked. `decide(signal, rules, user_lists, today, now)`: quiet hours -> defer;
  daily budget spent -> defer (a `trust: user` signal, e.g. a schedule, is exempt); then the
  table; no match -> `ask_model`. `judge(signal, cfg, today)` is a stub (D13's model call lands in
  step 3) returning `(False, "not implemented")` — the seam the daemon already calls through.
- **`deliver.py`** — `Notice(id, session, text, why, signal_keys)`; `Outbox.hold(session)` is true
  while `is_running(session)` or a fresh (< 60 s) `presence` row says `idle_s < idle_threshold_s`;
  `release()` sends every releasable notice in FIFO order, stopping at the first still held.
  `_deliver` records `delivery {id, via}` and `assistant {origin: signal, final: true}`, and sends
  a socket `notice` if any client is attached, else calls `banner` (a macOS `osascript`
  notification by default — injectable for tests). `rebuild()` restores the queue from
  `state.undelivered()`; nothing is auto-flushed until the next natural `release()` trigger, so a
  restart never pops a banner out of nowhere before anything else has happened.
- **`senses/`** — one file each, capped at 150 lines. `schedule.py`: reads `cfg["schedules"]`
  (poll, 30 s), fires a due row once, advances `at` through `config.set` for a repeat, drops a
  one-shot row; ships `add_schedule`/`list_schedules`/`remove_schedule` (the first and third
  `write=True, reversible=True`). `shell.py`: push-only (`build(fields, now)`), `command_failed`
  (exit != 0) / `command_finished` (exit == 0), registers `exit_nonzero` and `duration_over`; its
  dedupe key includes the arrival time, since a repeat is exactly what `count_over` needs to see.
  `docs/senses.md` has the zsh hook (backgrounded, errors swallowed — a hook must never block the
  shell) and a placeholder for the Claude Code hooks step 3 adds.
- **`config.py`** additions: `senses` (`{name: options}` — present key = on; `_check` imports the
  named sense module and validates its options against `OPTIONS`) and `schedules` (a list of
  `{id, at, repeat, text}`; `at` must already be `clock.parse_local`'s canonical string).

## 9. Invariants and the tests that hold them

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
| `signals.SIGNAL_ENVELOPE`/`SIGNAL_TURNS_CLAUSE` byte-identical to the bench that measured them (m3); no sense file over 150 lines; `rules.py` imports no llm module outside `judge` | `test_invariants.SignalEnvelopeInvariants`, `test_sense_files_under_150_lines`, `test_rules_imports_no_llm_at_top_level` |
| RULES.md's condition vocabulary is closed and validated per section; first match wins; the global gates (quiet hours, budget) run before the table; no match is `ask_model` | `test_rules.*` |
| A signal is deduped for 24 h; a burst holds the whole bundle; a sense with no default-silence `KINDS` row is refused; an unknown sense or option is refused by `config.set` | `test_signals.*`, `test_config.test_sense_options_checked_by_config` |
| A notice is held while its session's turn runs or presence says "typing", released in order after `turn.end`/`presence`, banners when no client is attached, and is rebuilt from `decision speak` rows the ledger still has no `delivery` for | `test_daemon.DeliverTest.*` |
| A signal turn's origin is `signal` (a write gates, never runs); the envelope never reaches `state.history`, the delivered text does; a blocked model still delivers the sense's summary | `test_daemon.SignalTurnTest.*` |
| The resident system prompt (`system.txt` + the Signal-turns clause) is byte-identical across turns; the local-time line is its own, separate system message | `test_daemon.ResidentPromptTest.test_resident_prompt_frozen` |
| D22: a reversible write runs by itself for the user, still asks for signal/job, still denied for system; an irreversible write is unchanged | `test_policy.test_policy_reversible` |
| `local_datetime` refuses a `Z`/offset and canonicalizes; a schedule fires once and a repeat advances; three same-kind failures speak, fewer stay silent | `test_tools.test_local_datetime_normalize`, `test_senses.*` |
| `state.db` deleted: `seen()` and `undelivered()` come back from the ledger alone | `test_daemon.ReindexRestoresSignalsTest.test_reindex_restores_seen_and_undelivered` |

## 10. How to count

```bash
python3 -m unittest discover -s tests            # all tests, no network (123)
wc -l pathfinder/*.py pathfinder/senses/*.py     # core; budgets: ledger 400, state 600, daemon 600,
                                                  # loop 400, broker+policy 350, exec 300, tools 300,
                                                  # clock+signals+rules 450, deliver 250, a sense file 150
python3 scripts/scan_personal.py --all           # personal-data scan over tracked files
```

## 11. Running it

```bash
python3 -m pathfinder config set model provider:model-name
python3 -m pathfinder daemon
python3 -m pathfinder say "hello"                 # a second `say` while one streams is a steer
python3 -m pathfinder listen                      # attach and print deltas and signal notices
python3 -m pathfinder approvals                   # pending gates, read from state.db
python3 -m pathfinder approve ID | deny ID        # resumes the gated turn and prints its answer
python3 -m pathfinder interrupt                   # stops the running turn of the latest session
python3 -m pathfinder config set grants '{"write_file": "allow"}'
python3 -m pathfinder config set senses '{"schedule": {}, "shell": {}}'
python3 -m pathfinder signal shell '{"cmd": "make test", "exit": 1, "seconds": 3, "cwd": "/tmp"}'
python3 -m pathfinder react up                    # thumbs-up the latest delivery
python3 -m pathfinder rules                       # print the path of RULES.md
python3 -m pathfinder reindex                     # refuses while a daemon is serving
```
