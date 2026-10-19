# v2 Partition 2 — the tool layer: four grades, an effect map, one policy table, search_tools, repair

**Where this stands (2026-10-19).**
- **Agreed:** blueprint v2 (V6, V7, V10, V17, V18), the owner's V20 (tool search = local embeddings through Ollama,
  FTS discarded), V22 (root gates hand off), D75, D76. Partition 1 is closed: `build` = 62f4f57, 715 tests, core
  4,351 / 6,000.
- **Measured for this contract (2026-10-19, local, read-only, no content copied):** the owner's 10-14 Notion session
  made **31 call ids: 30 tool calls and 1 install proposal** (26 ran, 5 were gated and never ran) and opened **20 approvals**: 19 Notion
  gates (`pages.archive` 12, `pages.get` 4, `pages.restore` 2, `config.status` 1) and 1 install proposal. **Only 5 of
  the 19 Notion gates were reads**; 14 were writes. A correct effect map takes the default mode from 19 to 14, not to
  ≤ 4 (Deviation 1, Q6). The fixture `bench/v2/fixtures/notion_tools.json` (the same server's real specs) declares an
  `action` enum on 8 of its 11 tools and `readOnlyHint: true` on 4 (users, workspace, content_convert, help); every
  action value the session used is in those enums. With every sense imported the native tool list is 21 tools,
  8,301 chars with `read_result` (P1's test pins ≤ 8,000 with fewer senses on).
- **The owner's own server has no map yet:** better-notion-mcp was installed before P2, so `tools.<id>` is absent and,
  by annotations alone, `pages` (readOnlyHint false, destructiveHint false) is a whole-tool write: `pages.get` would
  still ask. Under Q3 (b) `Attach` drafts the map at the server's first P2 load, the same path as for any new server
  (Behaviour, "The draft"); M3 measures it.
- **Decided (2026-10-19):** Q1–Q9 (Decisions); M5b approved; review round 1 folded in. **Next:** U1, then U0 ∥ U2a.

**"It runs" =** M3 passes: the owner's 10-14 session replayed offline through the real `policy.verdict` and the
drafted Notion map (the map `Attach` writes into the owner's home at its first P2 load) gates 0 of its 16 read calls (v1 gated 5)
and asks for every one of its 14 writes in the default mode, and opens ≤ 4 gates in `auto` (Q6); the live check passes: the root on Haiku finds a Notion tool with
`search_tools`, calls it through `call_tool` with no gate for a read and no fabricated ID, an archive is gated with the
page's **title** in the gate, an Ollama `call_tool` with an inner `properties` argument arrives intact, and with the embedding model unreachable the root still finishes a Notion read; the
§15-7 test passes (a new MCP server = one install approval + a drafted map, zero core changes); the M5 re-check passes on
the real registry. Model IDs live in config only; `$ROOT_MODEL`/`$EMBED_MODEL` are set by the operator.

### The live check (after U6; ~12 Haiku requests at xhigh ≈ $0.03, ~3 gemma4 requests, 0 credits for embeddings)

```
# 0. setup (WT = the partition-2 worktree; daemons are stopped by pid only, never pkill)
export WT="<worktree>" PATHFINDER_HOME=/tmp/pf2 ROOT_MODEL="<model from config>" FALLBACK_MODEL="<model from config>" EMBED_MODEL="<embed model from config>"
rm -rf /tmp/pf2 /tmp/pf2-venv && mkdir -m 700 /tmp/pf2 /tmp/pf2/check && python3 -m venv /tmp/pf2-venv
/tmp/pf2-venv/bin/pip install -q -e "$WT" && PF=/tmp/pf2-venv/bin/pathfinder CHK="python3 $WT/bench/v2/p2_runs.py"
$PF config set providers '{"anthropic": {"api": "anthropic", "base_url": "https://api.anthropic.com", "effort": "xhigh"}, "ollama": {"base_url": "http://localhost:11434/v1"}}'
$PF config set models "{\"root\": [\"anthropic:$ROOT_MODEL\"]}" && $PF config set embed_model "ollama:$EMBED_MODEL"
$PF config set mcp_servers.fake-notion "{\"transport\": \"stdio\", \"command\": [\"python3\", \"$WT/bench/v2/fake_notion_mcp.py\", \"/tmp/pf2/check/world.json\"]}"
# no `tools.fake-notion` is set by hand: Attach drafts and writes it at the server's first load (Q3 (b))
start() / stop()  as in the P1 contract, with /tmp/pf2
```
1. `start && $PF config get tools.fake-notion` → the draft (`pages.effect_by.action.get: read`, `archive: write`), one
   `config_set {key: tools}` in the ledger. `$PF say "In Notion, when was the page titled '휴가 계획' last edited?"` →
   `$CHK find /tmp/pf2`: a
   `search_tools` result naming `mcp__fake-notion__*` tools; ≥ 1 `call_tool` whose `result.tool` is
   `mcp__fake-notion__workspace` or `…__pages` (action `get`/`search`), `ok: true`; **no `gate`**; every page id in any
   call's arguments occurs in an earlier `result` body (fabricated IDs 0); the final text holds the world's time.
2. `$PF say "Archive that page."` → a `gate` with `effect: write` and a `title` naming `휴가 계획` (`page_id: 휴가 계획`); `$PF approvals` prints the
   title. `$PF approve N` → `result {gate: N}` `ok: true`, the world file marks the page archived, the root reports it.
   `$CHK gate /tmp/pf2 "휴가 계획"`.
3. `$PF config set embed_model ollama:no-such-model; $PF say "In Notion, search for pages about 휴가."` →
   `$CHK fallback /tmp/pf2`: the `search_tools` result says search is unavailable and lists tool names; a
   `call_tool` to `…__workspace` follows, `ok: true`; the turn ends `answer`; no `error` event from the turn.
4. `$PF config set models "{\"root\": [\"ollama:$FALLBACK_MODEL\"]}"; $PF say "In Notion, create a page titled 'P2 check'
   with the property Status = Done."` (gemma4 on Ollama, no Anthropic credit) → `$CHK ollama /tmp/pf2`: a `call_tool`
   to `…__pages` whose recorded `arguments` hold `properties` (M9's dropped name) and reach the fake server unchanged
   once approved (`$PF approve N`). This checks Q4's text-schema path under V21 (Deviation 12).
5. `stop; rm -rf /tmp/pf2 /tmp/pf2-venv`.

Rests on: V6, V17, V20, V22, D76; M5 (2026-10-17); M3's counts above. NOT in this partition: waiting clones, presets'
spawn override, gate expiry for children (3); self-notes on gate denials and tool errors (4); signal turns through the
root and standing tasks (5); the socket view messages and multi-id approve (6).

## Shape

No new dependency. Ceilings are §6 × 1.3 (P1's rule); targets are §6. The tool layer's §6 budget is 1,250 (registry 250
· policy 150 · results 300 · search 150 · broker+exec 400); now 898 (tools 261 · policy 58 · results 253 · broker 109 ·
exec 217), after P2 ~1,150.

| Module | §6 target / test | Now → after P2 | Imports (package) | Imported by |
|---|---|---|---|---|
| `tools.py` (the registry) | 250 / 325 (was 390) | 261 → ~280 (Deviation 11) | clock, exec | (NOT_YET: > 8) |
| `policy.py` | 150 / 195 | 58 → ~140 | config, exec | broker (and the M3 bench) |
| `broker.py` + `exec.py` | 400 / 520 (`broker+policy` row 455 → split) | 109 + 217 → ~135 + ~200 | policy, tools | clone, daemon |
| `results.py` | 300 / 390 | 253 → ~285 | tools | assemble, clone |
| `search.py` (new) | 150 / 195 | ~110 | config, llm, llm.openai_compat, tools | daemon (registration) |
| `clone.py` | 400 / 520 | 405 → ~455 | unchanged (the `Gate` carries the effect) | daemon |
| `state.py` | 600 / 780 | 582 → ~595 | clock | unchanged |
| `mcp/tools.py` · `pins.py` · `attach.py` (in `mcp/**` 1,858) | — | 137 → ~200 · 264 → ~270 · 107 → ~125; mcp/ 1,615 → ~1,705 | unchanged | unchanged |
| `installer.py` | 700 kept | 699 → ≤ 700 | unchanged (`mcp.tools.draft` replaces `read_only`) | unchanged |
| `config.py` | — (not core) | 280 → ~300 | clock | unchanged; holds `GRADES` (exec imports config, so the vocabulary cannot live in tools or policy) |

Core after P2 ≈ 4,680 (`search` joins the core list of `test_core_under_6000`). `search` imports
`openai_compat.embed`, so `test_single_assembler`'s named exemption grows from memory to memory + search (`embed` only).

Public interfaces (one line each; everything else private):

- `tools`: `ToolSpec` gains `effect: str = ""` (an explicit grade; "" = `write` if `write` else `read`); no `source`
  field: an MCP tool's server is its `mcp__<server>__` prefix (server ids hold no `_`, config.py:39). `write`, `network`,
  `sandboxed`, `reversible` stay as the attributes (Q2). `coerce` also accepts a JSON string for an `object`/`array`
  parameter. `CALL_TOOL` (neutral spec; the broker unwraps it, Q4).
- `config`: `GRADES = ("read", "write", "destructive", "external_send")`, used by `_check` for `tools`, by policy and
  by the draft.
- `policy`: `effect(spec, args, tools_cfg) -> tuple[str,
  dict | None]` (a grade or `"unknown"`, and `{arg, value}` when an `effect_by` lookup missed) · `MODES` (the V17
  table) · `verdict(spec, origin, args, cfg, session_grants) -> tuple[str, str, dict | None]` (decision, grade,
  unknown) · `DENIED_TEXT` (kept). v1's `decide` leaves in U7a.
- `broker`: `Broker(tools, origin, cfg, *, session_grants: Callable[[], set], control, approved=None, denied=frozenset())`
  · `Gate(id, name, arguments, effect, unknown)` · `chat_approval` (kept).
- `search`: `SEARCH_TOOLS` (neutral spec, registered into `tools.TOOLS` at import, like memory's) · `find(query: str,
  home: Path) -> str` (the inline result, ≤ 6,000 chars) · `text(spec) -> str` (what is embedded).
- `results`: `INLINE = {"read_result", "search_tools"}` (outputs shown in full, ≤ 6,000; the blueprint's
  "summary-renderer hint", Deviation 3) · `title(state, seen: set[int], args: dict) -> str` ("" when none).
- `state`: gate rows gain `title`, `recipient`, `scope` · `session_grants(session: str) -> set[str]`.
- `mcp.tools`: `grade_of(annotations) -> str` · `draft(definition) -> dict | None` (one tool's map entry) ·
  `draft_server(definitions) -> dict`. `mcp.pins`: `FIELDS` += `"annotations"`. `mcp.attach`: the one writer of a
  drafted map (U2b).
- `clone`: `decide(gate, decision, by, scope="", grade="") -> bool`.

## Data

**Ledger.** No new type (`test_no_new_ledger_type` unchanged). Extended fields:

| Type | Writer | New fields |
|---|---|---|
| `gate` | clone | `effect` ∈ read / write / destructive / external_send / unknown (from `policy.effect`; P1's U7 review note, the clone's duplicate mapping, is gone) · `title` (`"<arg>: <title>"` pairs from r:N, Q1) · `recipient` (external_send only; no tool produces it today) · `unknown: {arg, value}` when an `effect_by` lookup missed · `brief` = the item's text on one line, ≤ 120 chars |
| `resume` | clone | `scope` ∈ tool / server / session (Q5) · `grade` (the owner's answer for an unknown effect, Q3) |
| `result` | clone | `tool` = the real tool's name for a `call_tool` call (the `assistant` block keeps `call_tool`) |

`ORIGINS` (Q2 (a)): new appends use `user | signal | system`; `job` and `memory` lose their last writers in U7c
(memory.py's two `error` records become `system`); `schedule` joins when P5 writes it. Old events keep theirs: state's
projection still reads `job`/`memory` (line 218) for replays.

**state.db** (derived; `rebuild` replays): `gates` gains `title TEXT, recipient TEXT, scope TEXT` (ALTER
TABLE, the existing pattern). `session_grants(s)` = the tools of gates in session `s` approved with `scope =
'session'`. No tool index in state.db (Q4: the index is derived from the registry, not the ledger).

**Config** (through `config.set()` only):

```json
"approval_mode": "manual",
"tools": {"better-notion-mcp": {
  "workspace": {"effect": "read"},
  "pages": {"effect_by": {"action": {"get": "read", "get_property": "read", "create": "write", "update": "write",
                                     "move": "write", "archive": "write", "restore": "write", "duplicate": "write"}}}}},
"grants": {"mcp__better-notion-mcp__pages": "allow", "delete_reminder": "deny"}
```

`approval_mode` ∈ `read-only | manual | auto` (no `full-auto` in P2: Q5). `tools.<server>.
<tool>` holds `effect` (the per-server override, first in V6's order) or `effect_by.<arg>.<value>`; values are
`config.GRADES`; `config set tools.<server> JSON` sets one server (D75's KEY.SUB). `grants` keys gain the server form
`mcp__<server>__*`; values unchanged. `embed_model` is read by `search` (and memory, as now). Writers of `tools`:
`Attach` (a drafted server, once, Q3 (b)), `decide --grade` (one value), the owner (`config set`), and uninstall (drops
`tools.<id>` where D31's grant removal was). Install no longer writes grants; existing D31 grants stay valid and harmless
(they name read tools).

## Behaviour

**The policy table** (`policy.verdict`, Q2 (a) + Q5 (a)). Grade first, then the origin's row, then grants, then the
floors. A cell is the decision; "→" marks what an attribute or a grant may change.

| Row | read | write | destructive | external_send | unknown |
|---|---|---|---|---|---|
| user · `read-only` | allow | deny | deny | deny | ask |
| user · `manual` (default) | allow | ask → allow if `sandboxed` or `reversible`, or granted | ask | ask | ask |
| user · `auto` | allow | allow | ask | ask | ask |
| signal | allow (ask if `network`) | ask | ask | ask | ask |
| system (report turns, notes) | allow (deny if `network`) | deny | deny | deny | deny |

- **Grades of today's natives:** read = every tool without `write` (incl. mail reads, memory reads, `search_registry`
  and `propose_install` with `network`); write = `write_file`, `add_event`, `add_reminder`, `complete_reminder`
  (`add_schedule`: Q9); write + `reversible` = `remove_schedule`, `edit_memory`; write + `sandboxed` = `run_command`;
  destructive = `delete_reminder` (`effect="destructive"`, no undo) and `run_command` with `escalate: true` or no
  sandbox on the machine (v1's "left the box": never grantable, now the floor). Under Q9 (a) `add_schedule` is
  graded external_send; no other native sends (mail is read-only, D59).
- **Grants** (user origin only), from three scopes at once: the tool name, `mcp__<server>__*`, and the session's
  "always allow" set. A `deny` in any scope wins; else an `allow` in any scope lifts a `write` (and only a write) to
  allow; else an `ask` in any scope tightens an allow to ask; else the row. Nothing lifts destructive, external_send
  or unknown (§8, V17's floor; v1 let a grant allow anything but an escalated shell). A value outside allow/ask/deny
  is ignored (config refuses it too).
- **`effect(spec, args, tools_cfg)`**, V6's order: native → `spec.effect` or `write`/`read` (escalate rule above). MCP →
  `tools.<server>.<tool>.effect` → `effect_by.<arg>[args[arg]]` (arg absent or value not in the map → `unknown` with
  `{arg, value}`; a miss never falls through to annotations) → `spec.effect` set at load from annotations
  (`grade_of`: `readOnlyHint: true` → read; `destructiveHint: false` → write; `destructiveHint` true or absent →
  destructive; no annotations → unknown). `openWorldHint` is not a send. Unknown asks in every mode (V17).
- **Annotations are pinned** (U2b): they now decide policy for a tool the map does not cover, so `pins.FIELDS` gains
  `annotations` (pins.py's docstring said they were left out because nothing read them; now policy does). A changed
  `readOnlyHint` holds the tool like a changed description. A stored pin with no annotations digest (every pin before
  P2) takes the current one once, silently — a field added to the baseline, not a change — so the owner's Notion
  server is not held on the first P2 start.

**The door** (`Broker.call`, one call; the clone records every outcome as a `result` or a `gate`):

| # | Check | Outcome |
|---|---|---|
| 1 | (in `Clone._batch`, before the `read_result` branch) `call_tool {name, arguments}` → the real name and arguments (Q4 (b)), so `call_tool {name: "read_result"}` reaches the store; a nested `call_tool` is refused | — |
| 2 | Unknown name | refused: `no such tool: X` (+ the sense-off note, D74) `; nearest: a, b, c` (`difflib`, ≤ 3 names over the whole registry, MCP included) |
| 3 | Origin outside `spec.origins` (D76) | refused, as now |
| 4 | `coerce` (scalars as now; a JSON string for object/array is parsed), `normalize` | refused with the reason |
| 5 | Approved / denied for this exact call (single use, as now) | run / `USER_DENIED_TEXT` |
| 6 | `policy.verdict` | allow → run · deny → `DENIED_TEXT` · ask → `Gate(id, name, args, effect, unknown)` |
| 7 | Handler | `ok` + raw result, or `ok: false` + the error (an MCP `isError` is already `ok: false`, inline) |

**The batch** (`Clone._batch`, Q7 (a)): calls run in order. The first refusal (rows 2–4, a policy deny, a user denial)
or the first error skips the rest of the step's calls (`SKIPPED`, reworded "skipped after a refusal or an error in this
batch"). A gate is not a refusal: its call is handed off (V22) and the batch goes on (a later write gates too). The turn
stops its tool use when one tool fails `FAILS_TO_STOP` (3) times in a row (P1, kept) or when the turn's third
unknown-name refusal arrives (the nearest-name cap, counted as one pseudo-tool `?unknown`).

**search_tools and call_tool** (Q4 (b)):

| Moment | What happens |
|---|---|
| The list | `read_result` first, then every non-MCP spec of `tools.TOOLS` sorted by name (`search_tools` and `call_tool` among them; assemble.py:130's order, unchanged); one snapshot per turn, the same for every origin (D76). MCP tools are never in the list. |
| `search_tools {query}` | `search.find`: embeds the query with `embed_model` (one local call, ~13 ms in M5) and ranks every registry tool by cosine over `text(spec)` = name + description + parameter names + string enum values. Output (inline, `results.INLINE`): the top 3 as full neutral specs while the total stays ≤ 6,000 chars, the rest as name + first description line; an exact tool name as the query returns that spec. |
| The index | in memory: `{sha256(text): vector}` per embed model, filled lazily at the first search after a registry change (one batched `embed` call for the changed texts). Nothing to replay: it is derived from the registry. |
| Ollama down, `embed_model` "", or an embed error | the output says "search is unavailable (reason)" and lists every searchable tool's name + first line (≤ 6,000 chars); listed tools work as always. The error is not an `error` event (a tool result says it). |
| `call_tool {name, arguments}` | unwrapped by the clone (row 1): the real spec's coerce, normalize, effect, policy, gate and handler; the `result` names the real tool; a gate shows the real tool. The inner arguments are not renamed (Deviation 12). |

**Gates** (Q1 (a)): at `Gate`, the clone records `gate` with `effect`, `unknown`, `title = results.title(state,
seen, args)`, `recipient` and the one-line brief. `title`: every top-level argument value, and every element of a list
value, that matches `results._ID` (results.py:14) is looked up against the `id` of items in the seen results' bodies;
each match becomes `<arg>: <title>` (`results._title`'s rule), arguments whose name does not start with `parent` first,
at most 3, joined by `; ` ("" if none) — so `page_id` beats `parent_id`, and `page_ids: [a, b]` shows both.
`recipient` (external_send only): the first of `to, recipient, recipients, cc, email, channel` present, on one line;
no tool produces it today (no native sends; the list does not grow until one does).
`approvals` and the gate screen message show `effect` and `title`. No waiting machinery and no timeout here (Q1).

**Decide** (`Clone.decide(gate, decision, by, scope, grade)`; the CLI `pathfinder approve N [--always
tool|server|session] [--grade G]`):

| Input | Effect |
|---|---|
| `approve N` | as P1: `resume {approved}`, the call runs once, the report item |
| `--always tool` / `server` | also `config.set("grants", {..., <tool> or mcp__<server>__*: "allow"})` |
| `--always session` | `resume {scope: session}` only; `state.session_grants` derives it; the broker reads it per call |
| any `--always` | refused (one line, nothing recorded, the gate stays open) for a destructive, external_send or unknown gate; `server` also for a native tool |
| `--grade G` (unknown gates only) | `resume {grade: G}`, then the map entry is written: `tools.<server>.<tool>.effect_by.<arg>.<value> = G`, or `.effect = G` when the tool had no map and no annotations, through `config.set("tools", …)`; the call runs once. When the miss has no string value to key on (the argument was absent, or its value is not a string), nothing is written to the map: `resume {grade}` is recorded and the call runs once (lead decision, U5) |

**The draft** (`mcp_tools.draft(definition)`; `draft_server` maps it over a server's tools; Q3 (b)): a tool with
`readOnlyHint: true` → `{"effect": "read"}`; else the first string-enum parameter whose values match the verb table →
`{"effect_by": {arg: {value: grade}}}` for the matching values (a value matching nothing is left out: it asks); else
`{"effect": grade_of(annotations)}` when that is decisive; else nothing. A value's grade is the strictest over all its
`_`-separated words found in a list (destructive > external_send > write > read; `get_and_delete` is destructive):
read (`get list query search info retrieve read status children me fetch find count`), write
(`create update append move duplicate set add edit rename archive restore complete upload`), destructive (`delete remove
destroy purge drop reset clear trash`), external_send (`send post publish share invite reply forward notify`). The
fixture's draft: pages get/get_property read, archive/restore write; databases delete_page destructive; config
status/setup_status read, setup_reset/cache_clear destructive, setup_start unknown; file_uploads send external_send
(the owner may correct it); users, workspace, content_convert, help read. For the fixture (counted with this rule,
2026-10-19): 4 read tools, 6 mixed tools whose 38 action values draft as 13 read, 19 write, 4 destructive, 1
external_send and 1 unknown (`config.setup_start`), and 1 whole-tool write by annotation (`config__open_relay`).

Where it is seen and written (one writer, two moments):

| Moment | What happens |
|---|---|
| Install approval (installer trial) | `draft_server` over the trial's tools is **displayed**: every value that is not read by name, unmapped ones as `asks` (26 for this server, `tool.value: grade`), not counts only. Nothing is written. |
| First load (`Attach.register`, U2b) | for each server whose `tools.<server>` is absent, `draft_server` over its definitions is written once, at the first load where none of the server's tools is held (a partly held server is drafted after the owner accepts the hold, never from the passed tools alone: a held tool must not end up on annotations alone) through `config.set("tools", …)` (the ledger's `config_set` records it). Installed and hand-configured servers alike; the owner's pre-P2 Notion server takes this path at its first P2 load — there is no separate backfill. An existing entry is never overwritten (the owner's edits and `--grade` answers stay). |
| Later | a tool added to a known server (held, then accepted) has no map entry: annotations + ask, answered by `--grade`. |

**Thinking epoch on a tool change** (Q8 (a)): the clone keeps the hash of the last tool list it sent per session;
a different hash moves `View.epoch` to the ledger's last seq before the request is assembled (thinking up to it is
stripped, as after a fold; one cache rewrite). With Q4 (b) this fires only on a skill write (`read_skill`'s description)
or a code change; an MCP install or unload no longer touches the list.

## Invariants and tests

| Test | Unit, file | Asserts |
|---|---|---|
| `test_effect_table` | U1 test_policy (U0 adds two rows) | every native in the registry (all senses imported) has the grade the Behaviour list names — `delete_reminder` destructive, `add_schedule` external_send "until P5's schedule origin" (Q9); the read-only / manual / auto × origin × grade matrix (no full-auto row); `sandboxed`/`reversible` lift write only in user · manual; escalate and no-sandbox are destructive |
| `test_unknown_effect_asks` | U1 test_policy | an MCP tool with no map and no annotations, and an `effect_by` miss (value absent, arg absent; annotations saying read do not rescue it), ask in `read-only`, `manual` and `auto`; a user grant of allow does not lift it |
| `test_grants_never_lift_destructive` | U1 test_policy | tool, server and session allows on destructive/external_send/unknown leave ask; a deny in any scope beats an allow in another; a server `ask` does not block a session allow on a write |
| `test_batch_stops_at_refusal` | U5 test_clone | a step of 4 calls: refusal at call 2 → calls 3–4 `SKIPPED`, never run; the same with an error at call 2; a gate at call 2 → call 3 (a read) runs, call 4 (a write) gates |
| `test_new_server_needs_no_core_change` (§15-7) | U2b test_installer | a fake server (annotations + an `op` enum, a fake registry and trial session) through `Installer.check` → one approval whose text lists its non-read values → `install` writes no `tools.<id>` → after `Attach.register` (its first load) `tools.<id>` equals `draft_server` and `policy.effect` gives the drafted grades; no source file under `pathfinder/` names the server or its verbs (grep in the test) |
| `test_search_falls_back_without_embeddings` | U3 test_search | `embed_model` "" and an embed that raises → the name list, ≤ 6,000 chars, no exception; with a fake embed the nearest spec comes first and only changed texts are re-embedded |
| `test_call_tool_goes_through_the_door` | U5 test_broker | `call_tool` reaches the real spec's coerce, policy and gate; the `Gate` names the real tool; a nested `call_tool` and an unknown inner name are refused with ≤ 3 suggestions |
| `test_gate_carries_title_and_effect` | U5 test_clone | a gate on `pages.archive {page_id}` after a search result holding that id records `title` and `effect: write`; no seen match → `title: ""` |
| `test_unknown_answer_written_to_map` | U5 test_clone | `decide(N, approved, grade="read")` on an unknown gate writes `tools.<server>.<tool>.effect_by.action.<value>`, records `resume {grade}`, runs the call once; a second call with that value is allowed without a gate |
| `test_tool_list_change_advances_epoch` | U5 test_clone | a skill write between two turns → the second request has no thinking from before it; with no change, the clone's next request replays the thinking byte for byte (asserted here on the clone's requests; P1's `test_append_only_history` covers the adapter only) |
| `test_first_load_drafts_once_and_pins_annotations` | U2b test_mcp_pins | a server without a map — installed or hand-configured — gets `draft_server` once (one `config_set`); a second load and an existing entry are left alone; held tools are not drafted; a flipped `readOnlyHint` holds the tool; a pre-P2 pin without an annotations digest adopts it without a hold |
| `test_single_assembler` | U3 test_invariants | memory + search the named `openai_compat.embed` exemption (else U3 fails the suite) |
| `test_module_ceilings`, `test_core_under_6000`, `test_coupling_ceilings` | U7b test_invariants | rows from the Shape table; `search` in the core list; `search` and `policy` under the coupling ceilings |

Also: U1 `test_coerce_parses_json_for_object_and_array`; U2a `test_draft_of_the_notion_fixture` (the counts above),
`test_grade_of_annotations`; U4 `test_inline_outputs_render_in_full`, `test_title_from_seen_results`,
`test_session_grants_survive_rebuild`; U6 `test_approve_flags` (`--always`, `--grade`, the refusals).

**Retired or rewritten** with the unit that changes what they pin:
- U2a: the installer's D31 grant assertions (`permissions`, the `grants` install/rollback/uninstall steps) → the
  displayed draft (install), nothing (rollback), dropping `tools.<id>` (uninstall).
- U5 (the `Broker` signature: `cfg` and `session_grants` replace `grants`): test_broker.py (all `Broker(` builders,
  `test_broker_unknown_deny_ask_allow` on `verdict`), test_skills.py:101–128 and :161, test_memory.py:149,
  test_calendar.py:47 and :54, test_installer.py:201, test_mcp_tools.py:145, test_daemon.py:1562 — each passes a cfg
  `{"grants": …}` instead of the bare grants.
- U7a (v1 `decide` goes): test_policy.py's `test_policy_table`, `test_escalate_forces_write`,
  `test_missing_sandbox_forces_write`, `test_grants_win_for_user_only`,
  `test_grant_never_covers_leaving_the_sandbox_and_typos_fail_closed`, `test_grants_can_also_tighten` (:61),
  `test_policy_reversible`, `test_add_schedule_asks_in_a_user_turn` (:81), `test_memory_origin_row`;
  test_calendar.py:293; test_mcp_tools.py:113–119 — each case becomes a `verdict` row of `test_effect_table` or
  `test_grants_never_lift_destructive`, except the `memory` and `job` rows (gone).
- U7c (closed origins): test_memory.py:158, 207, 241, 318 (the consolidation pass's `memory_pass` control, dead since U9).
- U7b: exec's output-clip tests.

## Discarded here, and what is only stubbed

Discarded: v1's category table (`_TABLE`, `_category`, `decide`); the `job` and `memory` origins for new events; D31's
install-time `readOnlyHint` grant step (§12) → the map `Attach` drafts; `mcp/tools.py`'s `write=True` on every MCP tool (`network`
stays: an attribute); exec's 8,000-char output clip (the store caps at 256 KB and renders a summary; Deviation 8);
`clone._batch`'s own effect mapping (U7 review note: one place, `policy.effect`); the second-error rule of the batch.

Stubbed: waiting clones, gate expiry and M2's in-place resume (3, with children); **full-auto: the P3 contract must put
V17 ("allow everything", clone-only) against §11 (destructive and external_send always ask) to the owner as a
question, together with the spawn override — the owner asked that this not be forgotten (2026-10-19)**; until then it is
in neither `MODES` nor config; the night clone's map proposals from gate history (4); `schedule` origin (5); multi-id
approve and `list_approvals` (6). Not built (Deviation 3): `parallel_safe` and `source` on ToolSpec, a `gates.grade`
column (no reader: the answer lives on `resume.grade` and in the map).

## Measurements

**M3 — the effect map cuts the Notion gates** (offline, 0 model requests; run after U5, written after U2b). `bench/v2/
m3_gates.py --ledger <owner home>/events --session <id> [--grants <owner home>/config.json]` reads v1 events only, in
place (m8_fold.py's pattern; nothing copied into the repository). Calls = `tool_call` ∪ `approval_pending`, deduped by
call id (31 expected). Each call goes through the real `policy.verdict` with: natives from the registry (v1
`search_tools` → `search_registry`); Notion specs built by `mcp/tools.py` from `bench/v2/fixtures/notion_tools.json`
under the session's server id. A v1 approval that is not a tool call (`install_server`) is counted apart. Map arms:
(1) **no map** — the owner's home as it stands before P2's first start (annotations only; expected 19: the motive for
the first-load draft); (2) **drafted** — `draft_server` over the fixture, i.e. what `Attach` writes into the home at
its first P2 load; (3) **home** — the
`tools` entry read from `<owner home>/config.json`, re-run after P2's first daemon start there and required to equal (2)
(if the live server's definitions differ from the fixture, its pins hold them and (3) says so). Policy arms for each:
v1 (the ledger's own approvals), `manual` without grants, `manual` with the owner's grants (names only), `auto`.
Output `docs/measurements/v2-m3-<date>.json`: per (tool, action
verb, grade, decision) counts — no arguments, no ids, no content; `scan_personal.py` before commit.
Expected from this contract's count: 16 read, 14 write, 1 install; v1 19 tool gates; `manual` 14; `auto` 0.
Pass (Q6 (b)), on arm (2) now and arm (3) after deploy: `manual` gates 0 read calls and asks for all 14 writes, 0 calls
graded unknown; `auto` ≤ 4.
Fail → the map or the verb table is wrong (fix and re-run, free); a design-level fail (reads still gated) → V6's
fallback "annotations + ask only".

**M5 re-check — search on the real registry** (local, 0 credits; after U3). The 20 pre-registered M5 queries through
the product's `search.find` (the real `text(spec)` over every native and the fixture's Notion tools), expected names
mapped v1 → v2 (a query whose tool no longer exists is dropped and reported). Pass: top-3 ≥ 90% of the remainder (M5:
18/20). Truncation probe (M5's open question): the longest `text(spec)` (~3,000 chars now) embedded whole and cut in
half; cosine < 0.999 between them → not truncated. Output `docs/measurements/v2-m5-registry-<date>.json`.

**M5b (approved 2026-10-19; between U4 and U5) — the proxy call.** The one assumption P2's tool exposure rests
on and nothing has measured: Haiku calls a tool well when its schema arrives as text in a `search_tools` result and the
call goes through `call_tool`. M1's nine tasks on fake_notion with the Notion tools behind `search_tools`/`call_tool`
(store arm otherwise unchanged): ~45 requests ≈ $0.03, plus 3 gemma4 runs of the one task whose call carries an inner
`properties` argument (Deviation 12). Pass: fabricated IDs 0 and passes ≥ M1 store arm's (5/9 by the pre-registered
scorers), first-call argument errors ≤ 2, the inner `properties` arrives on all 3. Fail → back to the owner with
Q4's rejected option (found tools join the list, the epoch advances).

## Deviations from the blueprint

1. **M3's "19 → ≤ 4" rests on a miscount.** The blueprint read the 19 Notion gates as reads ("pages reads asked");
   the ledger says 5 reads and 14 writes. No honest grading of archive and restore gets the default mode under 14;
   ≤ 4 holds only in `auto` (expected 0). Q6 sets the pass line; P1's reality (the ledger) is taken.
2. **"0 fabricated IDs" is not an offline number.** A replay sends v1's own calls (which held the 10-14 fabrications)
   through the door; a door cannot change an argument's truth. It moves to the live check (and M5b), beside M1's 0 in
   18 Haiku runs.
3. **ToolSpec gains `effect` only.** The blueprint's "parallel-safe" has no reader (the batch runs in order); "source"
   is the `mcp__<server>__` prefix already (P3's presets can read it there); the "summary-renderer hint" becomes
   `results.INLINE`, a name set with one reader (assemble), not a field.
4. **Piece 2 is ~9,100 chars with every sense on** (8,301 measured now + `search_tools` + `call_tool`), over §7-1's
   8,000, if natives stay listed (Q4 (b)): the root is the full assistant, and each search costs a step. MCP tools,
   the cause of the 24k blueprint figure, leave the list entirely.
5. **FTS is gone from tool search** (V20 supersedes §7-1/§8's FTS5): embeddings only, the name list as the fallback.
6. **v1's `search_tools` (the MCP registry search in `senses/catalog.py`) becomes `search_registry`**, freeing the
   blueprint's name for the core tool (U0; the lead's call — or the core tool takes another name and U0 goes).
7. **`effect_by` keys one argument's value.** Notion's `pages.update {archived: true}` archives through the `update`
   action, so a map can grade archive only as strictly as update: archive = write (Notion's trash restores it). A
   two-argument rule is not built.
8. **exec's output clip goes now**, where P1 wrote "stays until P2"; it does: the store cut (256 KB) and the summary
   render make it redundant, and it cut what `read_result` could have served.
9. **Gate title and recipient are built here**, where P1's stubbed list put them in partition 3: the 10-14 approvals
   showed only UUIDs, and this partition's live check and M3 replay that session (criterion 10).
10. **mcp/ is 1,615 lines, not 1,858**: 1,858 is §6's allocation; the "port" already runs since P1 (Attach registers
    into `tools.TOOLS`), so P2 changes only `_to_spec`, the draft and the trial.
11. **`tools.py` ends ~280 lines, over §6's 250 registry target** (under the 325 test ceiling): it was 261 before P2;
    its text helpers (`clean`, `oneline`, `visible`, `map_strings`) are not registry and can leave in a later split.
12. **`call_tool`'s inner arguments are not renamed (V21).** The rename exists because Ollama drops `properties` /
    `description` from a tool *schema* it is given (M9); a text schema in a search result never passes through that
    path, and M9 saw calls still send `properties`. Live-check step 4 and M5b verify it on gemma4; if the name is
    dropped, `llm/rename.py` renames inside `call_tool.arguments` by the inner tool's schema (one call site).
13. **Annotations join the pin hash** (`mcp/pins.py`), against its docstring's reasoning, which no longer holds: they
    decide policy for any tool the map leaves out.

## Decisions (2026-10-19)

- **Q1 = (a):** the V17 gate fields now (effect, `arg: title` from r:N, recipient, one-line brief, `unknown`); no
  waiting machinery and no gate timeout in P2 — both come with the children in P3.
- **Q2 = (a):** four grades; `sandboxed`, `network`, `reversible` stay as ToolSpec attributes, so `manual` behaves as
  v1 did; escalate / no sandbox and `delete_reminder` are destructive; origins user / signal / system.
- **Q3 = (b), archive = write:** `Attach` drafts `tools.<server>` at a server's first load whenever it is absent —
  installed and hand-configured servers alike, and the owner's already-installed Notion server at its first P2 load —
  and writes it once through `config.set`; the installer's trial only displays the draft (every non-read value listed
  in the install approval). One writer, two moments it is seen. **Accepted cost:** a hand-configured server's draft is
  written without review; the owner corrects it with `config set tools.<server>` or `approve N --grade G`.
- **Q4 = (b):** a found tool is called through `call_tool`, its schema delivered as text in the search result; the
  list never changes. **M5b approved** (between U4 and U5).
- **Q5 = (a) with (i):** `approval_mode` read-only / manual / auto and the tool / server / session "always allow"
  scopes, with the CLI flags; `full-auto` stays out of `MODES` and config until P3 (Stubbed).
- **Q6 = (b):** M3 passes when `manual` gates 0 of 16 reads and asks for all 14 writes with 0 unknown, and `auto`
  opens ≤ 4; v1's 19 reported beside.
- **Q7 = (a):** a batch stops at the first refusal or the first error; a gate does not stop it; ≤ 3 nearest names;
  the third unknown-name refusal in a turn stops tool use.
- **Q8 = (a):** the clone hashes the tool list it sends; a change advances the thinking epoch (one cache rewrite).
- **Q9 = (a):** `add_schedule` is graded external_send until P5's `schedule` origin re-decides it: it asks in every
  mode and no grant lifts it (v1 allowed a grant; accepted).

## Units and build order

Each unit is one implementer call (≤ 3 edited files plus tests, briefed from a scratchpad file, worktree). U1–U4 only
add (U0 renames and grades two natives; U2b writes drafted maps and pins one more field); U5 switches; U6 wires the CLI
and the daemon; U7a–U7c delete. After each: tests green, `scan_personal.py --all`, review. Written for the Decisions.
Order: U1 → (U0 ∥ U2a) → (U2b ∥ U3 ∥ U4) → U5 → U6 → U7a ∥ U7b ∥ U7c.

| # | Unit | Files | Size | After | Parallel | Done when | Calls |
|---|---|---|---|---|---|---|---|
| U1 | Grades and the table | `tools.py` (`effect`, CALL_TOOL spec, object/array coerce), `policy.py` (`effect`, `MODES` without full-auto, `verdict` beside v1 `decide`, the grant order), `config.py` (`GRADES`, `approval_mode`, `tools` validated) (+ test_policy, test_tools, test_config) | +~150 | — | — | `test_effect_table` (every native but `delete_reminder` and `add_schedule`, which land in U0), `test_unknown_effect_asks`, `test_grants_never_lift_destructive`; v1 `decide` untouched, its tests green | 30 |
| U0 | Free the name, grade two natives | `senses/catalog.py` (`search_tools` → `search_registry`), `senses/reminders.py` (`delete_reminder` destructive), `senses/schedule.py` (`add_schedule` external_send until P5's schedule origin, Q9) (+ test_catalog, test_daemon, their two `test_effect_table` rows) | ±0 | U1 | U2a | no `search_tools` left in `pathfinder/` but `catalog.py`'s docstring (U3) and the installer's hint (U2a); suite green | 15 |
| U2a | The draft, displayed | `mcp/tools.py` (`grade_of`, `draft`, `draft_server`, `effect` from annotations; `read_only` goes; `write=True` kept until U7b), `installer.py` (the trial displays the draft — every non-read value in the approval; the D31 grant step goes; uninstall drops `tools.<id>`; hint "try search_registry"; ≤ 700) (+ test_mcp_tools, test_installer) | +~60 | U1 | U0 | `test_draft_of_the_notion_fixture` (strictest-word rule); install writes neither grants nor `tools`; installer ≤ 700 | 30 |
| U2b | The one writer, pinned annotations | `mcp/attach.py` (first load: draft and write an absent `tools.<server>` once), `mcp/pins.py` (`FIELDS` += annotations; a pin without that digest adopts it once) (+ test_mcp_pins, test_mcp_servers, test_installer) | +~30 | U2a | U3, U4 | `test_first_load_drafts_once_and_pins_annotations`, `test_new_server_needs_no_core_change`; existing pin tests green | 25 |
| U3 | search_tools | `search.py`, `catalog.py` (docstring: `search_registry`), `tests/test_invariants.py` (the `embed` exemption for search) (+ test_search) | ~110 | U0, U1 | U2b, U4 | `test_search_falls_back_without_embeddings`, `test_single_assembler`; ≤ 6,000 chars; the M5 re-check run (local) | 25 |
| U4 | Inline, titles, scopes | `results.py` (`INLINE`, `title` by `_ID`, lists, `parent_*` last), `assemble.py` (line 59 reads `INLINE`), `state.py` (gate columns `title`, `recipient`, `scope`; `session_grants`) (+ test_results, test_assemble, test_state) | +~55 | U0 (else `INLINE` shows v1's registry search uncapped) | U2b, U3 | its three tests; `rebuild` restores the new columns | 25 |
| U5 | The switch | `broker.py` (`Broker(tools, origin, cfg, session_grants=…)`, `verdict`, nearest names, `Gate.effect/unknown`), `clone.py` (the only `Broker` builder, :179 and :345; `call_tool` unwrapped before the `read_result` branch; list = core + non-MCP; gate fields; batch rule; unknown-name cap; epoch on list change; `decide(scope, grade)` with the map and grant writes) (+ test_broker, test_clone, and the Retired U5 list) | +~75 | U1–U4 | — | the five U5 tests; every `Broker(` test builder rewritten (Retired, U5) and green | 35 |
| U6 | The CLI, the daemon, the live check | `__main__.py` (`approve --always/--grade` and their refusals; `approvals` shows effect + title), `daemon.py` (`import search`; socket `approve` passes `scope`/`grade`), `bench/v2/p2_runs.py` (+ test_main, test_daemon) | +~45, ~150 | U5, R2 | R1 | **the live check passes**, recorded for the lead | 30 |
| U7a | Delete the v1 table | `policy.py` (`_TABLE`, `_category`, `decide`), `tools.py` (default `origins` without `job`), `skills.py` (`read_skill` origins `{user}`) (+ the Retired U7a tests) | −~45 | U6 | U7b, U7c | nothing calls `decide`; suite green | 20 |
| U7c | Close the origins | `ledger.py` (`ORIGINS` = user, signal, system), `memory.py` (the dead `memory_pass` branch of `_edit` and its `origin = "memory"`; `edit_memory` origins `{user}`; the two `error` records → system) (+ test_memory's four `memory_pass` tests, test_ledger) | −~20 | U6 | U7a, U7b | appending origin `job`/`memory` raises; no writer names them (grep); rebuild of an old ledger still works | 20 |
| U7b | Clip, flags, ceilings | `exec.py` (output clip), `mcp/tools.py` (`write=True`), `tests/test_invariants.py` (Shape rows, `search` in the core list) (+ test_exec) | −~25, tests +~15 | U6 | U7a, U7c | the invariant-table tests pass; core ≤ 6,000; live-check step 1 again (~4 requests) | 25 |

R1 `bench/v2/m3_gates.py` (researcher; written after U2b, run after U5, arm (3) again after the first P2 start in the
owner's home — free; the lead's go-ahead only for the commit). R2 `bench/v2/fake_notion_mcp.py` (a stdio MCP server over
`fake_notion.World` serving the fixture's specs, modelled on `tests/mcp_fake_server.py`, ≤ 150 lines; before U6). M5b
(approved) runs between U4 and U5.
