# v2 Partition 1 — the skeleton: one ledger of results, one assembler, two adapters, a router, the root clone, folding

**Where this stands (2026-10-17).**
- **Agreed:** blueprint v2 (V1–V19), the owner's V20 (tool search = local bge-m3 embeddings, index in P2) and V21
  (openai_compat renames `properties`/`description` parameters both ways, here). Built in place on `build`.
- **Measured (HISTORY 2026-10-17):** history is append-only on both providers; blocks are replayed verbatim inside a
  turn; Haiku emitted thinking only at effort xhigh (HISTORY's "thinks by default" bullet contradicts its own M2
  run — this contract trusts the run); the store costs ~1.5× requests. A 7-request probe (10-17) showed the
  `clear_at` beta accepted and the tools+system cache prefix fully read across cleared resident copies.
- **Decided (2026-10-17):** Q2 (a), Q3 (a), Q4 = V22 "hand-off", Deviations 1/4/5 approved (Decisions). **Next:** U1 ∥ U3.

**"It runs" =** the root clone answers one user turn end to end on Haiku; a tool result is stored as `r:N`
and reopened with `read_result`; a write is handed off at a gate (V22) while the root's turn ends; the daemon is
stopped and its index deleted; after restart the open gate is still listed, and approving it runs the call and the
root reports it in a new turn assembled from the ledger; a turn killed mid-run is
closed as `interrupted`; a blocked first model falls back to gemma4. Then M6 and M8 pass. Model IDs live in
config only; `$ROOT_MODEL`/`$FALLBACK_MODEL` below are set by the operator.

### The live check (after U8b, again after U10b; ~15 Haiku requests at xhigh ≈ $0.03, 2 gemma4 requests)

```
# 0. setup (WT = the partition-1 worktree; daemons are stopped by pid only, never pkill)
export WT="<worktree>" PATHFINDER_HOME=/tmp/pf1 ROOT_MODEL="<model from config>" FALLBACK_MODEL="<model from config>"
rm -rf /tmp/pf1 /tmp/pf1-venv && mkdir -m 700 /tmp/pf1 /tmp/pf1/check && python3 -m venv /tmp/pf1-venv
/tmp/pf1-venv/bin/pip install -q -e "$WT" && PF=/tmp/pf1-venv/bin/pathfinder CHK="python3 $WT/bench/v2/p1_runs.py"
security find-generic-password -s pathfinder-anthropic -a api >/dev/null      # key present in the Keychain
$PF config set providers '{"anthropic": {"api": "anthropic", "base_url": "https://api.anthropic.com", "effort": "xhigh"}, "ollama": {"base_url": "http://localhost:11434/v1"}}'
$PF config set models "{\"root\": [\"anthropic:$ROOT_MODEL\", \"ollama:$FALLBACK_MODEL\"], \"summarize\": [\"anthropic:$ROOT_MODEL\"]}"
mkdir -m 700 /tmp/pf1/workspace && (cd "$WT/bench/v2" && python3 -c 'import json, fake_notion as f; w = f.World(); json.dump({"results": w.pages}, open("/tmp/pf1/workspace/pages.json", "w"), ensure_ascii=False); print(w.by_title["휴가 계획"]["id"])') > /tmp/pf1/check/want
start() { $PF daemon >>/tmp/pf1/check/log 2>&1 & echo $! > /tmp/pf1/check/pid
          for _ in $(seq 50); do [ -S /tmp/pf1/pathfinder.sock ] && return 0; sleep 0.2; done
          echo "daemon did not start in 10 s"; tail -20 /tmp/pf1/check/log; return 1; }
stop()  { kill "${1:--TERM}" "$(cat /tmp/pf1/check/pid)"; while kill -0 "$(cat /tmp/pf1/check/pid)" 2>/dev/null; do sleep 0.2; done; }
```
1. `start && $PF say "Open /tmp/pf1/workspace/pages.json and give me the exact id of the page titled '휴가 계획'."` →
   the id in `check/want`. `$CHK turn /tmp/pf1 "$(cat /tmp/pf1/check/want)"`: a `result` for `read_file` with seq N, a
   later `tool_use read_result` with `handle: "r:N"` and its `result`; the final text holds the id; every `assistant`
   names the chain head; no `error`; `cache_read_input_tokens` > 0 from step 2, and on step ≥ 3 above the first request's
   cache write (the history breakpoint read across cleared copies; else take U4's fallback constant and report). Haiku's
   minimum cacheable prefix is 512 tokens (provider docs) and tools + system are ~3k, so a 0 means an invalidator.
2. `$PF say "Write that id, and nothing else, into /tmp/pf1/workspace/answer.txt."` → a `gate` line; the root says it
   asked for approval and its turn ends. `$PF approvals` lists gate N by its seq (tool `write_file`, args). `$CHK gate
   /tmp/pf1`: a `gate` event, no `result` for that call, then `turn_end answer`; the root's row `parked`.
3. `stop; rm -f /tmp/pf1/state.db*; start && $PF approvals` → the same gate, rebuilt from the ledger. `$PF approve
   "$($PF approvals | awk 'NR==1{print $1}')"` → the call runs and the root reports it (printed until that turn's
   `turn.end`). `$CHK restart ...`: `resume {approved}`, `result {gate: N}` `ok: true`, `answer.txt` = the id, then
   `user {origin: system, gate: [N]}`, `turn_start`, an `assistant`, `turn_end answer`; no `interrupted`, no HTTP 400; it
   prints `thinking blocks before the restart: k` — k = 0 means the post-restart strip is untested, not passed.
4. `$PF say "Run the shell command: sleep 30" & until grep -q '"name": "run_command"' /tmp/pf1/events/*.jsonl;
   do sleep 0.2; done; stop -9; start && $CHK interrupted /tmp/pf1`: `interrupted {why: restart}` and `turn_end
   interrupted` for that turn, written by `restore()` (kill -9: a SIGTERM `close()` writes no root events either, but
   -9 also covers the crash). Then `$PF say "Did the last command finish?"` → it says the outcome is unknown; no 400.
5. `$PF config set providers '{"anthropic": {"api": "anthropic", "base_url": "http://127.0.0.1:9"}, "ollama":
   {"base_url": "http://localhost:11434/v1"}}'; $PF say "Say hello in five words."` → an answer. `$CHK fallback
   /tmp/pf1`: an `error` (anthropic ref, status 0), then an `assistant` whose `model` is the ollama ref.
6. `stop; rm -rf /tmp/pf1 /tmp/pf1-venv`.

Rests on: V3, V5, V22 (root gates hand off), V11, V12, V13, V14, V21; HISTORY 2026-10-17. NOT in this partition (see
"Stubbed"): the tool layer (2), the clone tree and presets (3), self-record and night clone (4), attention, standing
tasks and channels (5), daemon 300 and view messages (6).

## Shape

Dependency: `anthropic` (the official SDK) joins `pyproject.toml` — 0 → 1 (V13), pinned `>=` the partition-0
bench venv's version, `<` its next major. Everything else stays stdlib. Test ceilings are §6 × 1.3 (the v1 margin
rule §6 keeps); targets are §6.

