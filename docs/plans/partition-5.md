# Partition 5 — memory: the vault, the hybrid index, the consolidation pass, the gate

**"It runs" =** (1) m4 is in HISTORY: on the transplanted Mark.5 retrieval corpus (8 Korean wiki
pages, 4 dated notes as pages, 17 positive queries) hybrid `state.search` reaches recall@3 >= 0.80
and beats FTS-only by >= 5 points, or D10's reversal turns the vector default off; (2) live, three
compressed "days": a synthetic conversation is fed through `say` (about ten turns), `pathfinder
memory run` forces a pass, and `wiki/` gains at least one page; the second day's conversation
changes a fact and the pass `replace`s the line rather than rewriting the page; a fact the user
stated in their own words shows up in `pathfinder approvals` as an `edit_memory` proposal for
`USER.md`, `approve` applies it and the next turn's resident context carries it; (3) live: a
question whose answer is in a wiki page makes the model call `search_memory` then `read_memory`
and answer from the page; (4) live: a session of about twelve turns, then a forced pass, then a
question about something said in the first turn is answered from the session summary (the ledger
shows the next `turn_start` was built from `session_summary` + the last four messages, not the
whole history); (5) live: seeded `judgment` events of one kind with the same outcome and a 👍
reaction make the pass propose a `RULES.md` row; `approve` applies it, the rules reload without a
restart, and the next signal of that kind is decided by the row (no `judgment` event); (6)
`state.db` deleted and `reindex` restores the memory cursor, the session summaries, the pending
proposals and the page index; (7) offline: three local days of passes with lines read and nothing
applied produce one `memory_stalled` signal that speaks.

Decisions this partition rests on: D8 (one step ahead), D10 (hybrid index; FTS-only fallback),
D13 (fenced judgment is a *rule-discovery device*: repeated 👍 judgments become RULES.md proposals),
D16 (an approval is a ledger row; `approve`/`deny` are the only approval commands), D22
(`reversible` writes run by themselves for the user), and the three this partition adds (D27–D29,
below). Open paths this partition closes (HISTORY): in-session compaction, `RULES.md` reload, rule
proposals from repeated judgments, `USER.md` fill.

NOT in this partition: `SOUL.md` (a persona file is a settings slot, not a capability - `system.txt`
is the persona until someone asks to change it at runtime); wiki lint, a procedures folder and a
wikilink graph (blueprint §17: after 100 pages); a per-turn auto-injected retrieval block
(Mark.5's `index.block`; the model calls `search_memory` when it needs it - the wiki map is
resident, and m6 priced retrieval blocks as the item that pushes the day toward x1.6); `notes/`
and `skills/` kinds; a git history for the vault (undo is the inverse op from the ledger); the
catalog (6), the TUI (7).

Line budgets (blueprint §6): `memory.py` 600 (new); `state.py` 600 (301 today, +~160 for the page
index, the summaries and the memory counters); `daemon.py` 642/600 already (+~20 here: the approval
route, the rules reload, the manager's start/close, two lines in the per-turn context - ending
near 660, inside the 1.3x line of 780, reported at the end as §6 requires); `llm/openai_compat.py`
+~25 (`embed`, adapter budget); `senses/memory.py` <= 150; `policy.py` +1 row; `ledger.py` +3 types
+1 origin; `rules.py` +3 frontmatter fields. Expected: about 650 core lines plus one bench, one
prompt file, the transplanted corpus and tests.

## Entrance measurement

### m4 — does the hybrid index earn its vectors? (`bench/m4_retrieval.py`, zero credits)

Transplant Mark.5 `bench/retrieval/corpus/{wiki,notes}` (12 Korean markdown files, synthetic - the
personal-data scan runs over them) into `bench/retrieval/` here, the four `notes/` files renamed
into `wiki/` (PATHFINDER has one page kind), and the 17 queries that expect a page (`search-*`
positives and `notes-*`; the 4 negatives are reported as "top hit under the floor", not gated).
The bench builds a temp `state.db`, indexes the corpus through the real `state` index (FTS5 with
the default `unicode61` tokenizer and **prefix queries per token** - Korean particles attach at
the end of a word, so `캘린더*` matches `캘린더가`; no kiwipiepy, no dependency), embeds with the
configured `embed_model` through `openai_compat.embed` (local `bge-m3`), and reports recall@3 for
FTS alone, vector alone and the RRF hybrid, plus the cosine of the best hit for each negative.
Pass (D10): hybrid recall@3 >= 0.80 and >= FTS + 5 points. Otherwise the shipped `embed_model`
default stays empty (vectors off, code kept). Mark.5's own baseline on this corpus with kiwi
segmentation: FTS hit@1 7/12, vector 11/12 - the number to beat for FTS-only is the interesting one,
since prefix matching replaces a dependency.

