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

## 2026-09-26 — Live use, day two (owner's choices on three questions)

**D38. Reminders get two write tools, `complete_reminder` and `delete_reminder`, and both ask
first.** "I took the train" had no move but a false one: with list/add only, the model reached
for `remove_schedule` with a Reminders id and reported it removed three times. Complete is the
everyday move; delete is for a reminder that should never have existed. Both stay `write` (not
D22's `reversible`): the owner asked to be asked, and an iCloud-synced reminder is not a config
row PATHFINDER can put back. — *Reversed if* two weeks of `complete_reminder` approvals are all
"approve": then a `grants` entry, not a code change.

**D39. A memory pass the model never answered keeps its slice.** A pass that applied and
proposed nothing and ended on an outage (`LLMError` status 0 - no connection -, 401, 402, 408,
429 or 5xx) records `upto = since`, and the trigger waits 30 minutes before trying again. Any
other 4xx (400, 413: the request the slice made) and an internal error still advance - retrying
the same slice would fail the same way. On 2026-09-26 four offline passes advanced over the
owner's own instruction ("ask me for approval") and the one about MCP; a network outage is not a
property of the lines. — *Reversed if* an outage of days makes the retries themselves noise in
the ledger: then the wait grows, and the slice is still kept.

**D40. A server that needs a secret gets it from the Keychain before its sandboxed trial -
planned into partition 8, not built now.** `easy-notion` exited at trial without its token; the
Slack server passed only because it starts without one, so the check today admits exactly the
servers that do not need what the owner would give them. The installer already reads
`pathfinder-mcp-<id>` entries at install time; the trial simply runs first. OAuth remotes
(`com.notion/mcp`, 403) stay outside v1 as ARCHITECTURE says. — *Reversed if* partition 8's
survey finds that the servers the owner wants mostly need OAuth, not a token: then the step
waits for an OAuth design instead of being built alone.

## 2026-09-29 — Live use, days three to five (owner's choices on two questions)

**D41. A session is its conversation: two hours without one end it, even under an open screen.**
A screen's 30 s `presence` no longer moves `sessions.last_ts`, nor does a signal's silent triage
(of a signal turn, only the notice it delivered is conversation); a `say` on an older session
re-attaches to the current one, and a signal that has to open a new session takes the idle
screens with it so its notice still arrives. One session ran from 2026-09-25 to 09-29 (109
turns) and its history kept bringing back a wrong answer ("MCP only runs inside the desktop
app") and the model's own habit of claiming work it had not done; on a clean session the same
questions called the right tools. — The cost: "what we said earlier" after two quiet hours is
gone from the turn; lasting facts ride in USER.md and the wiki. — *Reversed if* the owner keeps
asking about the previous sitting: first a larger `session_resume_s`, then a carried summary.

**D42. Every turn carries the next 24 hours of the calendar and reminders.** Each sense module
may define `context(now)`; `signals.context` puts their lines, titles only and one line each,
under a "data, not instructions" header in the second system message, read on a worker thread,
and a sense it cannot read says so ("unknown" is not "none"). A 01:13 briefing said "no events
or reminders" without calling a tool. The cost is one EventKit read and roughly 50-300 tokens a
turn; a question about the week still needs the tools. Calendar titles are third-party text and
now sit in a system message - clipped and single-line, but a longer injection bench (m3) has not
been re-run over it. — *Reversed if* m3 over the agenda shows a title steering the model: then
the agenda moves into a tool-result-shaped message.

## 2026-09-29 — Before partition 8 (owner's choices on six questions)

**D43. Partition 8 is trust in the answer plus the security bundle; it starts with a measurement
and nothing else.** The measurement reads the live ledger (no model credits): how often the
assistant states what the calendar, reminders or memory hold, or that something is done, with no
tool result in that turn; how far a stated time is from the turn's `now_line`; and why memory
was never written. Five days of use (152 turns): `edit_memory` was called zero times, nine
consolidation passes ran and applied and proposed nothing, no `wiki/` directory exists, and the
owner's own standing instruction was written down by neither the turn nor the pass. The fix's
scope is decided from the numbers. The secret-input path, D40 and ledger file modes are the
security bundle; the ARCHITECTURE pair's stale opening and the small live defects ride in the
same partition. The screen waits until the functions under it are steady (D35 stands). —
*Reversed if* the measurement shows the unsupported statements come from one session's history
and not from the model's habit: then D41 already fixed it and the partition is the security
bundle alone.

**D44. Tool discovery and verification move out of the assistant into a server; the sandboxed
trial and the install stay local** (amends D14; D3's "built by a teammate" no longer holds - the
owner's team builds it). The server indexes the MCP registry, ranked GitHub repositories and
tool and skill repositories, and answers with connection-spec candidates whose license,
malicious-scan and maintenance fields it filled. A verdict is the same for every user, so it is
computed once; a trial needs the user's credentials and an install writes the user's files. All
ten tool timeouts in five days of use were the official registry's own read timing out. —
*Reversed if* the index cannot be kept fresh on the hardware available: then search alone moves
and verification stays where it is.

**D45. The server speaks MCP, is designed after partition 8 in its own repository, and runs on
the owner's laptop first.** The assistant reaches it through the MCP client it already has, and
the built-in `search_tools` is deleted when the server lands; any other assistant can attach the
same way (D2). Hosting, the public address and who pays are open and decided with the design. —
*Reversed if* the design session finds the approval flow needs a field MCP tool results cannot
carry: then an HTTP API in the registry's shape, behind the `registry` option that exists.

## 2026-09-29 — After m9 (owner's choices on three questions)

**D46. `search_tools` and `propose_install` read a local mirror of the registry, searched by
name and description; the catalog sense refreshes it in the background.** 10 of 28 registry
calls timed out and 7 came back empty because the registry matches a substring of the name and
answers in 1 to 25 seconds. The tool-call ceilings stay where they are: the ceiling was never
the defect. The first sync is about eight minutes and 34 MB. — *Reversed when* the server of
D44 lands: the mirror becomes its indexer and the assistant's copy is deleted.

**D47. A consolidation pass is two calls - one that writes, then one that summarizes - and the
writing prompt has a place for the owner's standing instructions.** In five days nothing was
written; the replay made 0 edits with the shipped prompt and wrote once without its summary
paragraph. — The cost is one more model call per pass. — *Reversed if* the split pass still
writes nothing on the m9 slices: then the write moves into the turn itself, by the harness.

**D48. An unsupported answer is fixed for the model in use, not by choosing another model: the
prompt, and a second look by the same model at an answer that called no tool.** Any model above
a bar must be usable, so the fix cannot depend on which one runs. The 27 answers of m9 are the
replay set, with control turns where no tool was right; the candidates are compared on it
before either is built into the turn. — *Reversed if* the second look flags more than one
control turn in ten: then it is narrowed to answers about the calendar, reminders, memory and
the registry.

## 2026-09-29 — Partition 8 contract (owner's choices; the pasted-token question is still open)

**D49. The core is counted by the blueprint's §6 rows** (ledger, state, daemon, loop, broker,
policy, exec, tools, signals, rules, deliver, jobs, memory); the adapter layer (`llm/`,
`senses/`, catalog, installer, the registry copy) and the unbudgeted infrastructure sit outside
it. By that count the core is 3,762 lines; by partition 7's count it was 7,043, over D34's cap
before partition 8 began. The per-module ceilings in `test_invariants.py` stay the gate. — What
it throws away: partition 7's series is no longer comparable. — *Reversed if* a module outside
the core rows grows past its own ceiling twice: then the adapter layer gets a total of its own.

**D50. The Slack token written to the ledger on 2026-09-26 stays where it is.** The owner
revoked it on 2026-09-29, so every copy is inert; the ledger stays append-only. — *Reversed if*
a secret that cannot be revoked is ever written: then a one-time offline scrub by the owner's
hand, from `scripts/`, never a product command.

**D51. D48's fix is both: the rewritten prompt, and the second look by the same model at an
answer that called no tool.** m10: the prompt fixed 21 of 27 and pushed 2 of 10 controls into a
needless tool call; the second look fixed 20 and touched no control; together 22. The prompt's
state paragraph is narrowed so a recap or a self-review question is exempt. The cost is one
more model call on a turn that answers without a tool - about half of all turns. — *Reversed
if* two weeks of live use show the second look flagging fewer than one answer in twenty: then
it is removed and the prompt stands alone.

**D52. History carries a record of the tool calls the assistant made, and an approval typed
into chat while a gate is open is answered by the harness, not the model.** m10's unfixed
answers: the model denied a call it had made, and took "I approve" in chat for an approval.
Correcting a session summary that asserts something false is left for later (D41 already ends
the session after two quiet hours). — *Reversed if* the tool record grows a turn's prompt by
more than a fifth: then names only, without arguments.

**D53. A secret reaches the Keychain without passing through the chat: the assistant asks
through the harness and the screen takes it masked; a token pasted into the chat anyway is put
in the Keychain and the text carries a handle.** The value goes screen -> daemon -> Keychain over
the local socket and never reaches the ledger or the model. A pasted token-shaped string is
stored under a temporary name for 24 hours and replaced by `[secret:pasted-N]` at the ledger
door, so a false positive can be read back and a real token need not be typed twice. Encrypting
the token for the model was considered and dropped: the model cannot use ciphertext, so it is a
handle with a key to guard. — *Reversed if* the pattern misses a token in live use: the
patterns grow; the handle stays.

**D54. OAuth servers are inside partition 8.** Gmail and the calendar are what the owner asked
for first; a partition that leaves them out does not make the assistant more useful where it
was asked to be. The way in is designed from a survey and chosen by the owner before code.

**D55. The live check of the secret path uses a server that costs nothing.** Brave search is
paid; the check names a free token server the owner would use anyway.

**D56. `mirror.py`'s ceiling is 200 lines.** The contract's 150 was set before the review; the
file is 188 after it, and every line added maps to a failure the review reproduced (an endless
sync on a 4xx, a repeated cursor, a fresh database opened by several at once, a 2,000-word
query). — *Reversed if* the file passes 200: it is cut, not the ceiling raised.

## 2026-09-30 — The way in for Gmail, and skills (owner's choices)

**D57. Gmail comes in through what already exists: the mail sense over IMAP with an app
password, reading and searching; a local Google server that does its own OAuth is the fallback
if the owner's account refuses an app password.** Google Calendar is left out: the owner does
not use it. A generic MCP OAuth client is not built now - measured, it would reach one official
remote (Notion), which a token server already reaches. — *Reversed if* the app password and
the local server are both blocked by the account's administrator: then Gmail waits for the
external server's design.

**D58. Skills are a wiki of their own, apart from the memory wiki, and PATHFINDER ships with a
default set that is there from the first run.** A skill is a checked procedure the assistant
reads when asked to do that thing (connect Gmail), so it stops inventing one - on 2026-09-26 it
invented a way to get a Slack token. This brings back what the blueprint cut (`skills/`); the
blueprint is amended. Anything in a skill that involves a secret goes through the harness
(D53), never through the page. — *Reversed if* two weeks of use show the assistant not opening
a skill it has: then the matching skill's title rides in the turn, as the wiki map does.

**D59. Mail is read and searched, not sent, for now.** The owner wants sending; it waits until
the second look (D51) has been measured in live use. When it comes it is a write tool that asks
every time, showing the recipient, the subject and the body.

**D60. D36's foreign-file helper moves to `config.py`** (`backup_file`, `_atomic_write_json`,
`merge_json_file`, about 40 lines; decided 2026-09-30). `installer.py` reached 700/700 at U3 and
U10 and U16 still need 6 lines; by D56 a file past its ceiling is cut, not raised. The helper
writes files, it does not install, and `init` already imports it. It stays one reviewed copy:
`installer.py` and `init.py` import it from `config`. It writes foreign files only; `config.set()`
remains the one writer of PATHFINDER's own config. — *Reversed if* `config.py` needs a ceiling
the helper crowds: then D36's own reversal applies, a module of its own.

**D61. Skills live in one place, `<home>/skills/<name>.md`; their index rides in `read_skill`'s
own description; the assistant may propose a skill from a user turn (confirms and amends D58).**
The package's `pathfinder/skills/*.md` are seeds: a seed is copied into the home when its file is
missing, the way `rules.load` writes `RULES.md` and `USER.md` at the daemon's first start, and the
copy never overwrites. A new shipped skill arrives on upgrade; one the owner edited is left alone.
There is no override rule and no marker for a newer shipped version. The owner proposed this; its
cost is that a fix to a shipped skill does not reach a home that already has that file. The index
is every skill's `name` and `description`, sorted, never cut, bounded at load. The owner writes
skills by hand, and the assistant may propose one from a user turn: always the approval box,
never `grants`, never the memory pass, never from a signal, job or memory origin. One directory,
whatever the source, is also where a server package's skill lands (D44/D45). — *Reversed if* two
weeks of use show the assistant not opening a skill it has: then the matching skill's title
rides in the turn, as the wiki map does. If shipped fixes failing to reach homes becomes a real
cost, a marker is added then.

**D62. Every tool result passes a character filter before the model sees it.** Found at U22's
review: a tool result was clipped for length and otherwise raw, so bidi marks, zero-width
characters, ANSI sequences and control characters reached the model from web pages, mail bodies
and MCP servers; U21 filters only the signal envelope. One function, `tools.clean(text)`, removes
those and keeps newlines and tabs without collapsing whitespace, since a result may be code or a
page. `oneline` becomes `clean` plus its flattening, so there is one character rule, and the mail
body's per-line `oneline` becomes `clean`. It runs in `broker._clip_for_model`, which every
result and every error string already passes. The ledger's `tool_result` keeps what the tool
returned; the filter is on the way to the model. It is inside partition 8 because the deploy
gate's purpose is that third-party text is filtered before the model, and a tool result is the
larger path. — *Reversed if* a tool's legitimate output needs a removed character (a zero-width
joiner inside an emoji the owner uses): that character is allowed back in `clean`, in one place.