| Module | §6 target / test | Now → after P1 | Imports (package) | Imported by |
|---|---|---|---|---|
| `ledger.py` | 200 / 260 | 152 → ~175 | keychain | daemon, init, `__main__`, memory, keychain* |
| `state.py` | 600 / 780 (800 from U1 to U10a) | 695 → 800 (at the temporary ceiling until U10a) | clock | daemon, `__main__`, memory, senses/catalog |
| `results.py` (new) | 300 / 390 | ~200 | tools | assemble, clone |
| `assemble.py` (new, holds fold) | 400 / 520 | ~330 | llm, results | clone |
| `clone.py` (new) | 400 / 520 | ~380 | assemble, broker, llm, results, tools | daemon |
| `router.py` + `llm/**` | 550 / 715 | 385 → ~600 | llm, llm.anthropic, llm.openai_compat, keychain | clone, daemon, memory (embed) |
| ↳ `llm/openai_compat.py` (urllib SSE + `to_wire` + `complete`; `stream` leaves in U9; inside the pool row) | 200 / 260 | 135 → ~210 | llm, llm.rename | router, memory (embed) |
| ↳ `llm/rename.py` (new; V21; inside the pool row) | 80 / 105 | ~70 | — | llm.openai_compat |
| `daemon.py` | 300 at P6 / 780 kept | 778 → ~600 | (≈15: not yet under the ceiling) | `__main__` |

\* keychain's function-level import of `ledger.locked` predates v2; it is on the coupling test's `NOT_YET` list.
`daemon` loses now (~250 lines): `default_step_factory`, `turn`/`resume_turn`/`_run_turn`, `Session.control` and the
steer cut, the signal model turn, jobs wiring, `job.*`/`memory.run`, the slot; gains ~80 (the root task, `approve`/`deny`/
`interrupt` routed to it, restore). Later: triage and outbox leave in P5, the socket table and protocol in P6 — to 300.

Public interfaces (one line each; everything else private):

- `ledger`: unchanged — `Ledger(root: Path)`, `append(type: str, *, session="", turn="", origin="system",
  trust="self", **data) -> dict`, `read(after: int = 0) -> Iterator[dict]`, `partition(ts: float) -> str`. New:
  `TYPES` (below); `assistant.blocks` is written verbatim (exempt from `mask`/`vault`, Deviation 4).
- `state` (new readers): `result(seq: int) -> dict | None` · `context(clone: str, session: str, after: int) ->
  list[dict]` · `last_fold(clone: str, session: str) -> dict | None` (the newest `summary` context row) ·
  `clone_row(id: str) -> dict | None` · `gate(seq: int) -> dict | None` · `open_gates() -> list[dict]` (every gate not
  yet reported, decided or not) · `approval_item(seq: int) -> dict | None` (a gate or a proposal by its event seq;
  `kind: gate|proposal`).
- `results`: `READ_RESULT: dict` (neutral tool spec) · `summary(body) -> dict` (rule renderer) · `render(event:
  dict) -> str` (the tool message for a `result`) · `read(state: State, seen: set[int], args: dict) -> tuple[bool,
  object]` · `cap(body) -> tuple[object, dict | None]` (the store cap; the dict describes the cut).
- `assemble`: `View(id: str, role: str, session: str, epoch: int, model: str = "")` (frozen) · `assemble(state:
  State, view: View, tools: list[dict]) -> Request` · `window_handles(state: State, view: View) -> set[int]` ·
  `resident(parts: list[str]) -> str` · `fold_point(state: State, view: View) -> int | None` · `fold_request(state:
  State, view: View, upto: int) -> Request` · `handle_lines(state: State, view: View, upto: int) -> list[str]`.
- `llm`: `Endpoint`, `LLMError` (kept) · `resolve(cfg: dict, ref: str) -> Endpoint` (key: env `api_key_env`, else, for
  `api: "anthropic"` providers only, Keychain service `pathfinder-<provider>`, account `api`; a local provider never
  triggers a `security` call) · `Request`, `Response` (below).
- `llm.anthropic`: `async complete(endpoint: Endpoint, req: Request, on_text: Callable[[str], None] | None) ->
  Response` (cancelled by cancelling its task) · `to_wire(req: Request, endpoint: Endpoint) -> dict` (pure).
- `llm.openai_compat`: `async complete(endpoint, req, on_text, cancel: threading.Event | None) -> Response` (runs
  urllib in a thread) · `to_wire(req, endpoint) -> dict` · `embed(endpoint, texts) -> list` and `parse_sse` (kept).
- `llm.rename` (V21, used by `openai_compat`): `rename_out(tools: list[dict]) -> tuple[list[dict], dict]` ·
  `rename_in(tool_name: str, args: dict, renames: dict) -> dict` · `rename_args_out(tool_name: str, args: dict,
  renames: dict) -> dict`.
- `router`: `Router(load_cfg: Callable[[], dict])` · `async complete(self, req: Request, on_text=None, cancel=None)
  -> Response` (sets `Response.model` to the `provider:model` ref; adapters return the bare endpoint model) · `chain(self, role: str, override: str = "") -> list[str]`.