## ledger.py

`TYPES` gains `memory_pass` (`{from, upto, read, steps, usage, applied, proposed}` - one per pass,
recorded when the pass ends; `upto` is the cursor), `memory_apply` (`{target, op, old, new,
evidence, approval?}` - one per applied edit, the whole undo record: the inverse op is computable
from it) and `session_summary` (`{upto, text}` under the session it summarizes). `ORIGINS` gains
`memory` (the pass's own turns and tool events). `approval_pending` for a proposal carries
`tool: "edit_memory"`, the proposal as `args`, `messages: []` and no `job`.

## llm/openai_compat.py — `embed(endpoint, texts) -> list[list[float]]` (~25 lines)

One POST to `/embeddings` (the OpenAI-compatible route Ollama also serves), `{"model", "input"}`,
`LLMError` on any failure. The embedder is `cfg["embed_model"]` resolved through the same
`llm.resolve`; an empty `embed_model` means "no vectors" and never an error.

## state.py — the page index (+~110), summaries (+~20), memory counters (+~30)

Files are the truth; these tables are an index, dropped and rebuilt by `rebuild()` like the rest:

- `pages(path PRIMARY KEY, title, mtime, text)`, `pages_fts` (FTS5 external-content over
  `pages`, `unicode61`), `vectors(path PRIMARY KEY, model, vec BLOB)` - one float32 vector per
  page (`struct.pack`, no numpy), tagged with the model that made it: a vector from another model
  is not a hit (a stale vector is a wrong value).
- `index_page(path, title, text, vector=None, model="")`, `remove_page(path)`, `pages()` (the map:
  path, title, mtime, newest first).
- `search(query, query_vector=None, top_k=3) -> [{path, title, score, snippet}]`: FTS5 with every
  query token as a prefix term (`"tok"*`, OR-joined, bm25 `rank`), vector = cosine over every
  `vectors` row of the current model in Python (`ponytail:` ceiling about 10k pages, sqlite-vec is
  the upgrade path), merged by reciprocal rank fusion (k = 60). With no `query_vector` it is FTS
  alone - the D10 fallback is the same function with one argument missing.
- `summaries(session PRIMARY KEY, upto, text)` projected from `session_summary`; `history(session)`
  returns `[{"role": "system", "content": "Earlier in this conversation, summarized: ..."}]` plus
  the messages with `seq > upto` when a summary exists - the compaction is a query, not a rewrite.
- `memory_cursor` in `meta` (the last `memory_pass.upto`, 0 at first); `memory_stats(now)` -> passes
  and lines read per local day for the last three days, applied per day, pending proposals,
  judgments per day for the last seven (`clock.day_start` only; no date maths here).

## memory.py — the vault, the tools, the gate, the pass (~420 lines, budget 600)

The vault is `<home>/USER.md`, `<home>/RULES.md` (both already there) and `<home>/wiki/*.md`. No
`vault/` directory and no move: "inside the vault" is a path check against these three.

**Tools** (registered into `tools.TOOLS` at import, like `jobs.py`; `ToolSpec.origins` defaults
change from `ORIGINS` to the four existing origins, so `memory` is opt-in per tool - the pass never
sees `write_file`, `run_command` or a schedule tool):

- `search_memory(query)` - `state.search` with the query embedded when `embed_model` is set (an
  `LLMError` from the embedder degrades to FTS-only inside the handler). Origins: all five. Read.
- `read_memory(path)` - `wiki/<name>.md`, `USER.md` or `RULES.md`, vault-relative, resolved and
  checked to be inside the vault (no `..`, no absolute). Origins: all five. Read.
- `read_ledger(after, limit=80)` - the numbered slice, `#seq [type] text`, one line per readable
  event (`user`, `assistant final`, `signal` summary, `judgment` with its signal's kind and
  outcome, `reaction` with the kinds its delivery carried, `approval`, `job_done` summary,
  `memory_apply`); tool chatter is skipped. Text clipped to 400 characters per line, the whole
  result under `tools.clip`. Origins: `memory` only. Read.
- `edit_memory(target, op, old, new, evidence)` - `reversible=True` (every edit is line-level and
  its inverse is in the ledger, D22's meaning), origins `{user, memory}`. The handler is
  `Manager.edit`, below. A user turn's call runs by itself (policy: user/reversible = allow); the
  pass's too (memory/reversible = allow); nothing else may even see it.

**The gate (`vet`, no model, transplanted from Mark.5 `gate.py`):** target is one of the three
kinds and inside the vault; op in `add | replace | remove`; `new` non-empty for add/replace, `old`
non-empty for replace/remove; for replace/remove `old` matches **exactly one line** of the current
file (`new` may hold several lines - this is how a RULES.md row lands above the `always` row); the
file after the edit still parses: RULES.md through `rules.parse` with the loaded senses' vocabulary
(an unknown condition word or action is refused - the model proposes, the closed vocabulary
decides), USER.md's frontmatter still yields the three lists; `evidence` is a non-empty list of
seqs that exist in the ledger - for a pass, seqs the pass itself rendered (`allowed`, kept on the
pass's `TurnControl`); for a user turn an empty `evidence` is filled with the seq of that session's
latest `user` message (the model has no seq to cite in conversation); **a USER.md edit's evidence
must be `origin: user` events only** (a `user` message, a `reaction`) - the invariant
`test_user_facts_need_user_origin`. A failed check returns the reason as the tool's result; nothing
is written.

**Routing and apply:** `wiki/*.md` applies at once - atomic write (tmp + `os.replace`), `add`
creates the page (the text starts with `# Title`) or appends a line, `replace`/`remove` on the one
matching line; then `state.index_page` (embedding the page when `embed_model` is set, FTS-only on
an `LLMError`) and `memory_apply` recorded. `USER.md` and `RULES.md` go to the approval box:
`approval_pending {tool: edit_memory, args, messages: []}`, the tool returns "queued for your
approval as <id>" - unless the call is a user turn's and `cfg["grants"]["edit_memory"]` is
`allow` (D28's auto mode): then it applies at once like a wiki edit and the tool returns
"applied". The pass never reads grants. `Manager.decide(approval_id, decision)` is the daemon's third `approve`/`deny`
route (a row with tool `edit_memory` and no `job`): approved -> re-vet against the file as it is
now (the line may have moved), apply, record `memory_apply {approval: id}`, and for RULES.md call
the daemon's `reload_rules`; denied -> nothing. `undo(n)` (CLI) replays the last n `memory_apply`
events backwards as inverse ops (add -> remove `new`, replace -> replace `new` with `old`, remove ->
add `old`), each recorded as its own `memory_apply`.

**Resident context:** `context(home, state) -> str` = `USER.md`'s body (below the frontmatter,
whole) + the wiki map (`- title (YYYY-MM-DD)`, newest first, capped at 1,000 characters). The daemon
appends it to the per-turn second system message after `clock.now_line()` (user and signal turns) -
the resident first message stays byte-stable; the second one already changes every turn.

**The pass (`Manager.run_pass`):** one turn of `loop.run` on its own thread (priority `job` in the
slot): messages = `prompts/consolidate.txt` (byte-stable) + `clock.now_line()` + a goal message that
carries the ledger range (`after`, `upto`), the closed vocabulary line (condition words of the
loaded senses, the actions, the kinds - it varies per home, so it is not in the prompt file), the
previous `session_summary` of the live session if any, and the live session's id. Tools =
`tools.for_origin("memory")`; `Broker(origin="memory")`; budget `consolidate_steps`. The pass's
final answer is the session summary: recorded as `session_summary {session, upto, text}` for the
live session (the latest one younger than `session_resume_s`) with `upto` = the seq of its
fifth-newest message (the last four stay verbatim); no live session, or no answer -> no summary.
Then `memory_pass {from, upto, read, steps, usage, applied, proposed}` and a `Signal(sense
"memory", kind "memory_pass", trust "self", fields {applied, proposed, pages})`. A `Gate` cannot
happen (nothing in the pass's registry asks); if one does it is recorded as an `error` and the pass
ends. `LLMError` ends the pass with `memory_pass {read: n, applied: 0, error}` - **the cursor still
advances** (Mark.5's rule: a pass that failed to parse does not re-read the same lines forever; the
lines are still in the ledger).

**The trigger (`Manager.tick`, a 60 s thread started by `Daemon.start`, stopped by `close`):** a pass
starts when no turn is running in any session and no job holds the slot (daemon callbacks), the
latest `user` message is older than `consolidate_idle_min` minutes, at least `consolidate_min_events`
readable events sit after the cursor, and no pass is running. `pathfinder memory run` forces one
through the socket (`memory.run`), the live check's lever.

**`prompts/consolidate.txt`** (English, byte-stable): you keep the user's memory; read the slice with
`read_ledger` (page through it), search and read before you edit, write only what changed - a line,
never a page; one paragraph is one line so a later pass can target it; a new page starts with
`# Title`; promote only what will still be true tomorrow; cite evidence seqs exactly as shown;
USER.md takes only facts the user stated in their own words or reacted to; RULES.md takes a row
only when the same kind was judged the same way at least three times and got a 👍 or no 👎, using
only the listed vocabulary, placed above that kind's `always` row; never invent; finish by
answering with a summary of the live session's conversation so far (for your own memory next turn),
nothing else.

## senses/memory.py (poll, cadence 3600 s, ~40 lines)

`TRUST = "self"`. Kinds: `memory_pass` (fields `applied, proposed, pages`; summary
`Memory: <n> wiki edit(s), <m> proposal(s) waiting for your approval`; default silence) and
`memory_stalled` (fields `days`; summary `Memory: nothing learned in 3 days although there was
something to read`; default silence). Registers `proposed_over` and `applied_over`. `poll` reads
`state.memory_stats`: three consecutive local days each with a pass that read lines and applied
nothing -> one `memory_stalled` signal keyed by the day (the 24 h dedupe makes it once a day).
`memory_pass` is pushed by the `Manager`, not polled (like `jobs`), so the socket refuses `signal
memory ...`. Shipped RULES.md: `## memory_pass`: `proposed_over 0 -> speak` (something waits for
the user), `always -> silence` (a wiki edit alone is visible in `pathfinder memory`, not worth a
word); `## memory_stalled`: `always -> speak` (blueprint §9: Mark.5's 24 silent days become 3).

## policy.py (+1 row)

`memory: {read: allow, sandboxed: deny, network: deny, write: deny, reversible: allow}` - the pass
reads the vault and the ledger and makes reversible edits, nothing else; `grants` never apply (they
are the user's, for user turns).

## rules.py / RULES.md

Frontmatter gains `consolidate_idle_min: 20`, `consolidate_min_events: 20`, `consolidate_steps: 12`.
`Rules` gains the three fields. `load()` is unchanged; the daemon gains `reload_rules()` (below).

## daemon.py (+~20 lines)

- `memory.Manager(home, record, push, step_factory, rules, is_busy)` built in `__init__` and bound
  (`memory.bind`), `tick` started in `start()`, stopped in `close()`; `dispatch` marshalled like
  `jobs.Manager`'s.
- `approve`/`deny`: a row whose tool is `edit_memory` and that has no `job` goes to
  `memory_mgr.decide` instead of a lane.
- `reload_rules()`: `rules_mod.load` again, rebinding `self.rules` on the outbox, the jobs manager
  and the memory manager; a parse error keeps the old rules and records an `error` event. Called
  after an approved RULES.md edit, and at signal triage when `RULES.md`'s mtime moved (one `stat`)
  - this closes HISTORY's "read once at daemon start" for hand edits too.
- Protocol in: `memory.run` (forces a pass; `error` if one is running). Out: nothing new - a pass
  speaks through the signal path.
- `turn()` and `_signal_model_turn()`: the second system message becomes `clock.now_line() + "\n\n"
  + memory.context(...)`.
- `reindex` (CLI, daemon not serving): after `state.rebuild`, `memory.reindex_pages(home, state,
  cfg)` walks `wiki/*.md` and indexes each (embedding when set).

## __main__.py

`pathfinder memory` (cursor, passes and lines read today, applied today, pending proposals, the
last summary's `upto`, judgments per day for seven days - the number D13 says must fall),
`pathfinder memory run`, `pathfinder memory undo [N]`. `approvals` prints an `edit_memory` row as
`target op: old -> new`.

## Tests that must exist

- `test_state.test_search_fts_prefix_matches_korean_particles`, `test_search_hybrid_rrf_order`,
  `test_search_ignores_vectors_of_another_model`, `test_history_folds_after_summary` (summary +
  the messages after `upto` only), `test_reindex_restores_pages_summaries_and_cursor`.
- `test_memory.test_vet_refuses_outside_vault`, `test_vet_refuses_unknown_rule_word`,
  `test_vet_replace_needs_exactly_one_line`, `test_user_facts_need_user_origin` (a signal-origin
  seq as evidence for USER.md is refused; a `reaction` seq is accepted), `test_wiki_applies_at_once`,
  `test_user_and_rules_go_to_the_box` (approval row, nothing written; approve applies and reloads;
  deny writes nothing), `test_undo_is_the_inverse_op`, `test_pass_reads_slice_and_advances_cursor`
  (fake step: read_ledger, one wiki edit, one USER.md proposal, a summary -> `memory_apply`,
  `approval_pending`, `session_summary`, `memory_pass` with the counts), `test_pass_llm_error_still_advances`,
  `test_pass_evidence_must_be_from_its_slice`, `test_user_turn_evidence_is_the_last_user_message`,
  `test_tick_waits_for_idle_and_enough_lines`.
- `test_daemon.test_memory_approval_routes_to_manager`, `test_rules_reload_on_approved_row`
  (the next signal of that kind is decided by the new row, no `judgment`),
  `test_rules_reload_on_hand_edit`, `test_turn_context_carries_user_md_and_map`,
  `test_resident_prompt_frozen` (unchanged: the first system message is still byte-stable).
- `test_senses.test_memory_stalled_after_three_silent_days` (events backdated by hand through
  `state.apply`), `test_memory_pass_speaks_only_with_proposals`.
- `test_policy.test_memory_origin_row`; `test_tools.test_for_origin_memory_is_opt_in` (the pass's
  registry holds exactly `search_memory, read_memory, read_ledger, edit_memory`).
- `test_invariants`: `memory.py` <= 600; `senses/memory.py` <= 150; only `ledger.append` writes
  events; no date maths outside `ledger`/`clock`; the socket refuses `signal memory`.

## Build order

1. `openai_compat.embed`, the `state` page index and `search`, `bench/m4_retrieval.py` with the
   transplanted corpus (scanned) -> run (local embedder, no credits) -> HISTORY -> D10's default.
2. `memory.py` (tools, vet, apply, undo, context, pass, tick), `policy` row, `ledger` types and
   origin, `prompts/consolidate.txt`, `senses/memory.py`, `rules` frontmatter, `state` summaries and
   counters, daemon hooks, CLI, tests - in three implementer units (index+bench / memory.py+tests /
   daemon+senses+CLI+docs), each under 25 tool calls -> live checks (2)–(6) by the lead in a temp
   home (`config set embed_model ollama:bge-m3` there - a model name lives in config, never in code).
3. Reviewer pass (opus, read-only, reproduce) -> ARCHITECTURE both languages -> scan -> commit ->
   push -> draft PR #6 on `partition-4`.

## The owner's answers (2026-09-24) -> D27–D29

Q1 = a (float32 blobs, Python cosine). Q2 = a, plus the owner's manual/auto idea in its lazy
form: `grants {"edit_memory": "allow"}` makes a **user turn's** USER.md/RULES.md edit apply at
once (the handler reads the one grant; the pass's proposals always go to the box - grants never
apply to origin `memory`). Q3 = a (USER.md body + wiki map resident). This contract is binding.
