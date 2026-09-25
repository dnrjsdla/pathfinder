# Decisions

Format: **Dn. decision** — why — *reversed if*. A decision without a reversal condition is a
preference and does not belong here. All were confirmed by the owner (2026-09-21/22).

**D1. New repository; transplant selected Mark.5 modules** (MCP client with description-hash
pinning, policy table, broker, agent loop, rules engine, shell sandbox, gate pattern, hybrid index;
about 3,300 lines). — A fork would still rewrite the 2,426-line core and would need a full history
rewrite before going public. — *Reversed if* partition 1 shows that more than half of the
transplanted modules must be rewritten for the new lane structure: then carry the ideas only.

**D2. Own harness with provider adapters; most LLM API keys supported from the first version**
(Ollama cloud, OpenAI, Anthropic, Google, ...). Two adapters: OpenAI-compatible and Anthropic
native. — Open source, and a connection spec other assistants can use; the Claude Agent SDK is
Claude-only and forbids offering subscription login in distributed products. — *Reversed if* a
provider cannot pass the common contract: split support into "first-class" and "experimental".

**D3. v1 = macOS first, including the full path: tool proposal -> one approval -> install.**
The verification server is closed and built by a teammate. — The application's first prototype and
its primary metric (one tool attached per user per week) need the end-to-end path. — *Reversed if*
3 or more of 10 beta users are not on macOS: bring Linux forward.

**D4. Install targets are two: PATHFINDER's own MCP client and the user's Claude Code config.**
— That is where the target user actually works. — *Reversed if* m5 cannot merge and roll back a
foreign config file 3/3: v1 installs into PATHFINDER only.

**D5. Start as a private GitHub repository with a noreply commit identity; go public when the
whole structure runs and passes the personal-data scan.** License is chosen at that point, after
legal advice. — *Reversed if* beta recruiting or teammate review needs a public link earlier
(license first).

**D6. Code, comments, commits, docs in English; README and UI bilingual; ARCHITECTURE also kept in
Korean (`ARCHITECTURE.ko.md`).** — *Reversed if* the two files drift apart in two or more commits:
make one canonical and generate the other.

**D7. Build and verify without paid API keys.** Live chat models are Ollama cloud models only
(local chat models are too small); the only local model is the `bge-m3` embedder. Claude Code
subagents act as test drivers and graders, never as the product's runtime. Direct Anthropic /
OpenAI / Google adapters are written to spec, unit-tested against recorded responses, and labelled
"experimental". — *Reversed if* a key becomes available: rerun m1 on that provider and promote it.

**D8. Principle: a real assistant, not a problem-solving tool — look one step ahead.** Do not cut
a capability assistant-ness depends on merely because it is unused today; cut what can be absent
without reducing the assistant experience. — *Reversed if* a capability kept under this rule is
never used in two weeks of real use: move it to the open list.

**D9. Model configuration is `model`, optional `worker_model`, and `embed_model`.** Five roles
were dropped. — Under D7 there are no price tiers, so the roles collapse onto one model; "another
LLM does the subtask" is satisfied by an independent context. — *Reversed if* m6 shows the free
credit is insufficient: add a light-model key (light free models exist).

**D10. Embeddings and a vector index are in v1 (FTS5 + sqlite-vec hybrid).** — The wiki will grow;
adding it later means handling it twice. Indexing must succeed with FTS alone when no embedder is
available. — *Reversed if* m4 shows hybrid recall@3 gains under 5 points over FTS alone: default
off, code stays.

**D11. Native tool calling first.** — Verified live on three free-plan families (see HISTORY).
Mark.5 telemetry: 60% of turns took one step, maximum seven, so scripted multi-call turns buy
little. The sandbox stays for the shell tool. — *Reversed if* a family scores under 85% in m1
(that family becomes experimental); if all do, build code mode.

**D12. Steer is in v1:** what the user says mid-turn is inserted at the next safe point (after a
tool result, or by cutting the stream and keeping what was said). Signals never cut in; they wait
for the turn boundary in one queue. — *Reversed if* m2 fails: interrupt and start a new turn.