- `clone`: `Item(kind: str, text: str, session: str, origin: str = "user", gate: int = 0)` (kind
  `user|schedule|continue|signal`; `signal`: M6 now, attention in P5) · `Clone(id: str, role: str, *, home: Path, state:
  State, record: Callable[..., dict], router: Router, send: Callable, enqueue: Callable[[Item], None], resident:
  Callable[[], Awaitable[list[str]]])` · `async run(self, item: Item) -> str` (the `turn_end` kind) · `decide(self, gate:
  int, decision: str, by: str) -> bool` (records `resume`, runs the detached call on approval, enqueues the report item;
  False if unknown or decided) · `interrupt(self) -> None` · `restore(self) -> None` · `state: str` (`running|parked` for
  the root; `waiting|done|failed` are P3's children).

## Data

**Ledger types.** `TYPES` += `thought, self_note, summary, result, spawn, progress, gate, resume, return, triage,
task_open, task_wake, task_update, task_close` (`interrupted` exists). Removed in U10a with their last writers:
`tool_call, tool_result, job_start, job_done, job_failed, session_summary`, and `memory_pass` if Q2=a leaves it no
writer (the implementer greps; `test_no_new_ledger_type` pins it). Every new event carries `clone`. ORIGINS are
untouched until policy is rewritten (P2).

| Type | Writer (partition) | Fields (besides seq, ts, session, turn, origin, trust) |
|---|---|---|
| `assistant` (extended) | clone (1), one per model step | `clone, step, model, blocks, stop_reason, usage, text, final` |
| `result` | clone (1), one per run call | `clone, step, call_id, tool, ok, body` (raw, ≤ cap; trust third_party), `cut` when capped, `gate` for a handed-off call |
| `user` (extended) | daemon (an opening say, with the item's turn id), clone (a steer, report item, budget note) | `clone`, `steer` (a say that arrived while a turn ran: the daemon enqueues it, the clone records it with the item's turn id at the step boundary), `gate` (a report turn: the list of gate seqs it reports; `gate` is always a list; a single report is `[N]`) |
| `gate` | clone (1), instead of a `result` | `clone, step, call_id, tool, args, effect, brief` |
| `resume` | clone (1) | `clone, gate` (its seq), `decision` (`approved|denied`), `by` (`socket|chat`) |
| `interrupted` (extended) | clone (1) | `clone, step, why` (`user|restart`) |
| `turn_start` / `turn_end` (extended) | clone (1) | start: `clone, item, resident`; end: `clone, kind, steps, usage` |
| `error` (extended) | clone (1), one per failed attempt | `clone, step, model, status, message` (≤ 300 chars) |
| `summary` | clone after a fold (1) | `clone, first, upto, text, handles, model, usage` |
| `spawn` | daemon, once per home for the root (1); clone (3) | `clone, parent, preset, brief, model, budget` |
| `progress` / `return` | clone (3) | `clone, parent, text` / `clone, parent, status, text, handles` |
| `thought` / `self_note` | clone, night clone (4) | `clone, step, plan, of` (seq of the `assistant` holding the reasoning) / `clone, kind, text, evidence` |
| `triage` | attention (5) | `signal_key, action, urgent, line, model` |
| `task_open/wake/update/close` | root, worker pair, reviewer (5) | `task` + `goal, trigger, budget, end` / `why` / `state` / `why` |

`turn_end.kind` ∈ `answer | refused | budget | max_tokens | error | interrupted` (`interrupted` covers a user
interrupt and a restart; `why` is on the `interrupted` event). A gate never ends a turn (V22).

```
{"seq": 812, "type": "assistant", "turn": "t9", "origin": "user", "trust": "self", "clone": "root", "step": 2, "model": "anthropic:<model from config>", "blocks": [{"type": "thinking", "thinking": "...", "signature": "..."}, {"type": "tool_use", "id": "toolu_1", "name": "read_result", "input": {"handle": "r:810", "query": "휴가 계획"}}], "stop_reason": "tool_use", "usage": {"input_tokens": 410, "output_tokens": 96, "cache_read_input_tokens": 9800, "cache_creation_input_tokens": 0}, "text": "", "final": false}
{"seq": 810, "type": "result", "turn": "t9", "origin": "user", "trust": "third_party", "clone": "root", "step": 1, "call_id": "toolu_0", "tool": "read_file", "ok": true, "body": "{\"results\": [ ...100 items... ]}"}
{"seq": 845, "type": "gate", "turn": "t10", "origin": "user", "trust": "self", "clone": "root", "step": 1, "call_id": "toolu_3", "tool": "write_file", "args": {"path": "/tmp/pf1/workspace/answer.txt", "content": "..."}, "effect": "write", "brief": "Write that id, and nothing else, into ..."}
{"seq": 851, "type": "resume", "turn": "t10", "origin": "user", "trust": "user", "clone": "root", "gate": 845, "decision": "approved", "by": "socket"}
{"seq": 902, "type": "summary", "session": "s1", "origin": "system", "trust": "self", "clone": "root", "first": 700, "upto": 790, "text": "...", "handles": ["r:712", "r:760"], "model": "anthropic:<model from config>", "usage": {"input_tokens": 7100, "output_tokens": 640}}
```

**`r:N` = the seq of a `result` event.** The body is in the ledger (truth) and copied into state (derived, so
`read_result` is one SELECT; planner's call). Store cap (`results.cap`, planner's call): 256 KB of the body's compact
JSON (or text). Over it, a list is cut at an item boundary (`fake_notion.cut`, lifted) and the event gets `cut:
{"kept": k, "of": n}`; any other value is cut as text with `cut: {"chars": n}`; the summary then says "stored k of n
(store cap)". Reasoning v1 threw away (loop.py:242) is kept from now on inside `assistant.blocks` (thinking with
signature; Ollama reasoning as `{"type": "reasoning"}`, never replayed); P4's `thought` points at it by seq.
Every approval id a person sees is a ledger seq (a gate's `gate` event, a proposal's `approval_pending` event; U1
adds `approvals.seq`): `approve <seq>` has one namespace.

**state.db** (all derived, dropped and replayed by `rebuild`; no titles or recipients until P3 has a reader):
```sql
CREATE TABLE IF NOT EXISTS results (seq INTEGER PRIMARY KEY, clone TEXT, session TEXT, turn TEXT, call_id TEXT,
  tool TEXT, ok INTEGER, body TEXT);
CREATE TABLE IF NOT EXISTS clones (id TEXT PRIMARY KEY, parent TEXT, preset TEXT, state TEXT, session TEXT,
  turn TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS gates (seq INTEGER PRIMARY KEY, clone TEXT, session TEXT, turn TEXT, call_id TEXT,
  tool TEXT, args_json TEXT, effect TEXT, brief TEXT, decision TEXT, reported INTEGER, result INTEGER,
  ts REAL);
CREATE TABLE IF NOT EXISTS context (seq INTEGER PRIMARY KEY, clone TEXT, session TEXT, turn TEXT, type TEXT,
  event_json TEXT);  -- user, assistant, result (body left out), turn_start, turn_end, gate, resume, interrupted, summary
CREATE INDEX IF NOT EXISTS context_by_clone ON context (clone, session, seq);
-- approvals gains `seq INTEGER` (ALTER TABLE, the existing pattern)
```
Projection: `turn_start` → clone `running`; `gate` → `gates` row (no root state change); `resume` → decision; `user {gate: [...]}` → `reported` on each listed gate;
`result {gate}` → the gate's `result`; `turn_end`/`interrupted` → `parked`; `spawn` → insert, `parked`. v1's session-keyed `summaries` table goes in U10a with
`session_summary`. `messages` keeps feeding `session_messages` (screens) from `final` assistant text.

