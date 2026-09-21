# ARCHITECTURE

**Only what the code does today.** Why it is shaped this way: `docs/blueprint/` and
`docs/decisions/DECISIONS.md`. What measurement changed: `HISTORY.md`. Every name here exists in
the tree. Korean mirror: `ARCHITECTURE.ko.md` (updated in the same commit).

Built so far: **partition 1 — the minimal round trip.** No tools, signals, jobs, memory or
installer yet.

## 0. One sentence

Every fact is appended to one ledger first; everything else is derived from it and can be deleted.

```
client (nc -U, `pathfinder say`)
   │  NDJSON over <home>/pathfinder.sock  (0600, no TCP)
   ▼
daemon.Daemon ── one lane (queue + worker) per session
   │  record(): event = ledger.append(...)  →  state.apply(event)
   ├─▶ ledger.Ledger   <home>/events/<UTC date>.jsonl   append-only, the truth
   ├─▶ state.State     <home>/state.db                  derived, rebuildable
   └─▶ loop.run(step, messages, call_tool, emit)        in a worker thread
            └─ step = llm.openai_compat.stream(endpoint, messages)   SSE
```

## 1. Where things live

| Place | What | Written by | If deleted |
|---|---|---|---|
| `<home>/events/<date>.jsonl` | every event, one JSON object per line | `ledger.Ledger.append()` only | history is lost — this is the only truth |
| `<home>/state.db` | sessions, messages, cursor | `state.State.apply()` | `pathfinder reindex`, or just restart: the daemon catches up |
| `<home>/config.json` | model names, providers, knobs | `config.set()` only | defaults (no model configured) |
| `<home>/pathfinder.sock` | the daemon's socket | daemon | recreated at start |

`<home>` is `$PATHFINDER_HOME` or `~/.pathfinder` (mode 0700).

## 2. The ledger (`pathfinder/ledger.py`)

- `partition(ts)` maps a timestamp to a UTC date. It is the **only** place a time becomes a file
  name. Readers never compute dates; they iterate partitions in order and filter by `seq`.
- An event has the reserved fields `seq, ts, type, session, turn, origin, trust` plus data.
  `seq` is global and strictly increasing. `type`, `origin` and `trust` come from closed sets;
  anything else is rejected at `append()`. Dropping a word from the vocabulary means deleting it
  from the set, after which the ledger refuses it.
- Two processes may append (the daemon, and the CLI running `config set`): each append holds a
  thread lock and an exclusive `flock` on `events/.lock`, and re-derives the last `seq` from the
  file tail when the newest partition changed size behind its back.
- Crash recovery: the last `seq` is found by walking partitions newest-first until one yields a
  good line (the newest file alone can be empty or hold one truncated line after a crash at UTC
  midnight - trusting it would restart `seq` at 1 and the state would drop every new event as
  already applied). A fragment without a newline is closed with one before the next append, so a
  new event is never glued onto it. `read()` skips unparsable lines.
- Known ceiling: `flush()` without `fsync` — an OS crash can lose the last line.

Event types today: `session_start, user, assistant, tool_call, tool_result, turn_start, turn_end,
error, config_set`. `config_set` records the key, never the value.

## 3. Derived state (`pathfinder/state.py`)

`apply(event)` is the single projection, used live and by rebuild alike, so the two cannot drift.
It is idempotent (ignores `seq <= cursor`): the cursor is read inside `BEGIN IMMEDIATE` and
advances in the same transaction, under a lock, so a second process or a worker thread cannot
count a turn twice.

**Only what was said becomes history:** a `user` event counts when `origin == "user"`, an
`assistant` event when `final` is true. Signal envelopes and mid-turn chatter never reach
`history()`. `catch_up(ledger)` runs at daemon start; `rebuild(ledger)` drops and replays.

## 4. The model layer (`pathfinder/llm/`)

- `resolve(cfg, "provider:model")` splits on the first colon and returns an `Endpoint`. Model
  names exist only in `config.json`; the default is empty, and an unset model is a clear error.
- `openai_compat.stream()` speaks the OpenAI-compatible chat-completions SSE protocol and yields
  `("text" | "reasoning" | "tool_calls" | "usage" | "cancelled", payload)`. Tool calls are
  assembled from indexed deltas; arguments may arrive as string pieces or as an object; an empty
  id gets a fallback. Every failure is an `LLMError` - HTTP status, connection errors and the ones
  that arrive mid-stream (read timeout, reset, incomplete read); `blocked` is true for 402/429.