**D13. Signals the rules cannot decide go to a fenced model judgment** (closed JSON answer, 20 per
day, light model, thumbs on every judgment). **This is temporary; the structural direction:** the
model judgment is a *rule-discovery device* — judgments that repeat and earn thumbs-up become rule
proposals in the inbox, and once approved that kind never reaches the model again. Track that the
count falls over time. — *Reversed if* thumbs-up rate is under 70% after two weeks (switch off);
if the count does not fall in four weeks, redesign the absorption mechanism.

**D14. No reference-server process** (amends D3): a spec document, spec-shaped fixtures, and a
`catalog` that reads the official MCP registry directly. — *Reversed if* the teammate's server
gets an address: point at it, and add the HTTP round-trip test and the allowlisted outcome
projection then.

**D15. Work-context and "CLI" signals come from all three: attaching a PATHFINDER screen, a shell
hook, and Claude Code hooks; each is switched on during onboarding.** — *Reversed if* a source
never leads to an utterance in two weeks of real use: switch that sense off.

## Partition 2 (decided 2026-09-22)

**D16. An approval gate ends the turn** (`turn_end {kind: "gated"}`); the in-flight messages are
written into the `approval_pending` event, and an `approve` starts a new turn from that point. —
The wait lives in the ledger, so it survives a restart and a deleted `state.db` for free, and the
lane stays free. A blocking worker thread would die with the daemon; Mark.5's "collect writes,
approve after the answer" only made sense in code mode (it breaks write-then-verify chains under
native tool calling). — *Reversed if* real use shows the "continue" context is lost because another
turn ran in between more than once a week: pin the gated turn's context to the front of the next
turn.