**Request / Response** (`llm/__init__.py`, provider-neutral):
```python
@dataclass(frozen=True)
class Request:
    role: str               # a `models` key: root | worker | reviewer | triage | summarize | night
    system: str             # piece 1, byte-frozen
    tools: tuple = ()       # piece 2: {"name", "description", "parameters"}; read_result first, rest by name
    messages: tuple = ()    # pieces 3-5: {"role": "user", "content": [text | tool_result]} ·
                            # {"role": "assistant", "model": ref, "content": [blocks]} · {"role": "system", "content": resident, "item": turn}
    model: str = ""         # spawn override "provider:model"; "" = the role's chain
    max_tokens: int = 16000 # root; 2,000 for summarize
    clone: str = ""         # with `turn`, pins the router's chain position for this run
    turn: str = ""

@dataclass
class Response:             # blocks verbatim (thinking with signature) · stop_reason: end_turn | tool_use | max_tokens |
    blocks: list            # refusal | interrupted · usage: input/output/cache_read/cache_creation tokens · model: the
    stop_reason: str        # "provider:model" that answered · fallbacks: [{model, status, message}] tried before it
    usage: dict
    model: str
    fallbacks: list
```

**Inputs are read back, not passed along:** every piece is built from recorded events (context rows), never from an
in-memory string: the resident as `turn_start.resident` holds it, the user's words as vaulted, a result as masked.

**Resident block in P1** (≤ 4,000 chars; computed once per item from `Clone.resident()` and frozen in
`turn_start.resident`): `clock.now_line()`, `memory.context()` (USER.md body + wiki map), the existing agenda
(`signals.context`, D42) standing in for P5's. Each part clipped in order; the whole clipped at 4,000. No
`self/INDEX.md` (P4), no skills index (Deviation 8).

**Config** (`config.py`, through `config.set()` only): `models` = `{key: ["provider:model", ...]}`, keys closed to
`root, worker, reviewer, triage, summarize, night`, first = primary, the rest = the fallback chain, every provider must
exist. `providers.<name>` gains optional `effort` (`anthropic` only: `low|medium|high|xhigh|max`; absent = API default).
`max_steps` (8) stays the step budget. `model`/`worker_model` leave in U10a (§12 "single model setting");
`embed_model` stays (V20). Constants, not config, until a second value is needed: `router.CONCURRENCY = {"anthropic":
4, "openai": 1}` (per api), `clone.TOKEN_BUDGET = 100_000` per item, counted as `input_tokens +
cache_creation_input_tokens + output_tokens` (cache reads excluded: a tenth of the price, and a long window would
spend the budget by itself; openai_compat: prompt + completion).

## Behaviour

**The turn loop** (`Clone.run`, one lane item; the root is one asyncio task, tools run in `asyncio.to_thread`):

| # | What happens | Ledger | Screen |
|---|---|---|---|
| 1 | `say` (daemon): chat-approval check first if an open gate is newer than the last user message (D52-2); else, the root parked, record it as a new item's opening say (the item's turn id); a turn running, enqueue it for the clone, unrecorded (row 11) | `user {clone: root, turn}` | — |
| 2 | Root takes the item; resident parts gathered, frozen | `turn_start {item, resident}` | `turn.start` |
| 3 | `req = assemble(...)`; `resp = await router.complete(req, on_text=delta)` | `error` per failed attempt | `delta`s |
| 4 | Response recorded verbatim | `assistant {step, blocks, model, usage}` | — |
| 5 | `refusal` → fixed line ("the model declined; this exchange is left out of the conversation") | `turn_end refused` | that line |
| 6 | `max_tokens` → its `tool_use` blocks get "not run: the answer was cut at the output limit" | `result {ok: false}`, `turn_end max_tokens` | error line |
| 7 | No `tool_use` → done | `turn_end answer` | `turn.end` |
| 8 | Each `tool_use`, in order: unknown name / coerce error → refusal text; `read_result` → `results.read` | `result` | `tool` |
| 9 | Else `Broker.call`: allow → run; deny → `DENIED_TEXT`; `Gate` → hand-off (row 10) | `result` | `tool` |
| 10 | Gate (V22): the call is detached; the model gets the waiting status and the loop goes on | `gate` | `gate` |
| 11 | At each step boundary, immediately before it calls `assemble`, the clone drains the queued says and records them; they join the next user message as text blocks, so seq order equals what was sent | `user {steer: true, turn: <item turn>}` | — |
| 12 | Steps ≥ `max_steps` or tokens ≥ `TOKEN_BUDGET` → one last step, same tools, a budget note appended; its calls get "not run: budget spent" | `user {origin: system}`, `result`, `turn_end budget` | — |
| 13 | `interrupt` while running: step task cancelled, `control.interrupt` set (exec kills the process group); a running tool's late return is dropped | partial `assistant`, `result` "outcome unknown: interrupted by the user while it ran" for the started call, "not run: interrupted" for later calls of the batch, `interrupted {why: user}`, `turn_end interrupted` | `turn.end` |
| 14 | After `turn_end`: `fold_point` → fold (below) before the lane takes the next item | `summary` | — |

A partial `assistant` (cancelled stream) keeps only **complete** text blocks (no unsigned thinking, no half
`tool_use`); `text` holds everything streamed. Steps and tokens are counted from the turn's `assistant` events, so a
restart never resets them; a hand-off report is a new item with its own budget. Says after the final step become a
`continue` item (as v1). One root lane; items carry their session and the window is that session's (planner's call).

**Gate hand-off (V22, root only):**

| Moment | Ledger | The root sees |
|---|---|---|
| `Broker.call` raises `Gate` | `gate {clone, step, call_id, tool, args, effect, brief}`; no `result` | that `tool_use`'s `tool_result` = `{"status": "waiting for approval", "gate": N}`, rendered from the `gate` event (no body, no handle); the turn goes on and ends as any turn |
| `approve N` (socket, or chat approval) | `resume {gate: N, decision: approved, by}`; the call runs in a thread through `Broker(approved={call_id: (tool, args)})` (coerce, normalize; policy satisfied for that exact call only); `result {clone, gate: N, call_id, tool, ok, body}` = r:M | — |
| `deny N` | `resume {gate: N, decision: denied, by}` | — |
| the report item | `user {clone: root, origin: system, trust: self, gate: [N, …], text}`, then `turn_start` … as any item | opening user message: `gate N (write_file) approved → ` + `results.render(r:M)`, or `… approved → failed: <error ≤ 2,000>`, or `gate N (write_file) denied by the user` |

