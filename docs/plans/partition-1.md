# Partition 1 — the minimal round trip

**"It runs" =** a client connected with `nc -U` sends one line, the answer streams back token by
token, the whole turn is in the ledger, and after deleting `state.db` the conversation is restored
from the ledger alone.

Scope is deliberately small. NOT in this partition: tools, broker, policy, sandbox, approval gate,
steer, interrupt handling in the daemon (partition 2); signals and rules (3); jobs (4); memory and
search (5); catalog and installer (6); TUI, onboarding, Anthropic adapter, Keychain (7).

Python >= 3.11, **standard library only**, tests with `unittest`
(`python3 -m unittest discover -s tests -v`). Code and comments in English. No model IDs, secrets,
emails, home paths or account names in tracked files.

## Layout

```
pyproject.toml                 name "pathfinder", no dependencies, script: pathfinder = pathfinder.__main__:main
pathfinder/__init__.py
pathfinder/config.py           home(), load(), set()          budget  ~90
pathfinder/ledger.py           the one ledger                 budget  400 (this partition ~180)
pathfinder/state.py            derived SQLite state           budget  600 (this partition ~160)
pathfinder/llm/__init__.py     resolve()                      adapter ~50
pathfinder/llm/openai_compat.py  streaming chat completions   adapter ~200
pathfinder/loop.py             the agent loop                 budget  400 (this partition ~130)
pathfinder/daemon.py           socket, sessions, lane         budget  600 (this partition ~260)
pathfinder/__main__.py         daemon | reindex | say         ~80
pathfinder/prompts/system.txt  base system prompt (short)
tests/test_*.py
```

## config.py

- `home() -> Path` — `$PATHFINDER_HOME` or `~/.pathfinder`; created with mode 0700.
- `DEFAULTS` — `{"model": "", "worker_model": "", "embed_model": "", "providers": {"ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": ""}}, "max_steps": 8, "session_resume_s": 7200}`. The model is EMPTY by default: a model name is configuration, never code.
- `load(home) -> dict` — DEFAULTS overlaid with `config.json`. **An unknown top-level key raises `ConfigError`** (a key nobody reads is how Mark.5 accumulated dead settings).
- `set(home, key, value, ledger=None) -> dict` — the ONLY writer of `config.json` (atomic: write temp, `os.replace`). Appends a `config_set` event (key only, never the value) when a ledger is given.

## ledger.py — the one ledger

- File layout: `<home>/events/<partition>.jsonl`, one JSON object per line, append-only.
- `partition(ts: float) -> str` — UTC date `YYYY-MM-DD`. **The only place in the codebase that maps a time to a partition.** (Mark.5 lost 24 days of learning because two modules defined "the day" differently.)
- `TYPES` — closed frozenset for this partition: `session_start, user, assistant, tool_call, tool_result, turn_start, turn_end, error, config_set`. `ORIGINS = {user, signal, job, system}`. `TRUST = {user, self, third_party}`.
- `class Ledger(root: Path)` — `root` is the PATHFINDER home; the ledger owns `root / "events"` and creates it. (`State(path)` takes the DB file path, `home / "state.db"`.)
  - `append(type, *, session="", turn="", origin="system", trust="self", **data) -> dict` — validates type/origin/trust against the closed sets (`ValueError` otherwise), rejects `data` keys that collide with reserved fields, assigns `seq` (global, strictly increasing, starts at 1) and `ts` (time.time()), writes one line, flushes, returns the event. Thread-safe (one `threading.Lock`). **The only function in the codebase that writes under `events/`.**
  - Safe across processes too (the CLI may append while the daemon runs): each append holds an exclusive `flock` on `events/.lock` and re-derives the last seq from the file tail when the newest partition changed size behind its back. Test: `test_two_ledgers_never_duplicate_seq`.
  - `read(after: int = 0) -> Iterator[dict]` — all events with `seq > after`, in order, across partitions. A truncated last line (crash mid-write) is skipped, not fatal.
  - `last_seq -> int` — recovered at construction from the newest partition's last good line.
- Reserved fields: `seq, ts, type, session, turn, origin, trust`.

## state.py — derived, always rebuildable

- `class State(path: Path)` — SQLite, WAL. Tables: `meta(key, value)`, `sessions(id, started_ts, last_ts, turns)`, `messages(seq PRIMARY KEY, session, role, content, ts)`.
- `apply(event) -> None` — THE projection. Idempotent: ignores `seq <= cursor`; advances the cursor in the same transaction. Rules: `session_start` creates the session; a `user` event with `origin == "user"` and an `assistant` event with `final == true` become messages (**only what was said is remembered** — signal envelopes and intermediate tool chatter never become history); every event with a session bumps `last_ts`; `turn_end` increments `turns`.
- `cursor -> int`; `catch_up(ledger) -> int` applies everything after the cursor (called at daemon start, so a deleted or stale DB heals itself); `rebuild(ledger) -> int` drops the tables and replays from 0.
- `history(session) -> list[dict]` — `[{"role", "content"}, ...]` in seq order. `latest_session(max_age_s, now=None) -> str | None`. `session(sid) -> dict | None` — `{id, started_ts, last_ts, turns}`.
- `user` and `assistant` events carry their content in the data key `text` (assistant: `text=..., final=True`).
- The daemon always does `event = ledger.append(...)` then `state.apply(event)`: live writes and rebuild share one code path.

## llm/