**D17. A `say` that arrives while a turn is running is a steer**, not a queued turn. — D12's own
sentence: when the user speaks, the assistant hears it. A separate `steer` message would push the
"is this a steer or a new turn" judgment into the screen, which would rebuild this rule there. —
*Reversed if* m2 fails (then D12's fallback: interrupt, new turn) or users demonstrably queue
follow-up questions while an answer streams.

**D18. m2 runs on the default model and one second family (gemma4).** — Mechanism test, but m1
showed families differ in how they treat arguments; two families cost about 0.24M tokens. —
*Reversed if* the two disagree by more than 10 points: run the third.

**D19. Steer stays in v1 in both forms (amends D12's fallback).** At a tool boundary the steer is
a plain user message (m2: reflected 10/10 and 8/8, kept 9/10 and 7/8). Mid-stream the answer is
cut and continued in the same turn with the steer inside an envelope that tells the model its
answer was interrupted and to apply the instruction from where it stopped (m2: reflected
70-100%, kept 70-90% - below the 18/20 line, accepted knowingly). — D12's fallback "interrupt,
then a new turn" puts the same partial and the same words in front of the model without the
envelope, which measured worst (kept 3/10 and 6/10); the real alternative is not accepting
mid-stream interjections at all, which is a step back from D8. — *Reversed if* mid-stream steers
earn more than 20% thumbs-down in two weeks of real use: then a mid-turn `say` waits for the turn
end and interrupt stays explicit.

**Approval is explicit** (`approve`/`deny` messages, a `y` key in the screen later). A "yes" in
conversation is never read as approval by the model: the evaluating side has no model (blueprint
§2). Not a decision with a reversal condition — a principle already agreed.

## 2026-09-22 — Partition 3 (owner's choices on the contract's three questions)

**D20. A `speak` decision delivers the code-assembled notice plus one step from the model.** The
signal turn (origin `signal`, envelope, no history, read tools run, writes and network gate,
budget 4) appends one or two sentences to the sense's one-line summary; when the model is blocked
or silent the summary alone goes out. — D8: "meeting in ten minutes" is a notification, "meeting in
ten minutes - yesterday's draft is here" is an assistant; the morning brief only exists in this
form. Cost is inside m6's twenty signal turns a day. The notice line itself is never the model's
(blueprint §8). — *Reversed if* signal-turn additions earn more than 20% thumbs-down in two weeks:
then the summary alone, and the model joins when the user replies.

**D21. Mail arrives through IMAP headers only (Mark.5 bridge, password in the Keychain).** MCP
mail servers are reviewed at the entrance of partition 6, when the MCP client exists: if one
serves, the mail sense becomes a thin file over its tools and nothing else changes. — Proven
bridge, no body ever fetched, no client to pull forward. — *Reversed if* the partition 6 review
finds an MCP mail source that needs no app password and passes m3.

**D22. The policy table gains a `reversible` column: user allow, signal ask, job ask, system
deny.** A tool that is a write the user can undo by deleting a row (`add_schedule`,
`remove_schedule`) declares it; a calendar write does not (it may sync and invite). — The
invariant "only what can be undone runs by itself" becomes a cell of the table instead of a
`grants` default; a mail-opened turn still asks. — *Reversed if* a reversible tool is ever found
to have had an effect outside its own row: then it loses the flag.

## 2026-09-23 — Partition 4 (owner's choices on the contract's three questions)

**D23. m7 decides the job evaluator: ten goals, with and without one evaluator pass, graded
blind by an opus subagent with the owner checking five pairs; the evaluator row lands only if it
earns at least three more thumbs-up out of ten.** — The open path from the harness review
(partition 2) said "measure at the entrance"; a subagent grader keeps the owner's cost to ten
minutes while staying inside D7 (a subagent is a grader, not a runtime). — *Reversed if* the
owner's five spot checks disagree with the grader on two or more: then the owner grades all
twenty.

**D24. A job's write pauses the job at the gate and `approve` resumes the job itself.** The
mechanism is D16's: `approval_pending` keeps the job's messages, the row carries the job id, and
the resume runs the job loop, not a session turn. — "Research it and put it in a file" is a job
with a result; a dropped gate (the signal turn's rule) would make every worker read-only. —
*Reversed if* real use shows jobs gating more than once per job on average: then the worker
collects its writes and gates once at the end.

**D25. One model request at a time, the user first.** A slot in `llm/` serializes every
streaming request; a job takes the slot for one step and yields between steps; a user or signal
turn waits at most one job step. — The free plan allows one concurrent request (partition 0's
finding); m7a measures what the second request actually gets, and either way the slot keeps a
worker from doubling the day's request rate. — *Reversed if* m7a shows two requests run freely
AND real use shows user turns waiting on job steps more than a few seconds: then jobs run only in
the user's idle gaps.

**D26. A signal's exemption from the budget and quiet hours is its own flag (`Signal.exempt`),
not a side effect of `trust: user`; a job's outcome is `trust: self` + `exempt`.** — The review
of partition 4 found `trust` doing two jobs: "the user's own words, act on them" in the envelope,
and "spends no budget, honours no quiet hours" in the gate. A job's summary is the model's text
written from whatever it read (a web page, a mail), so it must not reach the model as the user's
words; but the user did start the job, so its outcome is still theirs to hear at 3 a.m. The
user's own schedule and reminders keep `trust: user` (they are their words) and are exempt as
before. — *Reversed if* a second exempt-but-not-user signal never appears in two partitions:
then the flag folds back into the trust check.

## 2026-09-24 — Partition 5 (owner's choices on the contract's three questions)

**D27. Vectors live in `state.db` as float32 blobs and cosine is computed in Python; no vector
extension.** One vector per wiki page, tagged with the model that made it; FTS5 (the default
tokenizer, one prefix term per query token) and the vector list are merged by reciprocal rank
fusion. — The standard library covers a personal wiki (tens to hundreds of pages) with no fourth
dependency and no install failure point; the storage format is the one sqlite-vec reads, so the
upgrade is one function. — *Reversed if* the wiki passes about 10,000 pages or a search takes
over a second: then `sqlite-vec` as an optional dependency, same blobs.

**D28. An edit to `USER.md` or `RULES.md` goes to the approval box whoever proposes it; the
owner may switch their own turns to apply-at-once with the existing `grants`
(`{"edit_memory": "allow"}`), never the consolidation pass.** Wiki edits apply at once for
everyone (line-level, the inverse op is in the ledger). — One path by default: every line in the
two files the rules and the resident context read has an approval behind it. The owner's
manual/auto idea (Claude Code's permission modes) needs no new setting: `grants` already means
"do not ask me about this tool, in my own turns only", which is exactly the line between what
the user said and what the pass inferred. — *Reversed if* real use shows the owner approving
every `edit_memory` row without reading it for two weeks: then apply-at-once becomes the default
for user turns, the box stays for the pass.

**D29. Resident every turn: `USER.md`'s body and the wiki map (title and date, newest first,
about 1,000 characters), in the per-turn second system message after the local-time line.**
The first system message stays byte-stable (cache). — D8: an assistant that knows who it is
talking to and what it has written down can look one step ahead without a tool round trip the
free models mostly skip; the cost is about 500–800 uncached tokens per turn, five to six percent
of an m6 day. — *Reversed if* two weeks of real use put the resident context above 15% of the
day's tokens: then the map only, `USER.md` through `read_memory`.

**D30 (after partition 5's live checks, 2026-09-24). When a turn's step budget is spent and the
model's last reply was still a tool call, the loop makes one more request with no tools, so the
turn ends in words.** Every trigger (user, signal, job, consolidation) gets it through `loop.run`'s
`final_step`. — Live, three turns in a row exhausted eight steps on repeated searches and said
nothing; the dangling `user` message then made the next turn keep answering the previous
question. "I looked eight times and found nothing" beats silence, and the history closes. Cost:
at most one extra request per exhausted turn. — *Reversed if* the final step itself comes back
empty in more than a fifth of exhausted turns in real use: then an assistant placeholder in
history instead.

## 2026-09-24 — Partition 6 (owner's choices on the contract's three questions)

**D31. The install approval itself grants `allow` to the tools the sandboxed trial listed with
`readOnlyHint`; every other MCP tool asks until the user grants it.** The proposal row shows the
list, so the one approval is an informed one. — A hint is the server's own word (the spec calls
it non-binding), so it is trusted only for tools the trial actually saw, and only because the
user read the list before approving; asking on every search call would make an installed tool
useless (D8). — *Reversed if* a read-only-hinted tool is caught writing or sending in real use:
then every MCP tool asks until granted by hand.

**D32. The catalog never proposes a package whose license the application excludes (GPL, AGPL,
LGPL, non-commercial, none); an explicit "install X" still checks it, shows the verdict on the
proposal row, and leaves the approval to the owner.** — The verified path is the product's
promise; the owner's own named request is their choice, and the record shows the license they
accepted. — *Reversed if* legal advice at the public launch says an excluded license must never
be installed through the product: then a hard refusal.

**D33. A Claude Code install lands in user scope: `mcpServers` at the top of `~/.claude.json`.**
— Every project sees it, nothing is written into a repository other people share, and the file is
the one the installer already backs up and restores byte-for-byte. — *Reversed if* beta users
ask for per-project tools more often than not: then project scope by default, with the proposal
naming the file.

## 2026-09-25 — Partition 7 (owner's choices on the contract's four questions)

**D34. The core cap is 7,000 lines; the per-module 1.3x line of §6 is the gate that is
measured.** The blueprint's 5,500 was the 4,600 allocation plus slack; the allocation was low
(memory, daemon, state and installer each passed review as minimal and each sits over its row),
not the code fat. — Trimming 900 lines would cost a partition of its own and improve nothing the
assistant does; a cap that is never true measures nothing. — *Reversed if* a module crosses its
1.3x line without a review finding it minimal: then that module is cut before the next partition,
not the cap raised again.

**D35. The screen is a line client in the terminal's own scrollback**: cbreak mode, one redrawn
input line, slash commands, `tui.py` <= 400 lines, stdlib only; no curses, no panes. — Scrollback
is kept, it runs inside another tool's shell, and `say`/`listen` stay as they are, so there is
nothing to reverse. — *Reversed if* two weeks of use show the single input line is what stops
the owner from keeping it attached: then `readline` first, a full screen only after that.

**D36. One foreign-file helper, generalised from the installer's, lives in `installer.py`**
(`merge_json_file(path, mutate)`: backup, atomic write, read-back deep-compare of every other
key); `init` imports it for `~/.claude/settings.json`, and `~/.zshrc` gets the same backup
discipline as an append. — One reviewed place to fix; a second copy is the defect class the
partition-6 review flagged. — *Reversed if* a third caller outside the installer appears: then a
module of its own.

**D37. The Anthropic adapter is built now, as the partition's last unit, with recorded responses
only and marked experimental everywhere it is named.** — D2 promised two adapters and D7's
reversal (a key appears -> m1 on that provider) needs the adapter to exist that day; building it
now also catches the loop's message shape drifting away from a second provider. It is the first
unit dropped if the partition runs long. — *Reversed if* no key exists by the public release:
then the adapter ships as a documented stub, not a promise.