U7 writes the failed-gate report line as "failed: " + `results.render(event)` and never clips the error itself.
A `result` with `gate` is never rendered as a `tool_result` (its `tool_use` was answered by the status); it reaches the
model through the report and `read_result` (r:M is in the window, so in the seen set). `decide` enqueues
`Item(kind="continue", text, session=the gate's, gate=N)`; the clone records `user {gate}` when it takes the item, not
at `decide` (a running turn must not pull it in: steers are `user` events with `steer: true` only). An `interrupt` leaves
open gates open — it stops what runs now, and a gate is a decision the user has not made, still listed (deny drops it);
it does kill a handed-off call already running after approval: `result {gate: N}` "outcome unknown: interrupted by the
user while it ran". Children (P3) keep V7's waiting in place, so M2's in-place resume path is deferred to them; in P3 a
detached call is simply a child clone.

Reports are merged: when the root takes a gate report item it drains every other queued report item of the same session
into the same turn — one opening user message with one line per gate in gate order (`gate N (tool) approved → r:M …` /
`denied` / `failed`), one `user {gate: [N, …]}` event, one turn, one budget. A new user `say` is never merged with reports.

**What the model sees** (§7-1; chars of rendered text, thinking excluded):

| # | Piece | P1 content | Budget | Anthropic adapter | openai_compat |
|---|---|---|---|---|---|
| 1 | Static system | `prompts/root.txt`: identity, handles + `read_result` use (M1's store arm), what a gate and a denial mean, output form | 6,000 (test) | `system`, breakpoint 2 | system message 1 |
| 2 | Tools | `read_result` + `tools.for_origin("user")` incl. MCP, sorted | 8,000 (test: native + core; Deviation 7) | `tools`, breakpoint 1 on the last | `tools` after `rename_out` |
| 3 | Resident | the item's frozen copy | 4,000 | primary: a copy after every user message of the item, `{"role": "system", "clear_at": "next_user_message"}` (beta `mid-conversation-system-clear-at-2026-08-21`); fallback: only after the opening one, no `clear_at` | one copy after the item's opening user message, `role: system`, in place |
| 4 | History | this session's turns since the last fold; summary first ("Earlier in this conversation — a record, not a request:"); one resident copy per past item counts | 24,000 → fold | blocks verbatim; breakpoint 3 on the last user block before the trailing copy (never on a `clear_at` message: 400) | text + tool_calls (renamed); thinking/reasoning dropped |
| 5 | Current step | user text, steers, tool results via `results.render` (handle + summary ≤ 2,000; errors inline ≤ 2,000; `read_result` output inline ≤ 6,000, Deviation 6) | 8,000 per step; over it, later results shrink to the handle line | `tool_result` + text blocks in one user message | `tool` messages, then a user message for steers |

"User message" for resident placement = the item's opening user message (the say, schedule text, `continue` steers, a
gate report or a signal envelope) and each `tool_result` message of the item; steers ride inside them. All copies of an item are the
same bytes and are never removed — append-only on both providers. Window accounting counts one copy per past item for
both (fold points must not depend on which provider answered: a fallback can switch mid-session); Anthropic's cleared
copies cost 0 tokens, so it folds a little early there. The fallback is one constant in `llm/anthropic.py`, taken if
U4's placement tests or live-check step 1 show the history breakpoint missing.

Renderer (rule, lifting `fake_notion.summary`): list → `{type, count, scalar top-level fields without IDs, keys, preview:
first 3 titles}`; object → `{type, keys, scalar fields without IDs}`; text → `{type, chars, lines, first line ≤ 120}`; a
string that parses as JSON renders as that JSON. `read_result(handle, range | query | path)` lifts `fake_notion.Store.read`
(range: items for lists, characters for text; query: list items, or a text's lines with their char offsets). U6 builds
`render`'s input as `{**context_row, "body": state.result(seq)["body"]}` (a context row has no body; `state.result` has
no `cut`). **Seen set** = `assemble.window_handles`: the `r:N` of `result` rows in the current window plus the
last summary's `handles`; any other handle (another session's, a non-result seq) is refused. `tools.clean` (D62) applies
on the way to the model, never to the ledger. No `eager_input_streaming`, no `tool_choice`; thinking `{"type":
"adaptive", "display": "summarized"}` (same cost; P4 reads the text).

**V21 rename (openai_compat).** Only keys inside a schema's `properties` map are renamed, at any depth (object
`properties`, array `items`), never a JSON-schema keyword: `properties`/`description` → `<name>_`, more `_` if that
collides with a sibling. `rename_out` returns the tools and `renames = {tool: {path: {original: renamed}}}` (path = the
original property names down to that map). `rename_in` maps arguments back: if both names arrive the renamed one wins and
the original is ignored; an original-only key is kept (M9: the model sent `properties` when a description named it).
A `required` list of the same schema node follows the rename.
Replayed tool calls go out through `rename_args_out`, matching the schemas the model sees; the ledger holds original names.

**Fold (V5)**, after `turn_end`: if piece 4 > 24,000, the oldest whole turns fold until ≤ 12,000 remain; the last turn
never folds. `fold_request` (role `summarize`, `prompts/fold.txt` ≤ 1,500 chars, the previous summary + the folded
turns as text: user/assistant text, call names + args, result summary lines; max 2,000 tokens). The harness, not the
model, then appends one line per handle of the folded part and the previous summary — `r:712 read_file list 100`:
handle, tool, summary type and count, no third-party strings (no titles, no previews) — so handle survival never
depends on the model. A failed fold (all models blocked) records `error` and retries after the next turn.

**Restart (V12)**, `Clone.restore()` at daemon start, after `state.catch_up`; `close()` (SIGTERM) writes no root events.
No root row → `spawn {clone: root, preset: root}` once per home. `running` → `interrupted {why: restart}` + `turn_end
interrupted`, nothing re-run (a write could repeat). Open gates need nothing reassembled: the root never waits, the
`gates` rows return with `rebuild`, and `restore` only re-lists them (`approvals`). A decided gate whose report never
ran (`reported` unset) is enqueued again; an approved gate with no `result` first gets `result {gate: N, ok: false}`
"outcome unknown: interrupted by a restart" (never re-run). A recorded `tool_use` without a `result` renders as
`is_error` "outcome unknown: interrupted by a restart" — during a started tool or a batch alike; "not run" is
used only where the harness knows the call did not start. `View.epoch` = max(`ledger.last_seq` at this daemon's start,
seq of the last `summary` for this clone and session), computed by `Clone` when it builds the View; thinking of every
`assistant` with seq ≤ epoch is stripped.