**D63. Only the owner's activity keeps a session alive (amends D41).** A job's events - its
start, each tool call and result, its gate, its end - no longer move its session's `last_ts`,
and neither does the approval of a job's gate. Found on 2026-09-30: a job that gated or ended
in a session more than two hours old made that session the newest again, so the owner's next
message went into it, without a word on the screen, and the model received the old history -
the 109-turn case D41 was written to stop. D41 never named jobs; its rule for a signal turn
("only the notice it delivered is conversation") now holds for a job too. The `job_done` notice
lands in the session the screen is in, with the goal and the summary. A job's open gate is
answered by the harness from any session (D52), because the gate no longer pulls the owner back
to the session that holds it. — *Reversed if* live use shows follow-up questions about a
finished job failing for lack of the conversation that started it: then the notice carries more
of the job, not the old session.

**The owner's direction for the server (D44/D45), recorded so it is not lost:** what the server
is for is handing over tool PACKAGES that are useful as they arrive - not wiring services one by
one. Partition 8 connects single services because the secret path and the registry copy need a
real case; the server's design starts from the package.

## 2026-10-01 — The screen's masked input, and an entry's optional and default (owner's choices)

**D64. The screen's masked `/secret` input moves to `tui_secret.py`, ceiling 150** (decided
2026-10-01). `tui.py` was 392/400 with U15b, U16 and U26 still to add about 20 lines to it. The
new module holds what is the masked entry's own: its target, where a bracketed paste stands (the
marks `[200~`/`[201~`), the 50 ms drain, the one-line refusal, the store, the `/secret` arguments
and their strings. `tui.py` keeps the key loop, the escape parser, the input buffer and the switch
into and out of the mode, and calls in; it is 367 after the move. Raising the ceiling was the
other way, and D34's intent is that a module past its line is cut, not its ceiling raised;
trimming the three units would cut the intake the secret path depends on. — *Reversed if*
`tui_secret.py` ends up needing the screen's state: then the split goes back and `tui.py`'s
ceiling is raised instead.