- `resolve(cfg, ref) -> Endpoint(base_url, api_key, model)` — `ref` is `"provider:model-name"`, split on the FIRST colon (model names contain colons). Unknown provider or empty ref raises `LLMError(0, ...)` with a message that tells the user to set `model`. API key comes from the env var named by `api_key_env` (empty = no auth header).
- `openai_compat.stream(endpoint, messages, tools=None, cancel=None, timeout=120) -> Iterator[tuple[str, object]]`
  - POST `{base_url}/chat/completions`, `stream: true`, `stream_options: {"include_usage": true}`, inside `with urlopen(...)`.
  - Yields `("text", str)` for content deltas, `("reasoning", str)` for reasoning deltas (fields `reasoning`, `reasoning_content`, `thinking`), then once at the end `("tool_calls", [{"id", "name", "arguments": dict}])` if any (assembled from indexed deltas; arguments may arrive as a JSON string in pieces OR as an object; an unparsable string becomes `{"_raw": "..."}`; an empty id becomes `call_<n>`), then `("usage", dict)` if the server sent one.
  - `cancel` is a `threading.Event`: checked between chunks; when set, close the response, yield `("cancelled", None)` and stop.
  - HTTP errors raise `LLMError(status, message[:300])`; `LLMError.blocked` is True for 402 and 429. Network errors raise `LLMError(0, ...)`.
  - `parse_sse(lines) -> Iterator[dict]` is a pure function (bytes or str lines -> decoded JSON chunks, stops at `[DONE]`) so tests need no network.

## loop.py

- `@dataclass Outcome(kind, text="", steps=0, usage=None)` — `kind` is `answer | exhausted | cancelled`. (`plan` arrives with the approval gate in partition 2.)
- `run(step, messages, *, call_tool, emit, budget=8) -> Outcome`
  - `step(messages) -> Iterator[tuple[str, object]]` is the adapter stream already bound to endpoint and tools. The loop never sees a model name, an origin or a policy.
  - Per step: forward `text` deltas via `emit("delta", str)`, ignore `reasoning` for history, collect tool calls. Append to `messages` an assistant message rebuilt as `{"role", "content", "tool_calls"?}` (never echo reasoning back; `tool_calls[].function.arguments` re-serialized as a JSON string). No tool calls -> `Outcome("answer")`. Otherwise for each call: `emit("tool", {"name", "ok"})`, `result = call_tool(name, args)`, append `{"role": "tool", "tool_call_id", "content": json}`. `cancelled` -> `Outcome("cancelled")`. Budget spent -> `Outcome("exhausted")`. The budget is a cost ceiling, not a stopping rule.
  - Usage dicts are summed into `Outcome.usage`. Exceptions propagate: whether a dead model is fatal is the caller's decision.

## daemon.py

- `class Daemon(home: Path, step_factory=None)`; `async serve()`. Unix socket `<home>/pathfinder.sock`, mode 0600, **no TCP**. A stale socket file is removed at start; a live one is an error.
- Protocol: NDJSON, one object per line. Inbound `attach {session?}`, `say {text}`. Outbound `attached {session, resumed, turns}`, `turn.start {turn}`, `delta {text}`, `tool {name, ok}`, `turn.end {turn, kind}`, `error {message}`. **An unknown or malformed line gets an `error` and the connection stays open.**
- `attach` without a session resumes `state.latest_session(session_resume_s)` or starts a new one (`session_start` event). `say` before `attach` attaches implicitly.
- Lane: one `asyncio.Queue` + one worker task per session, so a session runs one turn at a time and a second `say` waits. The turn itself is blocking and runs in `asyncio.to_thread`; deltas come back with `loop.call_soon_threadsafe`.
- A turn: `user` event (origin user, trust user) -> `turn_start` -> messages = system prompt + `state.history(session)` -> `loop.run(...)` -> `assistant` event with `final: true` -> `turn_end {kind, steps, usage}`. `call_tool` in this partition returns `{"ok": false, "error": "no tools yet"}`.
- **The daemon survives a blocked model.** `LLMError` (402, 429, network, no model configured) becomes an `error` event, an `error` line to the client and `turn.end {kind: "error"}`; the process keeps serving.
- `step_factory(cfg) -> step` is injectable so tests run the whole daemon against a fake model. At start: `state.catch_up(ledger)`.

## __main__.py

`pathfinder daemon` · `pathfinder reindex` (rebuild state from the ledger, print the event count) · `pathfinder say "text"` (attach, say, print deltas until `turn.end`; exit code 1 on error) · `pathfinder config set KEY VALUE`.

## Tests that must exist (names are part of the contract — blueprint §11)

- `test_ledger_closed_vocab` — unknown type/origin/trust is rejected.
- `test_partition_roundtrip` — events appended with a patched clock across 13 days are all read back in order; `last_seq` survives reopening; a truncated last line is tolerated.
- `test_single_writer` — scans `pathfinder/` source: only `ledger.py` opens a path under `events/` for writing and only `config.py` writes `config.json`.
- `test_reindex_restores_history` — delete the DB, `rebuild`, `history()` is identical; `apply` is idempotent.
- `test_restore_said_only` — a `user` event with origin `signal` and a non-final assistant event do not appear in `history()`.
- `test_sse_parse_and_tool_call_assembly` — pieces, object arguments, empty id, unparsable arguments.
- `test_loop_tool_roundtrip`, `test_loop_budget_exhausted`, `test_loop_does_not_echo_reasoning`.
- `test_daemon_round_trip` — real unix socket in a temp dir, fake model: attach, say, deltas, `turn.end`, ledger has user/assistant/turn events.
- `test_daemon_survives_model_402`, `test_daemon_unknown_line_keeps_connection`, `test_socket_perms` (0600), `test_daemon_resumes_after_state_deleted`.