**Append-only and thinking:**

| Where | Rule |
|---|---|
| Inside a turn | blocks replayed verbatim from `assistant.blocks`, thinking included |
| Across turns, same session, no fold/restart | still replayed: a sent block stays in every later request (Haiku keeps previous-turn thinking; stripping would miss the cache each turn) |
| After a fold | fresh prefix: summary + kept turns, their thinking stripped (epoch = summary seq) |
| After a restart | all thinking up to the epoch stripped (M2: 0 HTTP 400) |
| Interrupted step | only its complete text blocks: on cancel the adapter returns the blocks as they arrived with stop_reason `interrupted`; the clone (U7) keeps only complete text blocks when it records/replays the step (no reasoning/thinking, text marked partial). The Anthropic adapter catches `CancelledError` and returns a Response with stop_reason `interrupted`: the clone (U7) must treat that as the cancellation signal, not an exception. The clone checks `asyncio.current_task().cancelling()` after an `interrupted` Response and re-raises `CancelledError` unless the interrupt was its own (a flag it set). |
| Other model (fallback) | thinking blocks are kept only when the bare model name of the recording (the `provider:model` ref after its first colon) matches the endpoint model, the provider name ignored; otherwise the adapter drops them |
| Refused exchange | turns ending `refused` are left out of later windows (nothing was sent after them) |
| Never | an earlier message edited, a resident copy removed, a result re-rendered differently in one daemon life |

