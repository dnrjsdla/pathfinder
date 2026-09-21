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