**D65. An installed server's entry says which variables are optional and which have a default,
in its value grammar** (decided 2026-10-01). `keychain:NAME` is required, `keychain?:NAME`
optional, and after the first `=` comes the value used when the Keychain item is missing:
`keychain:NAME=<default>`, `keychain?:NAME=<default>`. The installer writes a default only for a
variable not marked secret - a secret's registry default is a placeholder, not a value - and an
empty default is none. One resolver, `mcp/servers._resolve`, reads the entry for the check and
for the start, so the two agree: a required variable with a default no longer stops the check,
and a missing optional secret no longer disables a server the check trialled without it. New
config keys (`optional`, `defaults`) were rejected because the entry would then live in two
places; keeping the grammar and reinstalling was rejected because an optional secret added later
would need a reinstall. A registry variable whose name is not an ASCII identifier is dropped
from the candidate: it could not be stored under that name anyway, and a `=` in it would read as
the grammar. Entries written before D65 keep `keychain:NAME` and stay disabled while
the item is missing; there is no migration - a reinstall is the way.

## 2026-10-01 — The pasted-token handle, and what the ledger door vaults (owner's choices)

**D66. A pasted token's handle is `pasted-<seq>[-i]`, the ledger seq of the event it came from,
and only text the owner typed is vaulted; any other text is redacted at the door** (decided
2026-10-01; amends D53). The first token in an event is `pasted-<seq>`, further ones in the same
event `pasted-<seq>-2`, `-3`, and `pasted.json` keeps only `{"pasted": {handle: created}}`. The
counter it kept before did not survive a lost or malformed index: it restarted, so an old handle
in the ledger could come to name a new value. A seq never recurs, so U15b's "expired - enter it
again" can trust that a number means one value. The service is shared by every home and a seq
is unique only within its own ledger, so an item that already holds the name (a trial home's,
or one left from before a reset) is never replaced: that token is written `[redacted]`. A sweep,
at daemon start and on a paste, also drops a handle whose item is gone; an item whose handle a
lost index listed stays in the Keychain, unknown - the accepted limit.
The vault runs only for an event with `trust: user` - the owner's chat text and steers, and a
reminder or a schedule entry, whose senses declare `user`. Every other event (a tool's call and
result, a third party's signal, a job's and the model's words, memory) passes `keychain.mask`,
so a token there becomes `[redacted]` with no Keychain call: one tool result could hold about
285 token-shaped strings, which was about 285 `security` calls under the ledger's lock and 95
items left for 24 hours. The cost accepted: a token in third-party text cannot be read back - it
never should reach the model anyway.

## 2026-10-02 — The daemon's push door (owner's choice)

**D67. The socket's `signal` branch moves to `signals.py`; the daemon keeps one delegating line**
(decided 2026-10-02). `signals.pushed(senses, msg, push, reply)` checks that the sense is loaded
and push-capable, that `fields` fits the 4 KB limit and that the sense's `build` accepts it, then
either pushes the Signal or answers one `error` line; `PUSH_FIELDS_LIMIT` moves with it.
`daemon.py` reached its 780 ceiling for the third time in partition 8: U3 and U4 moved code out
before, and U16's review fixes brought it back to 780, which f0a1b13 met only by folding a
docstring into a comment - undone here. The branch is the signal path's own responsibility: what
a pushed signal must satisfy is the senses' rule, not the socket's. The move takes 14 lines out
of the daemon; `signals.py` and `rules.py` stay inside their shared 585. Moving the `react` branch
to `loop.py` was rejected: it does not belong there, and it would free 6 lines. Raising the
ceiling was rejected: it ignores the pressure D34's per-module line exists to apply.

## 2026-10-06 — The second look and the record (owner's choice)

**D68. The second look sees the record (amends D51)** (decided 2026-10-06). `verifier_input`
carries the most recent record block of the history (D52: names, arguments and state, never
results, clipped to 1,500 characters) after the previous answer, and `VERIFIER_PROMPT` counts that
record, and a recap of the conversation, as support. Live on 2026-10-06 the model answered "the
pending call is write_file, id ..." and "yes, I called run_command with ..." correctly from the
record, but the verifier, which saw only the previous answer, the user line and "No tool was
called in this turn.", flagged both `unsupported_state`: the drafts were withdrawn and the re-runs
answered worse. "What did I just send you? quote it" was flagged too - a recap the main prompt
exempts and the verifier prompt did not. The cost: the verifier input m10 measured changes (the
prompt gains two sentences, the input up to 1,500 characters); m10' stays as measured and a smoke
of the affected cases follows. *Reversed if* the smoke or live use shows the record making the
verifier pass an unsupported answer. The record it sees is every record block of the session, newest rows last, cut to its last 1,500
characters at a row boundary: each turn has its own block, so the latest one alone loses a gate still
waiting from an earlier turn.

**D69. A call refused at the broker's door is recorded and is not a tool call (amends D51/D52)**
(decided 2026-10-06). For an unknown tool, a coercion error, a `normalize` error and a policy
`deny`, `Broker.call` now emits `tool_call` and then `tool_result` with `refused: True` (a policy
deny also `denied: True`) and returns `refused: True`; the record row ends ` refused` (` denied`
for policy). `loop.run` counts a step as a tool run only when one of its settled calls was not
refused, so a turn whose every call was refused is still checked by the second look. Live (10b): a
turn of two steps, no tool event, no check - `Broker.call` returned before `emit` while the loop
set `called` for any step with tool_calls, so the answer went out unchecked and the record never
showed the attempt. The gate path and the user-denied path are unchanged. The cost: two more
ledger events per refused attempt; the record shows the model's misses. *Reversed if* refused
rows crowd the record (`CALL_ARGS_CLIP` already bounds each).
A policy deny still counts as a tool call for the second look, because the verifier does not see
this turn's refusals and would flag a truthful "your settings block it" as `false_incapability`.

**D70. A stopped registry sync tries once more after ten minutes, and a run that has begun clears
the failure note** (decided 2026-10-06, from live check (1)). A page that fails after its own
retries stops the run (`failed_at`, `error`); before, the next attempt came only with a
`search_tools` call or the catalog sense's 24 h poll, so a registry timeout at 339 s would have
left the copy at 700 rows for a day without a search. Now `_sync` schedules one `refresh` after
`RETRY_AFTER_FAILURE_S` (600 s) on a daemon timer, and a retry that fails schedules nothing more -
the search or the poll takes over. `_run` clears `failed_at` and `error` as it starts, so
`search`'s note says "still syncing (N so far)" while a resumed run fills, not "failed at HH:MM".
Cost: one more request ten minutes after a failure; `mirror.py` 196/200. — *Reversed if* a
registry outage makes the retry itself a cost (it is one page), or if live use shows the copy
still stale for hours: then the poll's cadence shortens after a failure instead.

## 2026-10-11 — Seventeen days of real use (owner's choices on four questions)

**D71. A due schedule runs as the owner's own turn, and `add_schedule` asks (amends D20, D22)**
(decided 2026-10-11). `Daemon.push_signal` records a `schedule` sense's signal and checks its
24 h dedupe as before, then queues `("say", "[scheduled] <text>")` on the target session instead
of a signal turn. The turn is a user turn: its Gate is stored as an approval and resumed on
`/approve` like any other (D16), where a signal turn's Gate was dropped and only the note's text
went out. The owner wrote the text, so user trust is right; the cost is that a schedule bypasses
RULES.md and D20's quiet hours, daily budget and burst rules, and the shipped `## schedule_due`
rule row is now dead. When no screen is attached, the turn's outcome goes out through the outbox
as a notice (a banner), `[scheduled] <note> (answer|gated)`, and the answer waits in the
session's history (D41: a screen that opens follows the notice). Because that turn carries user
trust, `add_schedule` loses D22's `reversible` and asks in a user turn too: a schedule the model
registers while it handles someone else's text must not later run with the owner's trust
(`remove_schedule` stays reversible). Live: two daily schedule turns ended `gated` in 2 s and
delivered the bare note; the briefing the owner asked for never ran. — *Reversed if* scheduled
turns are cut or misread as owner chat (`chat_approval`, steers) in live use, or an unattended
`gated` outcome is left for hours: then the signal turn gains its own resume path (a daemon
module split first, `daemon.py` 775/780).

**D72. A draft that writes out the tool-call record is a forged record: withdrawn in the open
and re-run once, a second one cut at the header** (decided 2026-10-11; a harness guard, D52's
record unchanged). `loop.run` checks every final draft, re-runs included and before the second
look, for `RECORD_HEADER`. The first, with a step left, is withdrawn through the same path as a
flagged draft (`retract`, flags `["forged_record"]`, `RERUN_NOTE`); a second is cut at the
header, and the cut reaches the ledger and the answer, while the text already streamed stays on
the screen (no second `retract`). `FLAG_WORDS`, the verifier prompt and `system.txt` stay as
measured (D51, D52, D68): `FORGED_WHAT`/`FORGED_WHY` is a separate constant, so
`test_second_look_matches_the_bench` and m10' stand. Moving the record out of its own system
message was rejected: it would void m10' and D68's smoke and spend credits on a re-run. Live:
5 drafts in 17 days wrote out the record header; 1 reached the owner as the answer. — *Reversed
if* the guard withdraws a correct answer that quotes the record on request, or forged drafts
persist after the re-run: then the record's role and placement is a measured change.

**D73. Senses are turned on and off one at a time** (decided 2026-10-11). `pathfinder config set
senses.<name> '<json>'|off` loads the current `senses`, sets or drops that one key and writes the
whole value through `config.set("senses", …)` - still the only config writer; `pathfinder config
get [key]` prints the current value as JSON; a `ConfigError` prints one line and exits 1, not a
traceback. `config set senses '<json>'` still replaces the whole value, and the `connect-gmail`
skill, `init`'s announced lines, `docs/senses.md` and the READMEs no longer tell anyone to use
it. Live: on 2026-10-09 the model replaced the whole dict while connecting Gmail, and seven
senses went silent for two days unnoticed. Making `config set senses` itself merge was rejected:
it would leave no way to switch one off and break "set = replace". — *Reversed if* the dotted
form needs a second key family (another dict-valued key turned on piecewise): then one general
`config set KEY.SUB` rule instead of a `senses.`-only branch.

**D74. The tool door says why** (decided 2026-10-11). A call to an unknown tool that belongs to
a sense that is off is refused as `no such tool: X (the <sense> sense is off)`: `tools.OFF_TOOLS`,
filled by `signals.load` through `signals.sense_modules()` (the one scan of the senses package,
`memory._rule_vocab` uses it too). `run_command`'s `normalize` refuses, before the gate, a tool's
name used as a shell command and any restart or stop of PATHFINDER's own daemon (it ends the
turn with no answer; the owner runs it after the turn) - a convenience, not a security boundary.
A sense that keeps raising the same error is logged once, and its next clean poll once as
`Recovered` (`signals.Recovered`, recorded as an `error` event `sense <name>: Recovered: polling
again`, so error counts include recoveries); the daemon is unchanged, and calendar and reminders
drop their own `_off_logged` flag. Live: 16 `no such tool` refusals while seven senses were off
and the model diagnosed permissions; 144 of 158 error records were one mail `gaierror`; 2 turns
were cut by an in-turn restart. — *Reversed if* a refusal blocks a command the owner meant to
run (then the pattern narrows), or a sense that fails and recovers by turns
fills the ledger with error and `Recovered` pairs.
