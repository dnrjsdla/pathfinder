# m2 — does steer break the conversation? (entrance of partition 2)

**Assumption measured (D12):** a model keeps its thread when one user message is inserted while a
turn is in flight - after a tool result, or after its streamed answer was cut.

**Pass line (owner):** per model, *steer reflected* >= 18/20 AND *original task kept* >= 18/20.
Below: D12's fallback (interrupt, then a new turn) replaces steer. Models (D18): the default model
and gemma4; a third family only if the two disagree by more than 10 points.

## How it is measured — `bench/m2_steer.py`

Stdlib only. Uses the real adapter `pathfinder.llm.openai_compat.stream` (with `cancel`) so the
measurement also exercises the seam partition 2 builds on. Model names come from `--models`, never
from the file. Every request is temperature-free (the adapter sends none; do not add one). Results
are written after every task, raw traces included, so a blocked run still leaves data.

### A. Tool boundary (10 tasks)

Fake tools with fixed answers (subset of m1: `get_weather`, `list_events`, `add_event`,
`search_notes`, `read_note`, `run_shell`). The task needs two or more tool calls. After the FIRST
tool result is appended, a `{"role": "user", "content": <steer>}` is appended before the next
model call. The loop then runs to the answer (max 6 steps).

The steer changes something a scorer can read from the later tool calls or the final text:
a different city, a different time (local wall-clock, scored in KST like m1), a different duration,
one more event, "do not add the event after all", a different note, a different command, answer in
English. Scores:

- **reflected**: the changed argument appears in a call made AFTER the steer (or the forbidden
  call is absent; or the final text is >= 90% ASCII letters for "in English").
- **kept**: the part of the original task the steer did not touch is still done (the weather was
  reported, the event still has the original title, the note was still read, ...).

### B. Mid-stream (10 tasks)

No tools. A long answer is requested (count 1 to 30 one per line, list 20 fruits one per line,
the months of the year with one fact each, ...). The stream is cut with `cancel` once the text has
>= 3 complete lines (or >= 160 characters, whichever first). Then the messages get
`{"role": "assistant", "content": <partial>}` and `{"role": "user", "content": <steer>}` and the
model is called again to completion. The final answer is partial + continuation.

Steers: continue in English, only every fifth from here, go backwards from here, add an emoji to
each line from here, stop at 20 instead. Scores on the CONTINUATION only:

- **reflected**: the steer's rule holds on the continuation (English: >= 90% ASCII letters; every
  fifth: all numbers present are multiples of 5; backwards: strictly decreasing; ...).
- **kept**: the continuation continues the original job - it does not restart from 1, the items
  are still the same kind (numbers, fruits, months), and the original end is reached (or the new
  end when the steer moved it).

### Output

`bench/results/m2-<model>.json` per model with, per task: category, calls (name, args), partial,
continuation, final text, reflected, kept, latencies, tokens, http error if any. Summary table on
stdout: per model, A reflected/kept, B reflected/kept, total, median latency, tokens. `--selfcheck`
runs every scorer on canned traces (at least one pass and one fail per scorer). `--tasks N` limits
the run for a smoke test.

### Exclusions (not the model's score)

`http_err` (a request failed), `no_steer` (A: the model never called a tool, so nothing was
steered), `too_late` (A: the steered action ran in parallel before the steer could be sent),
`not_cut` (B: the stream ended, or arrived buffered as a whole, before the cut). The summary
prints them next to the scores. The same error three times in a row stops the run: that is the
server or the harness. `--rescore FILE...` re-runs the scorers on saved traces.

### The mid-stream envelope (added after run 1)

Bare user text after a cut partial made both families rewrite the list from the start: the
model does not know its answer was cut unless the harness says so. From run 2 the steer travels
in an envelope (`STEER_ENVELOPE`); `--no-envelope` reproduces run 1. Partition 2 sends the same
envelope. Results and the decision they led to: `HISTORY.md`.

### Cost

About 3 requests per task, 20 tasks, two models: roughly 0.24M tokens, well under a dollar at list
price. Do not run more than a `--tasks 2` smoke without the lead's go-ahead.
