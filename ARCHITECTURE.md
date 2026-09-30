# ARCHITECTURE

**Only what the code does today.** Why it is shaped this way: `docs/blueprint/` and
`docs/decisions/DECISIONS.md`. What measurement changed: `HISTORY.md`. Every name here exists in
the tree. Korean mirror: `ARCHITECTURE.ko.md` (updated in the same commit).

Built so far: **partition 5 — memory.** The assistant talks (1), calls tools (2), the world speaks
to it (3), and jobs run off its own turn (4); now it keeps a memory vault - `USER.md`, `RULES.md`
and `wiki/*.md` - that only `memory.Manager` writes, past a no-model gate (`vet`) that checks
every edit before it lands. A wiki edit applies at once; a `USER.md`/`RULES.md` edit goes to the
same approval box a tool gate uses (D28: a user-turn grant can let it apply at once too). A
consolidation pass (`Manager.run_pass`), off the user's own turn like a job, reads the ledger since
the last pass and writes down what's worth keeping - a wiki line, a fact the user stated in their
own words, or (after repeated judgments) a `RULES.md` row proposal - and folds an old session into
one summary. `USER.md`'s body and the wiki map ride in every turn's second system message;
`search_memory`/`read_memory` let the model look the rest up. `RULES.md` now reloads without a
restart, on an approved edit or a hand edit. The catalog and the TUI are later partitions still.

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
| `<home>/config.json` | model names, providers, `grants`, `senses`, `schedules`, `mcp_servers`, knobs | `config.set()` only (the installer too) | defaults (no model configured) |
| `<home>/workspace/` | the only place a sandboxed shell may write; cwd of every shell command; where a relative file path lands | tools, at the user's request | nothing of PATHFINDER's |
| `<home>/pathfinder.sock` | the daemon's socket | daemon | recreated at start |
| `<home>/RULES.md` | the speak/silence/defer/ask_model rules, one person can read | created from `pathfinder/prompts/RULES.md` on first start; edited by hand, or by an approved `edit_memory` row; either way reloads without a restart | rewritten from the shipped default on next start |
| `<home>/USER.md` | `USER.xxx` lists RULES.md conditions read, plus a free body (resident context) | created empty on first start; filled by approved `edit_memory` rows (or a user-turn grant's auto-apply, D28) | an empty frontmatter - memory starts from zero |
| `<home>/wiki/*.md` | wiki pages, one topic per file | `memory.Manager.apply` (`edit_memory`, applies at once) | the knowledge is gone - files are the truth |
| `~/.claude.json` (the user's, not `<home>`) | Claude Code's user-scope `mcpServers` (D33) - one entry per approved install, secrets as `${VAR}` references | `installer.Installer` only, atomically (the resolved target of a symlink, in its own mode), after a byte copy to `~/.claude.json.pathfinder-<ts>` | Claude Code's own concern; the installer restores from its backup on rollback and uninstall |
| `~/Library/LaunchAgents/pathfinder.daemon.plist` | the launchd agent: the running interpreter `-m pathfinder daemon`, a literal `PATH`, `PATHFINDER_HOME` when set, `KeepAlive`, log to `<home>/daemon.log`, `WorkingDirectory` = the package's parent | `init` (plistlib), after a yes | `pathfinder service start` fails until `init` is run again |
| `<home>/daemon.log` | the daemon's stderr under launchd | launchd | nothing |
| `<home>/hooks/pathfinder.zsh` and one guarded line in `~/.zshrc` | the shell sense's `preexec`/`precmd` hook (`docs/senses.md`), sourced only if the file exists | `init`, after a yes; `~/.zshrc` is backed up to `~/.zshrc.pathfinder-<ts>` and appended, never rewritten | the shell sense hears nothing |
| `~/.claude/settings.json` (`hooks.Stop`, `hooks.Notification`) | the Claude Code sense's two hook entries | `init` through `config.merge_json_file` (D60): backup, atomic write, read-back; idempotent by the `pathfinder signal claude_code` marker | Claude Code's own concern |
| macOS Keychain `pathfinder-mcp-<id>` / `pathfinder-mail` | every secret (an MCP server's token, the IMAP password) | the user, by hand (`security add-generic-password`) | the server stays listed as `disabled: secret missing: <NAME>`; the mail sense errors |

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
reaction, presence, memory_pass, memory_apply, session_summary`. `config_set` records the key,
never the value. A mid-turn steer is a `user` event with `steer: true`. `tool_call` args and
`tool_result` results are clipped to 2,000 characters; `approval_pending` carries the in-flight
messages whole (a resume needs them) - for an `edit_memory` proposal that's an empty list (the
box, not a gated turn). A `signal` event's `trust` is the sense's own declaration (`ledger.TRUST`),
never the data's own claim; `decision` carries `signal_key, action, why` and, only for a `speak`
that made it into a notice, `notice_id, text, signal_keys` too (what `Outbox` rebuilds from after
a restart). `memory_pass {since, upto, read, steps, usage, applied, proposed}` is recorded once per
consolidation pass (`upto` becomes `state.memory_cursor`; D39: a pass the model never answered records `upto = since`); `memory_apply {target,
op, old, new, evidence, approval?}` is the whole undo record for one applied vault edit;
`session_summary {upto, text}` folds a session's early messages. Origin gains `memory` (the pass's
own turns and tool events).

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
`today(now, idle_threshold_s=)` returns the counters `rules.decide` needs: `spoken`
and `judgments` (`clock.day_start`-scoped), `last_signal_ts`, `burst` (signal turns in the last
60 s), `recent_counts` (signals of a kind in the last 600 s, from `signals` not `decisions` - a
signal is recorded before its own decision, so "including this one" falls out for free; `>= N`
means the Nth occurrence itself speaks) and `away` (from `presence`, only when the daemon's
signal-turn triage passes `idle_threshold_s` - a rule predicate never sees `rules` or
`state` itself). `undelivered()` finds a `decision speak` with no matching
`delivery` — what `deliver.Outbox.rebuild()` restores after a crash. `rebuild(ledger)` drops and
replays these tables too.

Partition 5 adds the page index: `pages(path, title, mtime, text)` with an external-content FTS5
table `pages_fts`, and `vectors(path, model, vec)` (one float32 blob per page, `struct.pack`, no
numpy - a vector tagged with a stale model is never a hit); `index_page`/`remove_page`/`pages()`
keep them in step with `wiki/*.md`. `search(query, query_vector=None, model, top_k=3)` merges
FTS5 prefix-token matches and vector cosine by reciprocal rank fusion (k=60); no `query_vector`
runs FTS alone (D10's fallback is the same function with an argument missing). `summaries(session,
upto, text)` backs `history(session)`: with a summary, a `system` line built from it is followed by
only the messages after `upto` - a session's early turns are folded, not resent
(`test_history_folds_after_summary`). `meta.memory_cursor` (the last pass's `upto`, 0 at first) and
`memory_stats(now)` (passes/lines-read/applied per local day for the last three days, pending
`edit_memory` proposals, judgments per day for the last seven) back `pathfinder memory` and the
stalled-memory trigger. `rebuild(ledger)` drops and replays these tables too.

## 4. The model layer (`pathfinder/llm/`)

- `resolve(cfg, "provider:model")` splits on the first colon and returns an `Endpoint`. Model
  names exist only in `config.json`; an unset model is a clear error. `Endpoint.api` is
  `providers.<name>.api`, `"openai"` when absent (every existing config resolves as before) or
  `"anthropic"`; anything else is a config error. `llm.stream(endpoint, ...)` picks the adapter by
  it - the slot and `rules.judge` call that, never an adapter module (D2).
- `openai_compat.stream(endpoint, messages, tools, cancel)` speaks the OpenAI-compatible
  chat-completions SSE protocol and yields `text | reasoning | tool_calls | usage | cancelled`.
  `cancel` is anything with `is_set()`, checked between chunks. Every failure is an `LLMError`;
  `blocked` is true for 402/429. The daemon's step factory always passes the tool schemas: the
  model must see its hands (the first live run found it did not - `test_default_step_factory_sends_tool_schemas`).
- `anthropic.stream` (D37, **experimental - never run live, D7**) speaks the Messages API SSE
  (`/v1/messages`, `anthropic-version: 2023-06-01`, `x-api-key`) and yields the same tuples: the
  loop's OpenAI-shaped history is converted at the edge (system messages -> the top-level
  `system`, tool calls -> `tool_use` blocks, tool results -> `tool_result` blocks in one user turn),
  `input_json_delta` pieces are joined and parsed at `content_block_stop`, a mid-stream `error`
  event is an `LLMError`. Tests run recorded fixtures only (`tests/fixtures/anthropic_*.sse`).
  No embeddings: `embed` stays with the compatible adapter.
- `openai_compat.embed(endpoint, texts) -> list[list[float]]` embeds a batch through the same
  `/embeddings` OpenAI-compatible route. The embedder is `cfg["embed_model"]`, resolved through
  `resolve` the same way `model`/`worker_model` are; empty means no vectors, never an error.

## 5. Tools, policy, sandbox, broker

**`tools.py`** — `ToolSpec(name, description, params, required, handler, write, network,
sandboxed, reversible, origins, normalize)`; `schema()` is the OpenAI tool dict; `coerce(spec,
args)` casts each declared argument to its JSON-schema type (the string `"false"` must not sail
through as a truthy escalation), drops undeclared keys, reports a missing required one. `normalize`
runs after coerce and before policy, so **the gate shows what will actually happen**: for
`read_file` and `write_file` a relative path becomes `<workspace>/path` and `~` expands; a param
declaring `"format": "local_datetime"` is canonicalized by `normalize_local_datetime(*keys)`, which
parses with `clock.parse_local` and lets its `ValueError` (a `Z` or an offset) become a tool error
the model can act on. Core tools: `run_command(command, escalate)` (write, sandboxed),
`read_file(path)`, `write_file(path, content)` (write) — both file tools refuse
`exec.SECRET_PATHS`. A sense may add more (schedule's `add_schedule`/`list_schedules`/
`remove_schedule` — see §8), merged into this same registry by `signals.load`; `jobs.py` adds
`start_job` the same way, at import time (see §9). `origins` (default: all four - `ledger.ORIGINS`)
names which turn origins may even see a tool's schema; `for_origin(origin)` filters `TOOLS` by it -
`start_job` declares `origins={"user"}`, so a job's own registry and a signal turn's never contain
it: "a worker cannot make a worker" is structure, not a policy check. `origins` is opt-in for
`memory`: the four turn origins are the default, so the consolidation pass sees a tool only when
it explicitly adds `memory` (`search_memory`, `read_memory`, `read_ledger`, `edit_memory`) - it
never sees `write_file`, `run_command` or a schedule tool. `clip_for_ledger(value,
limit=2000)` is the head-only bound a recorded `tool_call`/`tool_result` (a job's own copies too)
keeps - shorter and simpler than `clip()`'s head+tail bound on what the model itself is shown.

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
| `memory` | allow | deny | deny | deny | allow |

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

`run(step, messages, *, call_tool, emit, budget, control, resume, final_step)` drives one turn to
`Outcome("answer" | "exhausted" | "cancelled")`; a gate is an exception, not an outcome. When the
budget is spent and the last reply was still a tool call, `final_step` (the same factory with no
tools - every call site passes one) makes one more request, so an exhausted turn still ends in
words and leaves no dangling `user` message (D30).
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

- Protocol, one JSON object per line. In: `attach {session?, cwd?}` (a string `cwd` pushes the
  `attach` sense's `context` signal, when that sense is on), `say {text}`, `interrupt`,
  `approve {id}`, `deny {id}` (a job's own approval routes to `jobs.Manager.resume` instead of a
  session lane - see §9), `signal {sense, fields}` (a push door — the sense must be loaded and
  push-capable, i.e. export `build(fields)`; `jobs` never is), `presence {idle_s}`,
  `react {vote, id?}`, `job.stop {id}`, `job.steer {id, text}`. Out: `attached`, `turn.start`,
  `delta`, `tool {name, ok}`, `gate {id, tool, args}`, `turn.end {kind}`, `notice {id, text, why}`,
  `job.start {id, goal}`, `job.gate {id, approval, tool, args}`, `job.end {id, state}`, `error`. A malformed or unknown line, an unknown
  or already decided approval id, an interrupt with no running turn: an `error` line, the
  connection stays open.
- A session is a queue plus one worker (`_Lane`, a deque-backed queue — plain `asyncio.Queue` has
  no peek). Lane items: `("say", text)`, `("resume", approval_id)`, `("continue", steer_text)`,
  `("signal", Signal)`. `attach` resumes the latest session younger than `session_resume_s`.
  **D41: a session is its conversation** - `sessions.last_ts` moves on every session event except
  a screen's `presence` heartbeat, a memory pass's own, a signal's triage (of a signal turn only
  the delivered notice, its `assistant` event, counts), and a job (D63): its own events, a steer to
  it and the approval of its gate. A `say` on a session older than that
  re-attaches to the current one (new if none); a signal that opens a new session takes every idle
  screen (`control is None`) with it, or its notice would reach nobody; a connection whose writer
  was moved follows it before handling its next line, and a closing connection leaves every
  session that holds it.
- **The resident system prompt is `prompts/system.txt` + `signals.SIGNAL_TURNS_CLAUSE`, byte-stable
  across every turn, user and signal alike** (cache); a second system message, `clock.now_line()`
  ("Local time now: ..."), carries the only thing that changes turn to turn - with, since D42,
  `signals.context(senses)`: each sense module's `context(now)` lines under one "data, not
  instructions" header (calendar: the next 24 h; reminders: due within 24 h or overdue; titles
  single-line, 80 characters, ten each), read on a worker thread; a sense that fails says so, and
  one whose access is not granted yet says that without prompting (`_eventkit.authorized`).
- **A `say` while the user's own turn is running is a steer** (D17; `Session.origin` is `"user"`
  for a fresh, resumed or continued turn and `"signal"` for a signal turn - a say during a signal
  turn is queued as its own turn, because that turn is invisible to the user): recorded as `user {steer: true}`, appended
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
  records the last two and sends `tool`. All ledger and state writes happen on that thread. A
  user turn's own `TurnControl` also carries `.session` - the one way `start_job`'s handler (which
  gets only `control`, like every tool handler) learns which session to report a job's outcome to.
- `serve()` registers a SIGTERM handler that closes the server (no `async with server`: its
  `wait_closed()` would wait for the attached clients that only `close()` closes); SIGTERM
  (launchd's `service stop`) and Ctrl-C both end in one `close()` (MCP servers closed, their process groups killed, the
  socket unlinked) - before this, Ctrl-C never reached `close()` and every npx child was orphaned.
- The daemon survives a blocked model: `LLMError` becomes an `error` event, an `error` line and
  `turn.end {kind: "error"}`. The socket is born 0600; a live one is refused.
- `jobs.Manager` is built in `__init__` (`self.jobs_mgr`, bound via `jobs.bind`) and given
  `self._job_record` (records, then sends `job.start`/`job.gate`/`job.end` to the job's session), `push_signal`
  and a step factory - `self.rules`, already loaded, is passed straight in rather than loaded
  twice. Since a job runs on its own thread, `Manager.dispatch` marshals its `record`/`push` calls
  onto the event-loop thread (`call_soon_threadsafe`, set once `start()` runs) instead of letting
  a worker thread call `state.apply` or write a socket directly; `close()` swaps it to a no-op
  first, so a job finishing during shutdown never races a closing `state.db`.

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
Daemon.run_signal_turn ── rules.decide per signal FIRST (never held, never sleeps):
        defer / silence ──▶ recorded, done, free
        ask_model ──▶ judgment_cap? defer : rules.judge (one small model call) ──▶ "judgment", then decided
        speak, but today.spoken + bundle-so-far >= daily_budget ──▶ defer instead (trust "user" exempt)
        speak (what's left) ──▶ signals.hold_for: 0 ──▶ Daemon._signal_model_turn
                                                  >0 ──▶ loop.call_later(wait, requeue "signal_ready")
                                                          (the lane is free the instant this returns -
                                                          a `say` right behind a held signal never waits)
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
  gives it no limit, everywhere else zero). `now_line()`, `day_start(ts)`,
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
  plus whatever the loaded senses register (`all_day`, `sender_in`/`sender_domain_in`/
  `subject_has`, `away`); a section whose kind belongs to no loaded sense is parsed but not
  checked. `decide(signal, rules, user_lists, today, now)`: quiet hours -> defer, daily budget
  spent -> defer (a `trust: user` signal, e.g. a schedule or a claude_code kind's own instance,
  is exempt from **both** — an alarm the user set is theirs to hear at 3 a.m. too); then the
  table; no match -> `ask_model`. `judge(signal, cfg, today)` (D13) is one request on
  `worker_model` if set else `model`, no tools, no history, messages = a short fixed system
  ("answer only JSON: `{\"speak\": ..., \"reason\": ...}`") plus that one signal's envelope;
  strict `json.loads`, anything else (or a blocked/unset model) is `(False, why)` - the only
  place this module reaches for an llm module, and only inside this function
  (`test_rules_imports_no_llm_at_top_level`).
- **`deliver.py`** — `Notice(id, session, text, why, signal_keys)`; `Outbox.hold(session)` is true
  while `is_running(session)` or a fresh (< 60 s) `presence` row says `idle_s < idle_threshold_s`;
  `release()` sends every releasable notice in FIFO order, stopping at the first still held.
  `_deliver` records `delivery {id, via}` and `assistant {origin: signal, final: true}`, and sends
  a socket `notice` if any client is attached, else calls `banner` (a macOS `osascript`
  notification by default — injectable for tests). `rebuild()` restores the queue from
  `state.undelivered()`; nothing is auto-flushed until the next natural `release()` trigger, so a
  restart never pops a banner out of nowhere before anything else has happened.
- **`senses/`** — one file each, capped at 150 lines (a leading-underscore file is a shared
  adapter, outside that budget). `schedule.py`: reads `cfg["schedules"]` (poll, 30 s), fires a
  due row once, advances `at` through `config.set` for a repeat, drops a one-shot row; ships
  `add_schedule`/`list_schedules`/`remove_schedule` (the first and third `write=True,
  reversible=True`). `shell.py`: push-only (`build(fields, now)`), `command_failed` (exit != 0) /
  `command_finished` (exit == 0), registers `exit_nonzero` and `duration_over`; its dedupe key
  includes the arrival time, since a repeat is exactly what `count_over` needs to see.
  `calendar.py`/`reminders.py` (the calendar's `poll` signals only events not yet started - an
  all-day event re-signalled every poll, 92 times in a day): two sense modules over one adapter, `_eventkit.py` (200 lines,
  PyObjC's `EventKit`/`Foundation` imported lazily inside each function - trimmed from Mark.5's
  `eventkit_bridge.py` to list/add, plus complete/delete for a reminder (D38: both `write=True`,
  so they ask; an id that is not a reminder raises `LookupError`), timestamps as plain epoch floats so no second date
  format has to stay in sync with `clock.py`'s; a tool result leaves through `for_model()` as
  local `YYYY-MM-DD HH:MM`, because the model misread epochs); split in two because their `TRUST` differs (an
  event may be someone else's invitation, `third_party`; a reminder is the user's own note,
  `user`) - one `TRUST` string per sense module is why they can't share a file. Off without
  PyObjC: `poll` raises once (`SenseLoop.on_error` logs it as an `error` event) and quietly
  returns `[]` on every later poll; the tools raise the same message, caught by the broker like
  any handler exception. `mail.py`: IMAP headers only (`imaplib`, `BODY.PEEK[HEADER...]`, a
  read-only `SELECT` - `poll` never fetches a body), the account password from the macOS
  Keychain (`security find-generic-password`) - never config or the environment; an in-memory
  UID watermark avoids re-fetching mail already turned into a Signal this process, while the
  real 24 h dedupe is `state.seen`. `attach.py`: fed by the daemon's own `attach {cwd}` message,
  not a hook - always silence, exists so rules and the future catalog know what the user is
  working on. `claude_code.py`: pushed by Claude Code's `Stop`/`Notification` hooks, registers
  `away` (read `state.Today.away`, computed by the daemon from `presence` before `decide` runs -
  a condition predicate only ever sees `(signal, value, today, now)`, never `rules` or `state`
  itself, so the `idle_threshold_s` comparison has to happen in `state.today()`, not in the
  predicate). `docs/senses.md` has the zsh hook (backgrounded, errors swallowed - a hook must
  never block the shell) and the Claude Code `settings.json` hooks block.
- **`config.py`** additions: `senses` (`{name: options}` — present key = on; `_check` imports the
  named sense module and validates its options against `OPTIONS`) and `schedules` (a list of
  `{id, at, repeat, text}`; `at` must already be `clock.parse_local`'s canonical string).
- **Second and third external dependencies.** `calendar`/`reminders` need
  `pyobjc-framework-EventKit`; `mail` needs nothing extra (`imaplib` is stdlib) but does need a
  Keychain entry (`docs/senses.md`) and network access to an IMAP server. Both optional -
  everything else runs without them.

## 9. Jobs (`pathfinder/jobs.py`, `pathfinder/llm/slot.py`)

- **`start_job(goal)`** is a `ToolSpec` with `origins={"user"}` (§5), registered into `tools.TOOLS`
  the moment `jobs.py` is imported. Its handler reads `control.session` (stamped by the daemon,
  see §7) and calls `jobs.Manager.start(goal, session)`, bound once via `jobs.bind(manager)` - the
  same pattern `signals.load` uses to bind `HOME` onto a sense module.
- **`Manager(home, record, push, step_factory, rules)`** owns every job: a `dict[id, Job]` (`Job`:
  `id, goal, session, control, messages, started_ts, state, steps, usage`; `state` is
  `running|gated|done|failed|cancelled`), its own read-only `State(home/"state.db")` (for
  `today().jobs` and the restart sweep - `record()` still writes through the daemon's own State),
  and the daemon's already-loaded `Rules` (`jobs_per_day`, `job_steps` - RULES.md frontmatter).
- **`start(goal, session)`** refuses with a plain string (not an error) when a job is already
  `running`/`gated` (one at a time - the free plan has one slot, D25 already serializes calls) or
  `today.jobs >= jobs_per_day`; otherwise it records `job_start {id, goal}`, builds messages =
  `prompts/job.txt` + `clock.now_line()` + `Goal: ...`, and runs `_drive` in a new thread.
- **`_drive`** builds `Broker(tools.for_origin("job"), "job", ...)` and a step from
  `step_factory(cfg, control, job_tools)` (production: `default_job_step_factory` - `worker_model`
  or `model`, `llm.slot.stream_step(..., "job")`), then `loop.run(..., budget=rules.job_steps)`.
  Outcome `answer`/`exhausted` → `job_done {id, summary (clipped 2000), steps, usage, exhausted?}`;
  `cancelled` → `job_failed {reason: "stopped"}`; `LLMError` → `job_failed {reason: "model: ..."}`;
  any other exception → `job_failed {reason: "internal: <type>"}`. Both terminal events push a
  `Signal(sense="jobs", kind="job_done"|"job_failed", trust="self", exempt=True, ...)` (D26: the
  summary is the model's text, so no envelope authority; the user's own job still skips budget and
  quiet hours through `exempt`) - `job.state` is set *after* the ledger write and the push, so
  anything polling it never observes a state change before the fact it describes is on disk.
- **Gate (D24)**: a `Gate` out of `loop.run` records `approval_pending {..., job: id}` and sets the
  job `gated`; the thread ends, the lane stays free. `approve`/`deny` on a row with `job` set skips
  the session lane entirely and calls `Manager.resume(approval_id, decision)`, which re-enters
  `loop.run(resume=True)` in a new thread with `approved`/`denied` built from the stored row - a
  resumed job can gate again.
- **Restart sweep** (`Manager.sweep()`, called from `Daemon.start()` once the loop runs - a push
  from `__init__` has no lane to land on): every `job_start` with no end event is either reconstructed
  as `gated` (its id is on a pending approval row - the messages come back from that row) or closed
  as `job_failed {reason: "daemon restarted"}` - a crash never silently loses a waiting gate.
- **`stop(id)`** sets `control.interrupt` (kills a running shell the same way an interrupted turn
  does); a job waiting at a gate has no thread to notice, so `stop` closes its pending approval
  as denied and ends the job as `cancelled` itself. **`steer(id, text)`** records `user {steer:
  true, job: id, origin: "user"}` (the user said it, not the job - and `state` keeps it out of the
  session's chat history) and queues it exactly like `daemon.steer`.
- **`llm/slot.py`**: `slot(priority)` is a context manager around one streaming request. A `user`
  caller waits only for the current holder; a `job` caller also waits while any user is waiting or
  holding, so a user's turn never queues behind a job that asked first (m7a, HISTORY: the free plan
  queues a second overlapping request server-side - the slot is an ordering device, not error
  insurance). `stream_step(endpoint, schemas, control, priority)` wraps `openai_compat.stream` in
  it; `daemon.default_step_factory` uses priority `"user"` for both user and signal turns,
  `jobs.default_job_step_factory` uses `"job"`.
- **CLI**: `pathfinder jobs` (id, state, started, goal - from `state.jobs()`), `pathfinder job stop
  ID`, `pathfinder job steer ID TEXT`; `pathfinder approvals` prints `job: <id>` when set;
  `pathfinder listen` prints `job.start`/`job.gate`/`job.end` lines too.
- **`state.py` additions**: table `jobs(id, session, goal, started_ts, ended_ts, state, summary,
  reason, steps, usage_json)`, projected only from the three job events (`gated` is never written
  here - it is `Manager`'s own in-memory state, found instead via a pending approval's `job`
  column); `approvals` gains a `job` column; `jobs()`, `job(id)`, `running_jobs()`. `Today` gains
  `jobs` (count of `job_start` today, `clock.day_start`-scoped).
- **`senses/jobs.py`** declares `TRUST = "self"` and `KINDS = {job_done, job_failed}` (both
  `always -> speak` in the shipped RULES.md - D8 §8: the outcome of what the user started spends no
  budget and honours no quiet hours, carried by `Signal.exempt`) purely as documentation for the shape `Manager`'s own `Signal`
  objects already match; it exports no `build`, so it is never `push_capable` and `signal jobs ...`
  from the socket is refused like any unknown sense.

## 10. Memory (`pathfinder/memory.py`)

The vault is three kinds of file under `<home>` - `USER.md`, `RULES.md` (both already there since
partition 3) and now `wiki/*.md` - no `vault/` directory: "inside the vault" is a path check
(`vault_path`) against these three, refusing `..`, an absolute path, or anything else.

- **Tools**, registered at import like `jobs.py`'s: `search_memory(query)` (hybrid `state.search`,
  FTS-only when `embed_model` is unset or embedding fails), `read_memory(path)`, `read_ledger(after,
  limit=80)` (the numbered slice a pass pages through, `render_line` per readable event, tool
  chatter skipped) and `edit_memory(target, op, old, new, evidence)` (`reversible=True`, D22).
  `search_memory`/`read_memory` are open to all five origins; `read_ledger` is `memory`-only;
  `edit_memory` is `{user, memory}` - a user turn's own call runs by itself (policy:
  user/reversible = allow), the pass's too, nothing else may even see it.
- **The gate (`Manager.vet`, no model)**: target inside the vault; `op` one of `add|replace|remove`
  with `new`/`old` non-empty as each needs; `old` must match exactly one line of the file as it
  stands; the file after the edit still parses (`RULES.md` through `rules.parse` with the loaded
  senses' vocabulary; `USER.md`'s frontmatter must keep its three lists); `evidence` is seqs the
  caller actually read (a pass's own `allowed` set from `read_ledger`, or - for a user turn, which
  has none to cite - the session's latest `user` message, filled in automatically); a `USER.md`
  edit from the pass needs evidence whose origin is `user` (`test_user_facts_need_user_origin`). A
  failed check returns the reason as the tool's own result; nothing is written.
- **Routing and apply**: a `wiki/*.md` edit applies at once (atomic write, then `state.index_page`
  re-embeds it); `USER.md`/`RULES.md` go to the approval box (`approval_pending {tool:
  "edit_memory", args: proposal, messages: []}`, the same table a tool gate uses) - unless the call
  is a **user turn's own** and `grants["edit_memory"]` is `"allow"` (D28; the pass's own proposals
  always go to the box). `Manager.apply` writes the file, records `memory_apply {target, op, old,
  new, evidence, approval?}` (the whole undo record), and for `RULES.md` calls `on_rules_changed()`
  (`reload_rules`, below). `Manager.decide(approval_id, decision)` is the daemon's third
  `approve`/`deny` route: approved re-applies against the file as it now stands; denied writes
  nothing. `undo(n)` (CLI only) replays the last `n` `memory_apply` events as inverse ops (`add` ->
  remove `new`, `replace` -> replace `new` with `old`, `remove` -> add `old`), each itself recorded
  as a fresh `memory_apply` - no separate vault history, the ledger already holds the inverse.
- **Resident context**: `context(home, state)` = `USER.md`'s body plus the wiki map (`- title
  (date)`, newest first, capped at 1,000 characters); the daemon appends it to every turn's
  *second* system message, after `clock.now_line()` - the resident first message stays byte-stable,
  only the second one already changed turn to turn.
- **The pass (`Manager.run_pass`)**: one `loop.run` turn on its own thread, off the user's own turn
  like a job (priority `"job"`). Messages = `prompts/consolidate.txt` (byte-stable) + `now_line()` +
  a goal message naming the ledger range, the closed condition/action/kind vocabulary, and the live
  session's previous summary if any. Tools = `tools.for_origin("memory")` (exactly `search_memory,
  read_memory, read_ledger, edit_memory`); budget `consolidate_steps`. A final answer becomes
  `session_summary {session, upto, text}`, `upto` = the seq of the session's fifth-newest message
  (the last four stay verbatim). Then `memory_pass {since, upto, read, steps, usage, applied,
  proposed}` - an internal error still advances the cursor, so a slice that breaks the step
  never loops; **a pass that did nothing and ended on an outage keeps it** (`LLMError` status 0,
  401, 402, 408, 429 or 5xx; `upto = since`, D39 - offline, the owner's own words were skipped)
  and the trigger waits 30 min before trying again, while any other 4xx advances; `proposed > 0` pushes `memory_proposed`. The trigger
  (`Manager.tick`, a 60 s thread, `Daemon.start`/`close`) starts a pass when no turn is running and
  no job holds the slot (`is_busy`), the last user message is `consolidate_idle_min` minutes old,
  and `consolidate_min_events` readable lines sit unread; `pathfinder memory run` forces one over
  the socket (`memory.run`), returning `run_pass`'s own result line.
- **`memory_proposed`/`memory_stalled`** are pushed directly by the `Manager` (`senses/memory.py`
  documents their shape only, exports no `build`, so `signal memory ...` is refused like `jobs`).
  `tick` also raises `memory_stalled` when three local days in a row each read something and
  applied nothing (blueprint §9: Mark.5's 24 silent days become 3), deduped to once a day
  (`_stalled_key`) - a naive push every tick would write a `signal` line every 60 s while stalled.
- **`reload_rules`** (`Daemon`) re-parses `RULES.md` and rebinds `self.rules` on the outbox, the
  jobs manager and the memory manager; a `ValueError` keeps the old rules and records an `error`
  event. Called after an approved `RULES.md` edit and, once per signal-turn triage (one `stat`),
  whenever a hand edit moved the file's mtime - closing HISTORY's "read once at daemon start".
- **CLI**: `pathfinder memory` (cursor; passes/read/applied for today and the two days before;
  pending proposals; judgments per day for seven days; the latest session summary's `upto`);
  `pathfinder memory run`; `pathfinder memory undo [N]` (refuses while a daemon is serving, like
  `reindex`; standalone `Ledger`/`State`/`Manager`). `pathfinder approvals` prints an `edit_memory`
  row as `target op: old -> new` (each side clipped to 60 characters); `reindex` also calls
  `memory.reindex_pages` and reports the page count.

## 11. The tool path (partition 6): MCP client, catalog, installer

Other people's tools, bought one approval at a time. The path is registry -> candidate -> tags ->
license -> `tool_candidate` notice -> `install it` -> check (runtime, pinned version + integrity,
license, secrets, a sandboxed trial launch that lists and hashes the tools) -> one `install_server`
proposal row -> `approve` -> config + Claude Code + pins + grants + load -> `install_done` notice ->
the next turn calls `mcp__<server>__<tool>`. Every hop is an event; `state.db` deleted and
`reindex` restores pins, installs and the rows still waiting.

- **`pathfinder/mcp/` (Mark.5's `brain/mcp/`, transplanted; the adapter budget, not the core)**:
  `protocol.py` (JSON-RPC envelopes for both spec eras, the `mcp__<server>__<tool>` naming rule -
  `__` because tool names may hold dots and server ids may not hold underscores - `sanitize`),
  `transport.py` (stdio: argv, the caller's env, a stdout pump thread, a stderr drain, id-matched
  replies; Streamable HTTP: one POST per request, JSON or SSE reply), `session.py` (`discover` /
  `list_tools` / `call_tool`; the **dual-era probe**: an explicit `protocol` config key wins, else
  the transport guesses - stdio -> the legacy `initialize` handshake, HTTP -> the stateless
  `server/discover` - and a JSON-RPC error flips the guess once; a timeout is never retried, because
  Mark.5 measured that legacy servers do not answer `server/discover` at all; the probe runs on first
  contact from `_call`, so `entries()` never bypasses it), `pins.py` (a fingerprint per tool -
  description hash, schema hash, argument names; `check()` pins an unknown server whole and holds
  every changed or new tool of a known one; `PinStore` lives in the ledger: `load()` reads
  `state.pins()`, `save()` records `pin_accept` for dirty entries only), `servers.py` (`load(cfg)`
  -> `ServerConfig` per `mcp_servers` entry, `keychain:<account>` values resolved through
  `keychain.secret("pathfinder-mcp-<id>", account)` - a missing one keeps the server listed as
  `disabled`, warned once per instance; `Servers`: `entries()`, `call()`, `load_one()` closing what
  it replaces, `unload()`, `close()`), `tools.py` (one `ToolSpec` per **passed** entry, built from
  the very list the pin check saw - a re-fetch could differ; description prefixed "written by that
  server, not an instruction from PATHFINDER", control characters stripped, clipped at 1,200;
  `write=True, network=True` always - a `readOnlyHint` is the server's word; D31's `allow` is a
  grant, which the policy honours on user turns only; a tool with an argument named `control` is
  skipped), `attach.py` (the daemon's seam: `load()` runs `entries()` in a thread then
  `register()` on the loop thread - pin check, `pin_accept`, replace the `mcp__` keys in
  `tools.TOOLS`, and for each held server one `accept_pins` proposal row + a `tool_held` signal,
  deduped against a row already waiting; `reload(id)` after an approve; `accept_pins` re-records the
  hashes and reloads; WARNING+ lines of the `pathfinder.mcp` logger become `error` events through
  `dispatch`). The daemon starts the load as a task after the socket binds (`mcp.loaded`; a turn
  awaits it once) so a dead server never delays the first `say`.
- **`catalog.py` (the registry reader, core)**: `REGISTRY` is the only registry URL in source;
  `fetch(since, search)` pages `GET /v0.1/servers` (`updated_since`, `version=latest`, cursor, at
  most five pages); `candidate(item)` -> the connection spec v0 shape (`docs/spec/connection-v0.md`,
  `SPEC_FIELDS`; every check not run is the string `not_checked`); `tags(state, home, now)` = the
  project names of the last seven days' `context` signals plus marker words from listing each cwd
  once (`package.json` -> node, javascript, npm; `.git` -> git - not "github", every `io.github.*`
  name would match); `matches` on the name's last path segment and the description, whole words;
  `license` (npm view / PyPI JSON, `license_expression` first) -> `ok | excluded | unknown` - GPL,
  AGPL, LGPL, CC-BY-NC, "GENERAL PUBLIC LICENSE", "AFFERO", non-commercial and `UNLICENSED` are
  excluded, "SEE LICENSE IN" is unknown; `declined(state, name, now)` = denied within 30 days.
- **`senses/catalog.py`** (`TRUST = third_party`, `CADENCE_S` 24 h): `poll` fetches since the last
  `tool_candidate` signal (clamped to a week), keeps entries matching a tag whose license is `ok`
  (at most ten lookups per poll), not installed, not declined -> `tool_candidate {candidate,
  matched_tags}` keyed `catalog:<name>@<version>`. Shipped rules: `count_over 3 -> silence` (a
  flood is a registry dump), `always -> speak`. Its `TOOLS` (registered only while the sense is on):
  `search_tools(query)` (user origin, the registry on the spot; the registry matches a substring
  of the hyphenated name, so spaces become `-`, ten results with a 60 s timeout - a rare name scans
  the whole table - and no match returns a sentence, not `[]`) and `propose_install(name)` (user
  origin only - a signal or job turn cannot even see it; runs the check on the turn's own thread and
  lands the proposal). `tool_held`, `install_done`, `install_failed` are pushed by `attach`/the
  installer (`trust self`; the install outcomes `exempt` like a job's - the user asked).
- **`installer.py` (core, ~480 lines)**: `check(candidate, explicit=)` fills `verification` in
  order and stops at the first hard failure - runtime (`shutil.which`, the fix line in the reason),
  pinned version + integrity (one `npm view ... dist.integrity license --json` / PyPI digests; never
  `latest`), license (the value from that same lookup; `excluded` fails a catalog proposal and is a
  shown warning on an explicit request, D32), secrets (every `secret` env must already be in the
  Keychain, else the row says `needs: NAME` with the `security add-generic-password` line; the trial
  runs without it), the **trial launch** (the stdio command under `exec.py`'s Seatbelt profile with
  writes confined to a fresh temp dir - `npm_config_cache`/`UV_CACHE_DIR` point inside it, which is
  what makes a fresh-HOME install work - `discover` + `tools/list` with a 60 s timeout, each tool's
  hashes and `readOnlyHint`, the process closed, the dir removed), then the `install` block
  (`permissions`: `allow` for read-only-hinted tools, `ask` for the rest). `propose` records the one
  `approval_pending {tool: "install_server", args: candidate, messages: []}` (D3); `pathfinder
  approvals` renders it as `license MIT ok · integrity ok · trial ok, 6 tools (3 read-only) · needs:
  TOKEN`. `decide` is the `PROPOSALS["install_server"]` handler: approved -> `install` on a thread,
  one `install_step {id, step, ok, detail}` per step - `mcp_servers` through `config.set`, the
  Claude Code merge (backup, `mcpServers[<id>] = {command, args, env: {NAME: "${NAME}"}}`, atomic
  write, read-back deep-compare of every other key), `pin_accept` per trial tool, `grants` for the
  read-only ones (D31), then `attach.reload(id)` on the loop - and `install_done` + its signal; any
  failing step runs `rollback` in reverse (unload, `pin_revoke`, restore `~/.claude.json` from the
  backup byte-for-byte, restore `mcp_servers` and `grants`) and records `install_failed {id, step,
  reason}` + its signal. `uninstall(id)` is the reverse path from a completed install (`uninstalled`) -
  except for `~/.claude.json`, which it edits in place (pop `mcpServers[<id>]`, atomic write,
  read-back compare): the install-time backup is for `rollback` only, seconds after it was taken;
  restoring it later would wipe another server's entry and Claude Code's own writes (review). The server id is decided once, at check (the registry name's last path segment sanitized to
  the config's id rule); an id already taken fails the install. The check also refuses a runtime
  other than `npx`/`uvx`, an argv that does not carry exactly the pinned `<identifier>@<version>`
  (the registry's `runtimeArguments` are third-party text), and any version that is not exact. Denied rows are what `catalog.declined` reads.
- **The proposal box** (`daemon.py`): a row with empty `messages` is a proposal; `PROPOSALS =
  {"edit_memory", "accept_pins", "install_server"}` names its handler by tool (sync or awaited);
  a row with messages is a lane gate and resumes its turn; a row with `job` resumes its job. A
  proposal from a user's own turn (origin `user`) sends that session the same `gate` line a lane
  gate does (`install_server` as `installer.brief`, one line, control characters dropped since
  registry text reaches a terminal) - `Daemon.record` is the one door;
  a pass's or an attach's proposal stays behind its own notice.
- **`state.py`**: `pins(server, tool, hashes, params)` from `pin_accept`/`pin_revoke`;
  `installs(id, name, version, state, ts, detail)` from the four install events (`detail` holds
  the backup path); `installed_names()`, `install(id)`, `context_cwds(since)`.
- **CLI**: `pathfinder install NAME` (over the socket; the check runs on a daemon thread; prints
  the row), `uninstall ID`, `search TEXT` (no daemon), `tools` (servers, tools and grants from
  config + pins, held tools marked), `approvals` (each proposal kind its own line).
- **Not in v1** (D14 and the contract): sending outcomes to a teammate's server (the spec names
  `outcome`; nothing is sent), Cursor/Claude Desktop targets, OAuth flows, mcpb bundles, automatic
  updates (a newer version is a new candidate), malicious-code scanning (`malicious_scan:
  not_checked`), `Mcp-Session-Id` for stateful legacy HTTP servers.

## 12. Invariants and the tests that hold them

| Invariant | Test |
|---|---|
| One ledger writer, one partition function, one config writer, no date math elsewhere | `test_invariants.test_single_writer` |
| Closed vocabulary; 13-day session reads back whole; two processes never share a `seq`; `seq` survives a truncated newest partition | `test_ledger.*` |
| State is rebuildable, apply is idempotent, a turn is counted once by two processes; only what was said is remembered | `test_state.*` |
| Conversation and **pending approvals** survive deleting `state.db` | `test_daemon.test_daemon_resumes_after_state_deleted`, `test_reindex_restores_pending_approval` |
| The daemon survives a blocked or unset model | `test_daemon.test_daemon_survives_model_402`, `test_unset_model_is_an_error_not_a_crash` |
| The real step factory sends the tool schemas and the control | `test_daemon.RealStepFactoryTest` |
| A gate survives a daemon restart; deny resumes with the denial; two gates with the same call id are both resolvable and the gate text reaches history | `test_daemon.test_daemon_gate_survives_restart`, `test_daemon_deny_resumes_with_denial`, `ReviewRegressionsTest` |
| Pins live in the ledger (a pin survives `state.db` deletion; `pin_accept` only for dirty pins); a changed tool is held, never registered, until `approve`; the probe runs on the real path | `test_mcp_pins.test_pins_live_in_the_ledger`, `test_daemon.McpDaemonTest.test_rug_pull_holds`, `test_mcp_client.test_list_tools_runs_the_probe_first` |
| A secret never reaches `config.json`; a missing one disables the server loudly | `test_mcp_servers.test_secret_from_keychain_never_in_config` |
| The install merges `~/.claude.json` non-destructively and rolls back byte-for-byte; grants only for read-only-hinted tools at approval; the catalog never proposes an excluded license; `propose_install` is user-only | `test_installer.*`, `test_daemon.InstallerDaemonTest` |
| One registry URL in source; `catalog.py` ≤ 300, `installer.py` ≤ 700, `senses/catalog.py` ≤ 150, `mcp/attach.py` ≤ 120, `daemon.py` ≤ 780; the spec fixtures carry every v0 field | `test_invariants.*`, `test_spec_fixtures.*` |
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
| `judge` returns strict-JSON-only, `(False, ...)` on garbage/missing fields/a blocked or unset model, and reads `worker_model` over `model` | `test_rules.JudgeTest.*`, `test_daemon.JudgmentCapTest.test_judge_never_runs_over_cap` |
| calendar/reminders are off (not crashed) without PyObjC, log the failure once, then stay quiet; their tools return the same message | `test_calendar.CalendarOffTest.*` |
| mail's `poll` never issues a body fetch (`PEEK[HEADER` only); its password comes only from the Keychain, never a config key | `test_mail.MailHeadersOnlyTest.*` |
| `attach {cwd}` pushes exactly one `context` signal when the sense is on, none otherwise | `test_daemon.AttachSenseTest.*` |
| `claude_code`'s `task_done`/`needs_input` speak only when `today.away` is true | `test_claude_code.ClaudeCodeAwayTest.test_claude_code_speaks_only_when_away` |
| A `local_datetime` gate shows the canonical string, not what the model wrote | `test_calendar.AddEventGateTest.test_add_event_gate_shows_canonical_datetime` |
| A job waiting is not FIFO-first: the user goes ahead of an already-waiting job; the slot is released between steps | `test_slot.SlotTest.*` |
| `start_job` is user-only; a job's and a signal turn's own tool registry never contains it | `test_tools.OriginRegistryTest.test_tools_for_origin` |
| A job runs to a summary with its tool events tagged `origin: job`; refuses over budget and when one is already running; `stop` kills a blocking tool within a second; a steer reaches the next step; a gate pauses and both `approve`/`deny` resume it correctly; a gate survives a restart, an ungated crash is closed as `job_failed`; an `LLMError` fails the job, not the daemon; an exhausted budget is `done` with `exhausted: true` | `test_jobs.*` |
| `state.db` deleted: a job and its gated approval (with the `job` column) come back from the ledger alone | `test_jobs.ReindexRestoresJobsTest.test_reindex_restores_jobs_and_job_approvals` |
| A session's own turn still answers while a job is running; `job_done` is `exempt`, so it speaks even with the daily budget spent and in quiet hours - with trust `self`, never `user` | `test_daemon.JobDaemonIntegrationTest.test_daemon_answers_while_job_runs`, `test_daemon.JobSignalTest.test_job_done_speaks_without_budget` |
| The socket refuses `signal jobs ...`; `jobs.py` <= 350 lines | `test_daemon.PushDoorValidationTest.test_socket_refuses_signal_jobs`, `test_invariants.test_jobs_module_under_350_lines` |
| The vault is exactly `USER.md`, `RULES.md`, `wiki/*.md`; the gate refuses outside it, an unknown RULES.md word, and a non-exact `old` match; a USER.md edit from the pass needs `origin: user` evidence | `test_memory.test_vault_path_refuses_outside`, `test_vet_refuses_unknown_rule_word`, `test_vet_replace_needs_exactly_one_line`, `test_user_facts_need_user_origin` |
| A wiki edit applies at once; USER.md/RULES.md go to the approval box unless a user-turn grant allows it; approving a RULES.md edit calls `reload_rules`; undo is the inverse op | `test_memory.test_wiki_applies_at_once`, `test_user_and_rules_go_to_the_box`, `test_grants_allow_applies_user_turn_at_once`, `test_rules_apply_calls_on_rules_changed`, `test_undo_is_the_inverse_op` |
| A pass reads its own slice, its evidence must come from that slice, and an internal error advances the cursor and a model error (nothing done) keeps it; the trigger waits for idle and enough unread lines; three silent days raise `memory_stalled` once | `test_memory.test_pass_reads_slice_and_advances_cursor`, `test_pass_llm_error_keeps_the_slice`, `test_pass_evidence_must_be_from_its_slice`, `test_due_waits_for_idle_and_enough_lines`, `test_stalled_after_three_silent_days` |
| Hybrid search beats FTS-only by D10's margin on the transplanted corpus, or vectors ship off by default; a stale-model vector is never a hit; a session's history folds after its summary; `state.db` deleted restores pages, summaries and the memory cursor | `bench/m4_retrieval.py`, `test_state.test_search_hybrid_rrf_order`, `test_search_ignores_vectors_of_another_model`, `test_history_folds_after_summary`, `test_reindex_restores_summaries_and_applies` |
| An `edit_memory` approval routes to the memory manager, not a session lane; RULES.md reloads without a restart on an approved edit or a hand edit, keeping the old rules on a parse error; the resident context carries USER.md's body and the wiki map in the turn's second system message only, the first unchanged | `test_daemon.MemoryDaemonTest.*` |
| `memory_proposed`/`memory_stalled` default to silence and the shipped RULES.md speaks both | `test_senses.MemorySenseTest.test_memory_kinds_default_silence_and_shipped_rules_speak` |
| `init` stops at the first failing check and writes nothing before Ollama answers; the model pick goes through `config.set`; senses merge, never replace; the zshrc line and the Claude Code hooks land once (a second run changes no derived file); the plist names the interpreter, a literal `PATH` and the log; `service` never calls the real launchd in tests; no model name in `init.py` | `test_init.*` |
| The TUI sends `say`/`approve`/`react`/`interrupt` over the same protocol, produces `presence` (typing = 0, silence = idle seconds), holds a notice while hands are on the keyboard, prints a stream as one paragraph, decodes UTF-8 keys, restores the terminal on every exit, and sanitizes third-party text | `test_tui.*`, `test_invariants.test_presence_has_a_producer` |
| SIGTERM and Ctrl-C both end in one `close()` (the fake stdio server's process is gone); a `say` during a signal turn gets its own turn, during the user's turn still steers | `test_daemon.SigtermTest`, `test_say_during_signal_turn_starts_its_own_turn`, `test_daemon_say_during_turn_steers` |
| `merge_json_file` keeps every other key, backs up first, creates a missing file, raises when the read-back differs | `test_installer.test_merge_json_file_*` |
| The Anthropic adapter yields the same tuples from recorded SSE (text, tool_use from `input_json_delta`, usage), converts the history at the edge, maps 429/402 to `blocked`; a config without `api` resolves to `openai` exactly as before; no test opens a network socket | `test_llm_anthropic.*` |
| `init.py` <= 300, `tui.py` <= 400; README.md and README.ko.md have the same headings | `test_invariants.test_init_module_under_300_lines`, `test_tui_module_under_400_lines`, `test_readme_pair_has_equal_heading_counts` |
| `memory` is an opt-in tool origin, exactly `search_memory, read_memory, read_ledger, edit_memory`; the socket refuses `signal memory ...`; `memory.py` and `daemon.py` <= 780 lines (the 1.3x line of their 600 allocations) | `test_memory.test_tools_for_origin_memory`, `test_daemon.MemoryDaemonTest.test_socket_refuses_signal_memory`, `test_invariants.test_memory_module_under_780_lines`, `test_invariants.test_daemon_module_under_780_lines` |

## 13. How to count

```bash
python3 -m unittest discover -s tests            # all tests, no network (440)
wc -l pathfinder/*.py pathfinder/llm/*.py pathfinder/senses/*.py   # core; budgets: ledger 400,
                                                  # state 600, daemon 600 (756 today, 780 hard line),
                                                  # loop 400, broker+policy 350, exec 300, tools 300,
                                                  # clock+signals+rules 450, deliver 250, jobs 350,
                                                  # memory 600 (686 today, 780 hard line), a sense file 150,
                                                  # catalog 300, installer 700, init 300 (partition 7);
                                                  # core cap 7,000 (D34) - the 1.3x line per module is the gate
wc -l pathfinder/tui.py pathfinder/llm/anthropic.py  # outside the core: tui <= 400, the experimental adapter in llm/'s 600
wc -l pathfinder/mcp/*.py                         # the transplanted MCP client: adapter budget 1,858, outside the core
python3 bench/m5_install.py                       # m5: installs from the registry in a temp HOME (network, npx; no model credits)
python3 scripts/scan_personal.py --all           # personal-data scan over tracked files
```

## 14. Running it

```bash
pathfinder init                                   # onboarding: checks, model pick, senses, hooks, launchd (partition 7)
pathfinder service start|stop|restart|status      # the launchd agent; stop = bootout (KeepAlive cannot respawn it)
pathfinder tui                                    # one screen: type, read, /approve ID, /deny ID, /approvals, /jobs, + or -, /quit; Ctrl-C interrupts, twice quits
python3 -m pathfinder config set model provider:model-name
python3 -m pathfinder daemon                      # by hand, without launchd
python3 -m pathfinder say "hello"                 # a second `say` while one streams is a steer
python3 -m pathfinder listen                      # attach and print deltas and signal notices
python3 -m pathfinder approvals                   # pending gates, read from state.db
python3 -m pathfinder approve ID | deny ID        # resumes the gated turn and prints its answer
python3 -m pathfinder interrupt                   # stops the running turn of the latest session
python3 -m pathfinder config set grants '{"write_file": "allow"}'
python3 -m pathfinder config set senses '{"schedule": {}, "shell": {}, "calendar": {}, "reminders": {}}'
python3 -m pathfinder signal shell '{"cmd": "make test", "exit": 1, "seconds": 3, "cwd": "/tmp"}'
python3 -m pathfinder config set senses '{"mail": {"host": "imap.example.com", "user": "you@example.com"}}'
security add-generic-password -s pathfinder-mail -a you@example.com -w   # once, before turning mail on
python3 -m pathfinder react up                    # thumbs-up the latest delivery
python3 -m pathfinder rules                       # print the path of RULES.md
python3 -m pathfinder reindex                     # refuses while a daemon is serving
python3 -m pathfinder jobs                        # id, state, started, goal - from state.db
python3 -m pathfinder job stop ID                 # sets control.interrupt on that job
python3 -m pathfinder job steer ID "focus on X"    # reaches the job before its next model call
python3 -m pathfinder config set embed_model provider:embedding-model-name   # empty = no vectors
python3 -m pathfinder memory                      # cursor, passes/read/applied, pending, judgments
python3 -m pathfinder memory run                   # force a consolidation pass now
python3 -m pathfinder memory undo                  # replay the last memory_apply as its inverse
```

## 15. Installing and the screen (partition 7)

- **`pathfinder init`** (`init.py`, <= 300 lines) walks the checks in order and stops at the first
  hard failure, printing the exact fix line: Python >= 3.11; Ollama answering `GET
  <providers.ollama.base_url>/models` (the ids are listed with numbers, the pick goes through
  `config.set("model", "ollama:<id>")`; an embedding model in the list is offered as `embed_model`;
  no model name is in the code or the README - the list comes from the machine); `npx` (a warning
  only); the home directory (0700; `RULES.md`/`USER.md` stay the daemon's first-start job); the
  secret-free senses one yes/no each, merged into `senses` through one `config.set` (mail and
  calendar are announced, not asked - they need a password or a permission prompt and `init` must
  not block on either); the shell hook (the snippet to `<home>/hooks/pathfinder.zsh`, one guarded
  line appended to `~/.zshrc` after a backup, a newline first when the file lacks one; the snippet
  loads `zsh/datetime` and registers through `add-zsh-hook`, beside the user's own hooks); the Claude Code hooks (two entries merged into
  `~/.claude/settings.json` through `config.merge_json_file`, D60); the launchd agent (written
  with `plistlib`, then `launchctl bootstrap gui/<uid> <plist>`, then up to ten seconds waiting for
  the socket). Every step is idempotent: a second run changes no derived file. Answers come from
  `input()` so a test pipes them; `launchctl` is one injectable function.
- **`pathfinder service start|stop|restart|status`**: `bootstrap` / `bootout gui/<uid>/pathfinder.daemon`
  / `kickstart -k` / `print`, reduced to one line plus whether the socket answers. `stop` is
  `bootout`, not a signal: a loaded agent with `KeepAlive` would respawn on a bare SIGTERM.
- **The daemon under launchd** (§7): SIGTERM and Ctrl-C both reach `close()`; the log is
  `<home>/daemon.log`; `WorkingDirectory` is the package's parent so `-m pathfinder` resolves from a
  clone (launchd's cwd is `/`).
- **`pathfinder tui`** (`tui.py`, <= 400 lines, stdlib, outside the core like `mcp/`; D35): one
  attached client for the sitting. Connects, sends `attach {cwd}` (the `attach` sense's context
  source - before this only the one-shot `say` sent it), puts the terminal in cbreak with `ISIG`
  off (so Ctrl-C arrives as a byte and is handled: `interrupt` while a turn runs, quit otherwise),
  multiplexes stdin and the socket with `selectors`. The screen is the terminal's own scrollback:
  a stream prints as one paragraph between `turn.start` and `turn.end`; anything else prints above
  and the input line (`\r\033[K` + prompt + buffer) is redrawn after it; a `notice` that arrives
  while keys are arriving waits until they have been silent for a second. Keys are decoded as
  UTF-8 (a Hangul syllable is three bytes). Enter -> `say`; `+`/`-` alone -> `react`; `/approve ID`,
  `/deny ID`, `/approvals`, `/jobs` (the CLI's own lists from `state.db`), `/quit`; a second Ctrl-C
  while a turn refuses to be cut quits; arrow keys are dropped. During a stream keys buffer
  silently and any other line takes a line of its own, so nothing erases the streamed text. **It is the only real producer
  of `presence`**: `{idle_s: 0}` at most once a second while typing, `{idle_s: n}` every 30 s of
  silence - the deliver state machine's typing and away branches (§8) had no live source from
  partition 3 to 6. `tui.render(msg)` is the one renderer; `listen` prints through it too. Fixed
  strings are bilingual side by side in one table (D6, no setting); notice text and job goals pass
  `mcp.protocol.sanitize`. The terminal is restored in a `finally` on every path.
- **`llm/anthropic.py`** (§4; D2, D37): experimental, recorded tests only, never run live (D7).
  `providers.<name>.api = "anthropic"` selects it; every existing config resolves to `openai`.
- **README.md / README.ko.md** carry the ten-minute path (Homebrew prerequisites, the Ollama
  account, `git clone` + `pipx install .` - Homebrew's Python refuses a plain `pip install`, PEP 668, `pathfinder init`, `pathfinder tui`) and name no model; the
  invariant test keeps their headings in step. The partition's exit measurement is `m8` - a person
  on a fresh macOS account reaching the first conversation from the README alone in ten minutes -
  recorded in HISTORY, gating partition 8.

