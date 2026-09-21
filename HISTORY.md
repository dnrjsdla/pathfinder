# HISTORY

What reality changed after the structure was agreed. The blueprint is not edited after
2026-09-22; this file is.

## 2026-09-22 — Partition 0 started

- Repository initialized (private first, noreply commit identity, pre-commit personal-data scan).
- Free-plan probe on the owner's Ollama account, one native tool call per model through the
  OpenAI-compatible endpoint: succeeded on `gpt-oss:20b-cloud`, `gpt-oss:120b-cloud`,
  `gemma4:31b-cloud`, `nemotron-3-nano:30b-cloud`, `nemotron-3-super:cloud`; HTTP 402
  ("not included in your free usage") on `glm-5.1`, `glm-5.3`, `deepseek-v4-flash`, `kimi-k3`,
  `qwen3.5`, `minimax-m2.7`. Three free families exist, so m1 can run as designed.
- Finding: the free plan is starter models + a small undisclosed monthly credit + 1 concurrent
  request. m6 was therefore changed from "run a synthetic day three times" to "sample each turn
  type, extrapolate to a day, price it" so the measurement does not burn the month's credit.

### m6 — what a resident day costs (`bench/m6_daycost.py`, sample user 6 / signal 5 / job 2 / consolidate 1)

Day profile: 30 user turns, 20 signal turns, 5 worker jobs, 2 consolidation passes. System prompt
plus 12 tool schemas measured at about 1,480 tokens. Prices are the public per-million-token rates
as read from the provider's pricing page on 2026-09-22 (input / cached input / output).

| model | tokens/day | $/day | $/30 days | median first token, user turn | blocked |
|---|---|---|---|---|---|
| `gpt-oss:120b-cloud` (0.15 / 0.014 / 0.60) | 616,424 | 0.0421 | 1.26 | 0.47 s | no |
| `gpt-oss:20b-cloud` (0.07 / 0.035 / 0.30) | 320,888 | 0.0267 | 0.80 | 4.34 s | no |

- Pass line "no 402, first token <= 3 s": **120b passes, 20b fails on latency** (and one
  consolidation pass took 190 s and 14,463 output tokens on 20b against 12.8 s / 4,004 on 120b).
  The lighter model is not the faster one on this service; the default model is the 120b.
- Prompt caching works through the OpenAI-compatible endpoint: 82% of user-turn input and 97% of
  signal-turn input was billed at the cached rate. The resident system prompt is nearly free after
  the first request - keep it byte-stable (invariant: resident prompt frozen during a session).
- Worker jobs dominate per-turn cost (about 29k input tokens over 5 requests each); user turns
  dominate the day because the history grows (fit: first-request size rises with turn index).
- Caveat from review: this is a lower bound. Not in the sample: retrieval blocks injected into
  user turns, steer re-sends, the daily catalog check, retries. Plan with x1.3-1.6, i.e. roughly
  $1.6-2.0 per 30 days on the 120b. Conversely, 20 full-context signal turns overstate D13's
  fenced judgments (closed JSON, no tools).
- Unknown that only the owner can read: the size of the free monthly credit. The run itself spent
  about 0.2M tokens (about $0.02 at list price) without being blocked.
### m1 — native tool calling across free model families (`bench/m1_toolcall.py`, 20 tasks, scored by code)

| model | passed | A single | B normalize | C chain | D distractor | E no tool | F missing info | tokens | median latency |
|---|---|---|---|---|---|---|---|---|---|
| `gpt-oss:120b-cloud` | **20/20** | 6/6 | 3/3 | 5/5 | 3/3 | 2/2 | 1/1 | 23,203 | 0.75 s |
| `gemma4:31b-cloud` | **20/20** | 6/6 | 3/3 | 5/5 | 3/3 | 2/2 | 1/1 | 26,386 | 0.66 s |
| `nemotron-3-super:cloud` | **20/20** | 6/6 | 3/3 | 5/5 | 3/3 | 2/2 | 1/1 | 50,735 | 4.10 s |

- Pass line (each family >= 17/20): **all three pass.** D11 stands: native tool calling, no code
  mode, no script executor. Saved traces were read by hand to confirm the passes are real (right
  tool, right arguments, search-then-read and list-then-add order, no tool for arithmetic, a
  question instead of an invented recipient).
- Ceiling effect: 20/20 everywhere means this set answers "does it work" but does not rank the
  families. Latency does: nemotron-3-super is about 5x slower per request.