**Router:** each run (`clone`, `turn`) starts at the top of the chain; a failure on a run's first request moves to the
next model at once (V13: the clone boundary first); mid-run, a model is left only after 2 consecutive failures. No
memory of failures across runs (planner's call): during an outage every new item pays one failed attempt (plus the
SDK's own retries) before the fallback. Fallback on 0 (connection), 401, 402, 403, 408, 429, 5xx, 529; not on
400/404/413 (our bug: the user sees it) and not on `refusal` (a declined request is not laundered through another
model). One `asyncio.Semaphore` per api (`CONCURRENCY`); with one root this replaces slot.py's ordering, P3 adds root
priority. The model that answered is `Response.model` → `assistant.model`.

## Invariants and tests

| Test | Unit, file | Asserts |
|---|---|---|
| `test_history_has_handles_not_bodies` | U6 test_assemble | a 20 KB body with a marker: across later steps and turns the marker occurs in an assembled Request only inside the `tool_result` of a `read_result` call; `r:N` is in the window |
| `test_static_system_frozen` | U6 test_assemble | piece 1 and both adapters' `to_wire` `system`/`tools` byte-identical across steps and turns with changed time, USER.md, wiki |
| `test_fold_keeps_handles` | U6 test_assemble | a fold over 3 turns and 4 handles: `summary.handles` ⊇ them + the previous summary's; handle lines hold no title; `read_result` on one still works |
| `test_unknown_handle_refused` | U2 test_results | unknown `r:N`, one outside the window, a non-result seq → refused, nothing read |
| `test_open_gate_survives_restart_and_returns_as_item` | U7 test_clone | gate → the turn ends → close → delete state.db → rebuild → `restore` lists it → approve → the call ran once with the same args → `result {gate}` → report item → answer; no thinking after restart (M2's in-place resume: P3) |
| `test_append_only_history` | U7 test_clone | a 3-step turn + a second turn with a fake router: for both adapters each `to_wire` body extends the previous byte-for-byte, except at an epoch; for Anthropic system/tools compare exactly and messages with their `cache_control` markers removed (the history breakpoint moves forward, per the API docs) |
| `test_clone_transitions_in_ledger` | U7 test_clone | every root state change (parked → running → parked, interrupted) has its event; a gate changes no root state; the clones row is derived only from them |
| `test_fallback_chain` | U5 test_router | 402 → next answers, `fallbacks` holds it; first-request failure switches at once; mid-run 1 stays, 2 switch; 400 does not fall back; all blocked → `LLMError`, the lane lives (U8a) |
| `test_single_assembler` | U10b test_invariants | AST: `Request(` and `dataclasses.replace`/`replace(` on a Request only in assemble.py; `router.complete` called only in clone.py; adapters imported only by router (named exemption: `openai_compat.embed` in memory) |
| `test_no_new_ledger_type` | U1, then U10a | the new `TYPES` set exactly |
| `test_module_ceilings` | U1 (state 800, noted), U10b | the Shape table (state 780, results 390, assemble 520, clone 520, router+llm 715, ledger 260, daemon 780, broker+policy 455) |
| `test_core_under_6000` | U10b | ledger, state, daemon, clone, assemble, results, router, llm/*, broker, policy, exec, tools, memory, rules, signals, deliver ≤ 6,000 (≈ 4,850 expected) |
| `test_coupling_ceilings` (first) | U10b | P1 modules: ≤ 8 package imports, ≤ 8 importers (clock, config exempt), no function-level imports; `NOT_YET` = daemon, tools, keychain, memory, `__main__` — empty by P6 |

Also: U1 `test_assistant_blocks_written_verbatim`; U2 `test_result_body_capped_at_256k`; U3 `test_v21_rename_round_trip`
(nested, collision, both names, original only, replayed calls, ledger original); U4 `test_resident_breakpoint_placement`;
U7 `test_refusal_ends_turn_and_leaves_window`, `test_budget_note_keeps_tools`, `test_gate_status_rendered_from_gate_event`, `test_interrupt_leaves_gate_open`,
`test_decided_gate_requeued_on_restart`, `test_approved_gate_without_result_is_outcome_unknown`,
`test_partial_step_keeps_complete_text_only`, `test_restart_renders_outcome_unknown`, `test_thinking_stripped_up_to_epoch`;
U8a `test_approve_ids_are_ledger_seqs`.

**Retired** with the unit that removes what they pin (each is replaced above or is a §12 discard):
- U3: `test_llm.py`'s four `test_stream_*`/mid-stream tests → rewritten for `complete` (SSE, `resolve` tests stay).
  U4: `test_llm_anthropic.py` (10; the urllib adapter, V13) → rewritten on a fake SDK client.
- U8a: `test_broker.py`'s two clip tests (D62's clip; the store renders) and two emit-based refusal records (the clone
  records; filter tests move to test_results); `test_tools.py`'s read_file clip test; `test_daemon.py`'s steer cut,
  second look and retract (`test_daemon_say_during_turn_steers`, `test_steer_after_the_last_chunk_*`, `test_retract_*`,
  `test_withdrawn_draft_*`, `test_history_holds_only_the_rerun`, `test_turn_end_carries_check`, `test_steer_during_the_look_*`),
  step factories, signal turns (`test_signal_turn_*`, `test_say_during_signal_turn_*`). Rewritten on the root: round
  trip, second turn, 402, unset model, gate restart, deny, interrupt, reindex, resident frozen, schedule, USER.md/agenda,
  chat approval, MCP tool call.
- U9: `test_loop.py` (25), `test_jobs.py` (19), `test_slot.py` (2), `test_daemon.py`'s job tests and
  `test_memory_run_forces_a_pass`, the pass/`summarize`/`judge` tests in test_memory and test_rules (named in U9's report).
- U10a: `test_state.py`'s D52 record tests (`test_history_is_said_and_called_only`, `test_tool_record_*`, `test_a_record_*`,
  `test_record_skips_*`, `test_clipped_arguments_*`, …), D51's `test_withdrawn_draft_survives_rebuild`, v1's
  `test_history_folds_after_summary`, and the job-gate tests. U10b: `test_invariants.py`'s jobs/daemon line tests (into
  the table), `test_core_under_5500_lines`, `test_second_look_matches_the_bench`, `test_no_tool_record_without_tool_call_events`,
  the clause half of the envelope test; `test_rules_imports_no_llm_at_top_level` → "rules imports no llm".

## Discarded here, and what is only stubbed

Discarded (§12): D52 call record (`RECORD_HEADER`, `calls` rows, `CALL_ARGS_CLIP`); D62's clip and `LEDGER_CLIP`
(`clip_for_ledger`, broker's `_clip_for_model`, `read_file`'s 8,000 clip; exec's output clip stays until P2); D51 second
look, `RERUN_NOTE`, the forged-record check; dropping reasoning; `model`/`worker_model`; the urllib Anthropic adapter;
jobs.py, job resume (D24), `senses/jobs.py`, `start_job`; the signal-turn special case (`SIGNAL_TURN_BUDGET`,
`SIGNAL_TURNS_CLAUSE`, `_signal_model_turn`'s model part); D16 gate = turn end (V22: the turn goes on); `memory.summarize`/`session_summary` (the
fold replaces them); `llm/slot.py` (D25); lazy imports in `llm/__init__` and `rules.judge`; D30's tool-less last step
(→ the budget note, same tools: a changed tool list misses the whole cache).

Stubbed: `rules.judge` returns `(False, "model triage returns in partition 5")`, signals end at rules + summary notice
(5); the consolidation pass (4); clone gates and proposals coexist in `approve` (one seq namespace) until P2; gate
title/recipient (3); gate timeout and piece 2 over 8,000 with MCP tools (2); the mid-stream steer cut (D19) waits for
the TUI (6); ARCHITECTURE(.ko) rewrite (6, §14); nothing added to ToolSpec for V20 until P2.

## Measurements at the end of the partition (after U10b, each run on the lead's go-ahead)

**M6 — a day costs ≤ $1 and caches ≥ 70%.** `bench/v2/m6_day.py` (≤ 200 lines) drives the real `Clone`, assembler,
router and fold in a temp home on Haiku (root, summarize), with fake_notion's world behind `read_file`. Input
`bench/v2/m6_day.json` (synthetic, committed), each item with a scripted clock time: 30 user items (≥ 8 need tools, 2
gate and are approved), 20 signal envelopes as `signal` items, 5 child runs as fresh clone ids (role `worker`, a brief
first), 1 night run (role `night`, a 30k-char digest), folds as the window demands. Run at the product's effort, not the
live check's xhigh; `effort` is per provider, so summarize/triage/night run at the root's effort (the JSON records it).
Prices from the command line (`--price-in --price-out`; cache read 0.1×, 5-minute write 1.25×; none in code). Cold
adjustment: an item more than 300 s after the previous request has its first request's cache reads repriced as writes.
Pass (adjusted): cost ≤ $1.00 and Σcache_read / Σ(input + cache_read + cache_creation) ≥ 0.70; raw and 1-hour-TTL figures
beside. Output `docs/measurements/v2-m6-<model>-<date>.json` (tokens by kind, costs, hit rates, folds, a row per item).
≈ 150–200 requests, ≈ $0.10–0.30. Fail → cheaper role models, a smaller resident block.

**M8 — the fold keeps facts.** `bench/v2/m8_fold.py --ledger <home>/events --session <id> --questions <file>`, locally,
on the owner's real 10-14 Notion session (~109 turns) converted to v2 events in a temp home (`tool_call`+`tool_result`
→ `tool_use` + `result`, body = v1's 2,000-char clip, flagged). 10 questions with keys, written by the lead before any
run, kept outside the repo: ≥ 4 on the first third, ≥ 3 needing an ID or title only a folded handle holds, ≥ 1 trap the
session never answers. The session is replayed through the real fold (summarize, Haiku); the questions are user turns
with `read_result` as the only tool. Verdicts: correct, miss ("not in the record"), factual error (contradicts the key,
or a fabricated ID/title). Pass: 0 factual errors, 100% handles (each handle shown before a fold ∈ `summary.handles`, each
`read_result` on one succeeds); misses reported. Output `docs/measurements/v2-m8-<model>-<date>.json`: counts, question
ids, verdicts — no question text, answers or content (`scan_personal.py` before commit). ≈ 30 requests, ≈ $0.05.
Fail → V5's per-turn one-line index. Model IDs in these file names: approved 2026-10-17 (Decisions).

## Deviations from the blueprint

1. **The resident block is stored** (`turn_start.resident`), against §5 "working memory is not stored". Every earlier
   copy must replay byte-identical (HISTORY 10-17), and the clock, USER.md and agenda change. **Approved 2026-10-17.**
2. **A config `models` table keyed by role**, where §12 discards "the router's per-role model table" (V18). The keys are
   the preset names plus `root`: preset files are tracked and may not name a model, so a preset (P3) names its key.
3. **M6 and M8 run at the end of P1, not its entrance** (§13): they need the fold, the assembler and the router.
4. **`assistant.blocks` are exempt from the ledger's token mask** (D66's backstop). Masking a thinking block, a
   signature, or a text/tool_use block edits an earlier message on replay (400 or lost thinking). The model only saw
   masked inputs, so a block can hold only a token it made up; the screen `text` stays masked. **Approved 2026-10-17.**
5. **Resident copies:** Anthropic gets a `clear_at` copy after every user message of an item (beta; probe: accepted,
   tools+system prefix read; not yet shown: the history breakpoint across cleared copies — U4 test + live step 1), with
   one copy per item as the fallback; openai_compat always gets one copy per item kept in place — not §7-1's "second
   system message", which replaced each turn would edit history. **Approved 2026-10-17.**
6. **`read_result` output inline up to 6,000 chars**, where §7-1 gives a result 2,000: M1's store arm ran at 6,000 and
   still cost 1.5× the requests (Haiku reads one range at a time); at 2,000 a 50-item page takes three or more reads.
7. **Piece 2 exceeds 8,000 with MCP tools** until P2's `search_tools` (M5: Notion alone breaks 8k); P1 pins native + core.
8. **No skills index in the resident block** (§7-1 lists it): it stays in `read_skill`'s description; P4 revisits it.

## Decisions (2026-10-17)

- **Q1:** built in place on `build` (CLAUDE.md); U1–U7 only add, U8a is the one break point, revertible until U9.
- **Q2 = (a):** `loop.py`, `jobs.py`, `llm/slot.py`, the memory pass, `summarize` and `rules.judge` go in U9; the branch
  has no jobs until P3, no consolidation until P4, rules-only signals with summary notices until P5.
- **Q3 = (a):** `models.summarize` = [Haiku, gemma4]; M8 measured on Haiku.
- **Q4 = V22 "hand-off"** (a new decision): the root never waits at a gate; the gated call is detached, the root gets the
  waiting status and its turn ends naturally; on approval the harness runs the call and the root reports in a fresh
  `continue` item (Behaviour). Children keep V7 (P3).
- **Deviations 1, 4 and 5 approved.**
- **Model IDs in measurement file names approved.** The owner's exception line for CLAUDE.md's "Hard rules" ("Exception:
  measurement records under `docs/measurements/` carry the model ID they measured, in the JSON and in the file name.")
  is the lead's edit to make; this contract's units do not touch CLAUDE.md.

## Units and build order

Each unit is one implementer call (≤ 3 edited files plus tests, briefed from a scratchpad file, worktree). U1–U7 only add;
U8a switches; U9–U10b delete. After each: tests green, `scan_personal.py --all`, review. (12 rows: the review's splits.)

| # | Unit | Files | Size | After | Parallel | Done when | Calls |
|---|---|---|---|---|---|---|---|
| U1 | Types and the index | `ledger.py`, `state.py`, `tests/test_invariants.py` (`test_no_new_ledger_type`; state ceiling 800 with a note until U10a) (+ test_ledger, test_state) | +~120 | — | U3, U4 | new types append; `blocks` verbatim; `approvals.seq`; `rebuild` restores results, clones, gates (with `reported`), context | 30 |
| U2 | The store | `results.py` (+ test_results) | ~200 | U1 | U4, U5 | render, read (range/query/path, JSON and text), seen-set refusal, 256 KB cap, filter on the way out | 25 |
| U3 | Neutral types, openai_compat + V21 | `llm/__init__.py`, `llm/openai_compat.py`, `llm/rename.py` (+ test_llm) | +~110 | — | U1, U2 | `Request`/`Response`; `to_wire` stable; the rename spec; `complete` on canned SSE; v1 `stream` kept until U9 | 30 |
| U4 | Anthropic on the SDK | `llm/anthropic.py`, `pyproject.toml` (+ test_llm_anthropic) | ~190 | U3 | U1, U2 | fake-client tests: blocks verbatim, 3 breakpoints, resident placement + fallback constant, thinking drop on model change, refusal | 30 |
| U5 | Router and config | `router.py`, `config.py` (+ test_router, test_config) | ~170 + ~25 | U4 | U6 | `test_fallback_chain`, semaphores, override check, `models` and `effort` validated; `Response.model` == the ref (tested) | 25 |
| U6 | The assembler and fold | `assemble.py`, `prompts/root.txt`, `prompts/fold.txt` (+ test_assemble) | ~330 | U1, U2, U4 | U5 | five pieces in budget, seen set, epoch strip, refused turns out, fold point/request/handle lines; its three invariant tests; `prompts/root.txt` teaches the waiting status — a handed-off call is "requested, not done", never reported as done until its report item arrives | 35 |
| U7 | The clone | `clone.py` (+ test_clone) | ~380 | U5, U6 | — | the turn table and the hand-off with a fake router and broker: waiting status, `decide` runs the detached call once, the report item (queued reports of one session merged into one turn, never with a say; records `user {gate: [N]}` for one report (list, tested)), interrupts, budgets, partial steps, steer (steers recorded by the clone at the step boundary; a say carries the item's turn id; a continue item's text is its own `user` row in the new turn), refusal, fold, restore (re-list, re-enqueue, outcome unknown); its three invariant tests; the clone checks `asyncio.current_task().cancelling()` after an `interrupted` Response and re-raises `CancelledError` unless the interrupt was its own (a flag it set) | 35 |
| U8a | The switch | `daemon.py`, `broker.py` (raw result), `tools.py` (`read_file` clip → store, 1 MB cap) (+ test_daemon, test_broker, test_tools) | daemon −250/+80 | U7 | — | rewritten daemon tests pass; approve by seq; the lane survives all-blocked (the memory pass sees unclipped results until U9); a home whose state.db predates P1 needs `pathfinder reindex` once (approvals.seq is not backfilled by catch_up); the live check starts from a fresh home | 35 |
| U8b | The CLI and the live check | `__main__.py` (`approvals` by seq; `approve` prints the report turn), `bench/v2/p1_runs.py` | ~+30, ~120 | U8a | R1, R2 (writing) | **the live check passes**, recorded for the lead | 25 |
| U9 | Delete the old turn engine | deletes `loop.py`, `jobs.py`, `llm/slot.py`, `senses/jobs.py`, old prompts; edits `memory.py` (pass, summarize), `rules.py` (judge stub), `daemon.py` (memory_mgr without a pass factory) (+ retired tests); `llm.stream` and `_post`-based stream leave; `api: anthropic` is served only by the router from U5 on | −~1,000 | U8b | — | nothing imports the deleted modules; suite green; retired tests listed | 30 |
| U10a | Dead state and keys | `state.py` (D52 record, v1 summaries, job rows; ceiling back to 780), `ledger.py` (TYPES removals), `config.py` (`model`, `worker_model`) (+ test_state, test_config) | −~150 | U9 | — | `test_no_new_ledger_type` final set; state ≤ 780 | 25 |
| U10b | Clips, emits, the ceilings | `tools.py` (`LEDGER_CLIP`, `clip_for_ledger`), `broker.py` (emits), `tests/test_invariants.py` | −~40, tests +~90 | U10a | — | the invariant-table tests pass; core ≤ 6,000; live check again | 30 |

R1 `bench/v2/m6_day.py` + `m6_day.json`, R2 `bench/v2/m8_fold.py`: researcher work, written after U8a, run after U10b on the lead's go-ahead.