## 5. The loop (`pathfinder/loop.py`)

`run(step, messages, call_tool, emit, budget)` drives one turn and ends as
`Outcome("answer" | "exhausted" | "cancelled")`. It never sees a model name, an origin or a
policy. Reasoning is never echoed into history; the assistant message is rebuilt as
`role / content / tool_calls`. The budget is a cost ceiling. Every outcome carries all text
streamed during the turn, so an exhausted or cancelled turn still reaches history. Exceptions
propagate to the caller.

## 6. The daemon (`pathfinder/daemon.py`)

- Protocol: one JSON object per line. In: `attach {session?}`, `say {text}`. Out: `attached`,
  `turn.start`, `delta`, `tool`, `turn.end {kind}`, `error`. A malformed or unknown line gets an
  `error` and the connection stays open.
- A session is a queue plus one worker: one turn at a time, a second `say` waits. `attach` resumes
  the latest session younger than `session_resume_s`, else starts one.
- A turn: `user` → `turn_start` → system prompt + `state.history()` → `loop.run` in a thread →
  `assistant {final}` → `turn_end {kind, steps, usage}`. The worker thread hands output back
  through one queue and puts the end marker itself, so no delta can arrive after `turn.end`.
- All ledger and state writes happen on the event-loop thread (`record()`).
- Every turn owns a `threading.Event` (`Session.cancel`) that is handed to the step factory and
  reaches the HTTP stream. Nothing sets it yet; interrupt and steer (partition 2) build on it.
- **The daemon survives a blocked model**: `LLMError` (no credit, rate limit, offline, no model
  set) becomes an `error` event, an `error` line and `turn.end {kind: "error"}`.
- The socket is created under umask 0177 (born 0600). A stale socket file is removed; a live one
  is refused. `close()` closes attached clients first.

## 7. Invariants and the tests that hold them

| Invariant | Test |
|---|---|
| One ledger writer, one partition function, one config writer | `test_invariants.test_single_writer` |
| Closed vocabulary | `test_ledger.test_ledger_closed_vocab` |
| A session spanning 13 days is read back whole | `test_ledger.test_partition_roundtrip` |
| Two processes never share a `seq` | `test_ledger.test_two_ledgers_never_duplicate_seq` |
| State is rebuildable; apply is idempotent | `test_state.test_reindex_restores_history` |
| Only what was said is remembered | `test_state.test_restore_said_only` |
| Conversation survives deleting `state.db` | `test_daemon.test_daemon_resumes_after_state_deleted` |
| The daemon survives a blocked or unset model | `test_daemon.test_daemon_survives_model_402`, `test_unset_model_is_an_error_not_a_crash` |
| `seq` survives an empty or truncated newest partition; an append after a fragment is readable | `test_ledger.CrashRecoveryTest` |
| A turn is counted once, even by two processes | `test_state.test_two_states_on_one_db_count_each_turn_once` |
| What was streamed reaches history even when the budget runs out | `test_daemon.test_exhausted_turn_still_records_what_was_said` |
| Mid-stream failures are `LLMError` | `test_llm.test_mid_stream_failures_become_llm_errors` |
| No date is computed outside `ledger.partition()` | `test_invariants.test_single_writer` (scans the whole package) |
| A bad line never costs the connection | `test_daemon.test_daemon_unknown_line_keeps_connection` |
| Socket is 0600, unix only, no model IDs in source | `test_daemon.test_socket_perms`, `test_invariants.test_no_model_ids_or_tcp_in_source` |
| Reasoning is never echoed back | `test_loop.test_loop_does_not_echo_reasoning` |

## 8. How to count

```bash
python3 -m unittest discover -s tests            # all tests, no network
wc -l pathfinder/ledger.py pathfinder/state.py pathfinder/daemon.py pathfinder/loop.py pathfinder/config.py
python3 scripts/scan_personal.py --all           # personal-data scan over tracked files
```

Core budget (blueprint §6): ledger 400, state 600, daemon + lane 600, loop 400; a module that
passes 1.3x its share is reported at the end of its partition.

## 9. Running it

```bash
python3 -m pathfinder config set model provider:model-name
python3 -m pathfinder daemon
python3 -m pathfinder say "hello"
python3 -m pathfinder reindex        # refuses while a daemon is serving; a restart catches up by itself
```

`config.json` values are type-checked at both doors (`config set` and a hand-edited file);
`max_steps` must be at least 1. The home directory is forced to 0700 even when it already existed.