- A defect the scorer could not see: on task C5 `gemma4` said "added at 11 a.m." while sending
  `start_iso: ...T11:00:00Z`, which is 20:00 in Seoul. No overlap, so it passed; in production the
  event lands nine hours off. **Design consequence for partition 2:** time arguments are local
  wall-clock without an offset, the broker rejects or normalizes offsets, and the approval gate
  shows the normalized local time so a human sees where the event really goes.
- Review before the run (reviewer, opus) found six loop defects that would have produced a
  misleading result - echoing the raw assistant message back (reasoning fields), crashing when
  arguments arrive as an object, empty tool_call_id, HTTP errors reading as "0/20", a blocked run
  reading as 12/12, results written only at the end - and scorer holes (UTC answers scored wrong in
  both directions, a right call plus an uninvited email passing, prompts that contradicted "ask
  when information is missing"). All fixed; self-check went from 3 to 12 assertions.

- Defects found by the m6 smoke run and fixed before measuring: the first-token clock started after
  the response headers (read 0.00 s), and the user-turn fit mixed multi-request turn totals
  (negative slope, negative tokens/day). Raw per-turn metrics are now saved so the formula can be
  corrected later without paying for the requests again.

## 2026-09-22 — Partition 1: the minimal round trip

"It runs" was checked against a live model through the unix socket: one turn streams, a second
turn in the same session recalls the first, and after stopping the daemon and deleting `state.db`
the restarted daemon answered "four exchanges so far, the word was plum" - restored from the
ledger alone. 47 offline tests. Core so far about 800 lines against a 5,500 budget.

### What changed against the plan

- **D1, honestly: the Mark.5 loop could not be transplanted.** `react.py` is code-mode through and
  through (it pulls a Python block out of the answer and hands it to an executor). With native tool
  calling (D11) only its ideas carried over: a closed set of outcomes, a loop that knows nothing of
  origin or policy, a budget that is a cost ceiling, exceptions that propagate. The transplant
  value of D1 sits in later partitions (MCP client, policy table, rules engine) and is unchanged.
- The contract gained three things while the daemon was being written: the ledger must be safe
  across PROCESSES, not just behind one function (the CLI appends while the daemon runs; an
  in-memory `last_seq` would hand out duplicates) - now an `flock` plus tail re-derivation; the
  content key of `user`/`assistant` events is `text`; `State.session()` exists for the `attached`
  reply.

### Defects found, by whom

- Lead, during integration: deltas arrived AFTER `turn.end` (ordering had been left to event-loop
  scheduling; now one queue with an end marker put by the worker thread); `close()` hung forever
  with a client attached.
- Implementer: the contract's function name `set` shadowed the builtin inside `config.py`, so
  `load()` called itself; two leaked sockets in the adapter and its tests.
- **Reviewer (opus), NO-GO with two reproduced blockers - the predecessor's death, again:**
  1. `last_seq` was recovered from the newest partition only. A crash on the first append after
     UTC midnight leaves that file empty or holding one truncated line, `last_seq` restarts at 0,
     new events reuse old seqs, and `State.apply` drops every one of them as already applied.
     A day boundary, silent, every test green. Fixed: walk partitions newest-first.
  2. An append after a fragment without a newline glued the new event onto the fragment and both
     were skipped on read, unrecoverably. Fixed: close the fragment with a newline first.
  Also fixed from the same review: mid-stream timeouts and resets escaped the `LLMError` contract;
  an exhausted turn showed the user eight steps of text and recorded none of it; a non-string
  session id dropped the connection without an error line; `State.apply` read the cursor outside
  its transaction (two processes counted a turn twice) - now `BEGIN IMMEDIATE` plus a lock, which
  is also what partition 2's worker threads need; `max_steps 0` ran the model zero times with no
  output and no error; an existing home directory kept its loose mode; `reindex` dropped tables
  under a live daemon; and the "one definition of the day" source scan only looked inside
  `ledger.py` - it now scans the whole package.
- Seam added on the reviewer's advice (D8, one step ahead): every turn owns a cancel `Event` that
  reaches the HTTP stream, so interrupt and steer in partition 2 extend the signatures instead of
  ripping them up.

### Left open on purpose

`flush()` without `fsync`; `_write` without `drain()` (a slow client grows memory); an unbounded
session queue; sessions are never evicted. None loses data; each is a note for partition 7.
