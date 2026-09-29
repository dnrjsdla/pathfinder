# Partition 8 — trust in the answer and the security bundle: the rewritten prompt and a second look, a record of what was done, the registry copy, the split memory pass, secrets only through the Keychain, a private home

## Question for the owner (still open; the units that depend on it are marked [Q1])

**Q1. A token pasted into the chat anyway.**

What already exists does not depend on Q1:
- the intake (`/secret` in the screen, `pathfinder secret` in a shell);
- D40's stop before the trial;
- the system prompt's line that a secret never goes through the chat.

Q1 is what happens when a token is pasted anyway (it was on 2026-09-26). Encrypting the token
before the model sees it does not help. The model cannot use ciphertext, so ciphertext is only a
longer handle, and the key would sit on the same machine as the Keychain it would stand in for.
The real choices are these three.

**(a) Redact at the ledger door.**
- **Mechanism:** `ledger.append` writes `[redacted]` over a token-shaped substring and returns
  the redacted event, so `state`, the history and every later turn see `[redacted]`.
- **Lines:** `ledger.py` +4. The scanner's `api-key`/`private-key` patterns move into
  `keychain.TOKEN_RE` (+5), and `scripts/scan_personal.py` imports it, so there is one
  definition.
- **What it throws away:**
  - A false positive (a long string that only starts like a token) is rewritten in the truth for
    good.
  - A real token is gone too, so the owner enters it again through `/secret`.
  - Within the same turn, the model still sees a steer, a signal envelope or a tool result raw,
    because those reach it beside the ledger, not through it.
- **What the owner sees:** their line on their own screen as typed. The answer refers to
  `[redacted]`.

**(b) Vault and handle.**
- **Mechanism:**
  - At the same door, the substring goes into the Keychain as service `pathfinder-pasted`,
    account `pasted-N`, and the text carries `[secret:pasted-N]` instead.
  - `pathfinder secret <id> NAME --from pasted-N` and `/secret <id> NAME --from pasted-N` move the
    value to `pathfinder-mcp-<id>`/`NAME` and delete the pasted item. D40's stop line offers a
    handle when one exists.
  - Expiry: an index `<home>/pasted.json` (0600) holds handles and creation times, never values.
    Every new paste, and every daemon start, deletes pasted items older than 24 hours. Binding an
    expired handle says "expired - enter it again with /secret".
  - A failing Keychain write (a locked keychain, no `security` binary) falls back to (a)'s
    redaction, so the append still lands.
- **Lines:** `ledger.py` +8, `keychain.py` +~35 (vault, bind, sweep, index), `__main__` +4,
  `tui.py` +3, plus the same pattern move.
- **What it throws away:**
  - The one ledger writer now calls a subprocess under its lock (about 50 ms, only for events
    that hold a token).
  - One more file under the home.
  - A false positive can be read back (`security find-generic-password -s pathfinder-pasted -a pasted-N -w`)
    for 24 hours only; after that it is gone.
- **What the owner sees:** the answer says "you pasted a token; it is stored as pasted-1: bind it
  with /secret <id> NAME --from pasted-1".

**(c) The assistant asks through the harness,** with (a) or (b) as the backstop for a paste.
- **Mechanism:**
  - When D40's check stops on a missing required secret, `Daemon.record` sees
    `install_failed {step: "secrets"}` and sends every attached screen
    `secret.ask {server_id, name, server}`.
  - The screen switches to the masked input `/secret` already uses:
    "NOTION_TOKEN for easy-notion (hidden; stored in the Keychain, never shown to the
    assistant)".
  - Enter sends `secret {server_id, name, value}` over the 0600 socket. The daemon stores it with
    `keychain.store` on a worker thread, writes no ledger event holding the value, answers
    `secret.stored`, and runs `installer.propose_by_name(server)` again. The proposal's gate line
    follows.
  - Neither the model nor the ledger ever holds the value. `pathfinder say` has no screen and
    gets the `/secret` line instead.
- **Lines:** daemon +~9 (the `record` hook, the `secret` handler), `tui.py` +~12, `installer.py`
  +~2, tests ~80, plus the backstop.
- **What it throws away:**
  - A secret value now crosses a daemon handler. A future bug that logs a message there would
    leak it; `test_secret_value_never_reaches_ledger_or_state` guards it.
  - Two new socket message types.
- **What the owner sees:** "Notion needs a token", the prompt appearing right away, a paste into
  it, then the proposal with `/approve`. No command to learn.

**Recommended: (c) with (a) as the backstop.**
- (c) turns the moment the owner would paste into a masked prompt, before the paste happens. That
  is D8's one step ahead.
- (a) catches what still slips, in four lines, at the one door every path passes through.
- (b)'s only gain over (a) is keeping a pasted real token. With (c) that is one paste into a
  masked prompt, and (b) makes the ledger writer depend on the Keychain and adds an index with an
  expiry sweep.

---

**"It runs" =** each of the following is checked live. [Q1] marks a check that depends on Q1.
- (1) **The first sync.** A temp home with the catalog sense on. The socket answers `say` at
  once while the registry copy fills in the background. `search_tools` during the first sync
  answers from the partial copy with a line saying so. The first sync's time, row count and file
  size go into HISTORY (m9: 462 s, 37,181 servers, 33.6 MB raw). A `service restart` mid-sync
  resumes from the stored cursor. A `service stop` mid-sync returns inside launchd's timeout and
  leaves no orphaned MCP child.
- (2) **Search.** m9's failed and empty queries each answer in under a second with real server
  names: "brave search", "google mail gmail", "web search", "weather", "gmail". `propose_install`
  on one of them proposes. A name deleted from the copy by hand proposes through the exact-name
  lookup.
- (3) **Later syncs and failures.** The next start runs an incremental sync of a few pages. With
  the network pulled mid-sync, `search_tools` says "last sync failed at HH:MM", and the first
  search after the network returns restarts the sync.
- (4) **The intake.** `/secret <id> NAME` shows only `*`. A second client attached with `listen`
  receives nothing. No file under `events/` contains the value. `security find-generic-password`
  returns it. `pathfinder secret` does the same from a shell. Use a throwaway service and delete
  it after.
- (5) **D40.** Use the Brave search server (npm, MIT, `BRAVE_API_KEY` required and secret), or
  `easy-notion`. `propose_install` with the key missing answers with the `/secret` line, and no
  trial ran. After the key is stored, the same request trials and proposes; approve, and
  `pathfinder tools` lists the server enabled. [Q1 (c)]: the masked prompt appears unasked, and
  the proposal follows once the value is entered.
- (6) **[Q1] A token-shaped string typed at the prompt.** (a): `[redacted]` in the events file,
  and the model never saw the value. (b): `[secret:pasted-1]` in the file, and the value sits in
  the Keychain.
- (7) **The private home.** Start the new daemon on a copy of a pre-partition home. Afterwards
  `find <home> -path <home>/workspace -prune -o -perm +077 -print` prints nothing. A partition
  file, `registry.db` and `state.db-wal` born during the run are 0600.
- (8) **The split pass.** m9's replay (U18) writes at least one edit per slice in each of two
  runs, and a standing instruction inside a slice becomes a USER.md proposal. Live: a forced
  `memory run` in a seeded temp home leaves a USER.md proposal and a `session_summary`.
- (9) **The small defects** on a screen moved into a new session (D41's aged-session check). A
  notice that arrives while the owner types is held (5a). A job from the old session shows its
  gate and end lines (5b). A gate line longer than 300 characters is clipped (5d).
- (10) **The second look (D51).**
  - A greeting answered without a tool records `check: passed` on `turn_end`. The added wait
    before `turn.end` goes into HISTORY.
  - Through the real screen in a pty, with a scripted step, a draft that states the reminders
    with no tool prints; then the withdrawal line prints; then the re-run's answer streams below
    it. The ledger holds the draft as `assistant {final: false, withdrawn}`, and
    `state.history()` holds only the re-run.
  - A verifier that returns prose instead of JSON leaves the draft standing, with
    `check: failed`.
- (11) **The record (D52).** After a turn that called `search_tools`, "did you call search_tools
  just now?" is answered yes, from the record line in history (m10's T112). No tool result text
  appears in the history.
- (12) **Approval typed in chat (D52).** With an install proposal open, typing "승인", "I'll
  approve" or "yes" gets the harness's `/approve <id>` line. The ledger shows the `user` line and
  the harness's `assistant` line, and no model request (no usage on that `turn_end`). The approval
  is still pending. "yes, and also add X" goes to the model.
- (13) **The exit measurement.** `bench/m10_unsupported.py` re-runs on the adopted prompt, with
  the second look and with the record in the replayed history (U18). Pass: at least 22 of 27
  fixed, 0 of 10 controls pushed into a tool (T103 and T136 were m10's two), 0 normal turns
  broken, no control flagged. About 215,000 tokens (m10's prompt+verify cost); the lead's go-ahead
  first.
- (14) **The docs.** The ARCHITECTURE pair opens with partition 8 and keeps line counts in §13
  only. The README pair names `/secret`.

Decisions this partition rests on:
- D8: the exact-name fallback, the resumable sync, and the masked prompt of Q1 (c).
- D16: a gate ends the turn, so a `say` after a gate is a new turn and not a steer.
- D17: steers are never intercepted.
- D25: the second look waits in the slot like any user request.
- D28: the pass's standing instructions go to the approval box.
- D29: the first system message changes once, then stays byte-stable.
- D30: the final step leaves the write call; a second look needs a step left.
- D32: the license check is unchanged.
- D39: the write call keeps its slice on an outage.
- D40: the secret comes before the trial. Its survey (U0) found 4 of the owner's 7 services take a
  static token, so the reversal is not met.
- D41: screens move between sessions, hence 5a and 5b.
- D43: the scope; m9 was the entrance.
- D44/D45/D46: the registry copy is one separable module, the future server's indexer.
- D47: the two-call pass.
- D49: the count rule below.
- D50: the old token stays.
- D51: the prompt and the second look.
- D52: the record, and approvals typed in chat.

NOT in this partition:
- **Correcting a session summary that asserts something false** (D52 leaves it for later; D41
  already ends a session after two quiet hours).
- **Tool results in history.** The record keeps names and arguments only.
- **A second look for signal turns, jobs and the memory pass.** Their text is not an answer to
  the owner.
- **Buffering answers** so a draft is never seen: it would hide streaming for half of all turns.
- m3 re-run over the adopted prompt and the agenda (credits; the lead decides).
- The discovery server (D44/D45).
- **Google (Gmail, Google Calendar) and every OAuth remote.** Google has no static token, and the
  official Notion and Slack remotes are OAuth too (U0). Notion and Slack arrive through their
  token servers: community Notion servers, and Slack with a bot token. Header secrets of remote
  servers are also out.
- A live reload of an installed server after `/secret`: a restart does it.
- `/secret` for the mail password.
- A live registry search while the first sync runs.
- The screen's redesign.
- The shell sense announcing `pathfinder tui`.
- The model's wrong times.
- `presence` volume.
- No new config key, sense option, RULES.md knob or ledger type. The ledger gains fields only:
  `assistant.withdrawn`, `turn_end.check`. One new server→client line: `retract`. [Q1 (c)] adds
  `secret.ask`, `secret` and `secret.stored`.

The token of 2026-09-26 (D50): the owner revoked it on 2026-09-29, so every copy is inert. Those
copies are the ledger, `state.db`, the other app's config file, backups, and what the cloud model
received. The ledger stays append-only. A secret that cannot be revoked would get a one-time
offline scrub by the owner's hand, from `scripts/`, never a product command.

**Line budgets.**

The count rule (D49): the core is the blueprint's §6 rows: ledger, state, daemon, loop, broker,
policy, exec, tools, signals, rules, deliver, jobs, memory. That is **3,762 lines** today and
**~3,880** after this partition. The adapter layer (`llm/`, `senses/`, catalog, installer, mirror)
and the infrastructure (`config`, `clock`, `keychain`, `init`, `__main__`) sit outside it and keep
their own ceilings.

The gate is the per-module ceiling in `test_invariants.py`. Of the 13 core rows, only daemon,
memory and jobs have one today, so U17 adds the 1.3x line for the other ten.

Counts are `splitlines()` at c749e17:

| Module | Today | Ceiling | After this plan |
|---|---|---|---|
| `daemon.py` | **779** | 780 | **~759**; [Q1 c] ~768 |
| `loop.py` | 197 | 520 (1.3 × 400, added) | ~242 |
| `state.py` | 625 | 780 (1.3 × 600, added) | ~652 |
| `broker.py` + `policy.py` | 84 + 57 | 455 (added) | ~110 + 57 |
| `ledger.py` | 122 | 520 (added) | 122; [Q1 a] 126 / [Q1 b] 130 |
| `memory.py` | 695 | 780 | ~715 |
| `jobs.py` | 267 | 350 | ~282 |
| `signals.py` + `rules.py` | 201 + 220 | 585 (added) | unchanged |
| `exec.py`, `tools.py`, `deliver.py` | 217, 212, 86 | 390, 390, 325 (added) | unchanged |
| `installer.py` | 674 | 700 | ~689; [Q1 c] ~691 |
| `catalog.py` | 243 | 300 | ~248 |
| `senses/catalog.py` | **141** | 150 | ≤ 145 |
| `mirror.py` | new | 150 (new) | ~130 |
| `tui.py` | 314 | 400 | ~342; [Q1 b/c] ~345-355 |
| `init.py` | **297** | 300 | 297 |
| `keychain.py`, `config.py`, `__main__.py` | 14, 169, 455 | none | ~41 [Q1 b ~76], ~179, ~472 |

463 offline tests today.

## Entrance and exit measurements

The entrances are m9 (D43) and m10 (D51, D52). There are two exits, both needing the lead's
go-ahead for credits:
- m10 re-run on the adopted prompt with the record and the second look: (13), about 215,000
  tokens.
- m9's memory replay on the split pass: (8), about 40,000 tokens.

m10 replayed the last 12 messages of each session. Live sends the whole session, and the re-run
inherits that simplification.

## pathfinder/daemon.py — what moves out first (779/780)

Three items want daemon lines: the second look's wiring (+4), approvals typed in chat (+7), and
[Q1 c]'s `secret` handler (+9). Two moves come first. Each moves protocol code to the module that
owns the protocol, the way `PROPOSALS` already routes approvals to their owners.

**Move 1 (U3): `install`/`uninstall` to `Installer.handle(msg) -> reply`.**
- These are lines 366-388: 23 lines of validation, `to_thread(propose_by_name)` with its
  exception guard, and the error/reply shaping.
- In the daemon they become two lines: `elif kind in ("install", "uninstall"):` and
  `_write(writer, await self.installer.handle(msg))`.
- **−21 in the daemon; `installer.py` +15.**

**Move 2 (U4): `_job_record` into `jobs.Manager.record`.**
- These are lines 166-180: 15 lines. The Manager records, then builds the `job.start`,
  `job.gate` and `job.end` line from the same fields and calls a `send` callback (default no-op).
- The daemon gains `_send_all(msg)` (3 lines + blank): every connection of every session, so a
  job's line reaches a screen D41 moved away (5b). Line 117 wraps to pass `self.record` and
  `send=self._send_all`.
- **Net −10; `jobs.py` +15.**

**Then:**

| Step | Change | Count |
|---|---|---|
| Start | | 779 |
| Move 1 | −21 | 758 |
| Move 2 | −10 | 748 |
| D52's chat approval in `turn()` | +7 | 755 |
| D51's wiring in `_run_turn` | +4 | 759 |
| [Q1 c] | +9 | 768 |

5a edits line 573 in place.

Nothing else moves. The lane, the signal turns and the rest of `handle()` depend on daemon state,
and splitting them would add a seam only to buy lines.

## The second look and the adopted prompt (D51)

**The prompt (U8).** `pathfinder/prompts/system.txt` becomes `bench/m10/system.txt`, which m10
measured (21 of 27 fixed), with four changes:
1. **The narrowing** (m10's two failed controls: T103 "is your self-check working?" and T136
   "summarize our conversation"). The state paragraph gains:

   > "This is about the world outside this conversation: a question about the conversation
   > itself - a recap, a summary, what you said or did earlier, how you work - is answered from
   > the conversation and the record of your tool calls, without a new tool call."
2. **The record line (D52):**

   > "Before your answers, the system notes the tools you called in earlier turns - names and
   > arguments from the ledger, never their results. Use it to say what you did; it is not the
   > current state."
3. **The secret line:** never ask for a token, key or password in chat; a secret goes in with
   `/secret <server> <NAME>`, and you never see it.
4. **Standing instructions:** USER.md holds the user's standing instructions; follow them in
   every turn.

m3 read the old `system.txt` and is not re-run (see NOT). The adopted text is what (13)
measures.

**Where the second look attaches (U7).** At the answer exit of `loop.run`, the
`if not tool_calls:` branch. It is switched on by a new keyword, `second_look=None`, a list of
tool names.
- **The caller.** The daemon's `_run_turn` passes `second_look=list(user_tools)`. That covers
  user turns, `continue` turns and resumes. A resume always holds a tool result, so it is never
  checked. Signal turns, jobs and the pass never pass the keyword.
- **The verifier's step** is the `final_step` the caller already hands over: the same model, no
  tools, the slot at priority `user`.
- **It runs only when all hold:**
  - the draft is not empty;
  - no `tool` message follows the turn's last `user` message;
  - a step of the budget is still left for a re-run;
  - no second look has run in this turn yet.

  At the last step the draft stands. D30's tool-less final step is never checked.

**What the verifier is sent.** These are bench m10's texts, copied verbatim and held
byte-identical by `test_invariants`, as m3's envelope is.
- **System message:** `VERIFIER_PROMPT`.
- **User message:** the parts of `verifier_input`, joined by blank lines:
  - `Tools: <every tool name offered this turn, comma-separated>`. It must see the names:
    `false_incapability` is judged against them.
  - `Agenda the system gave this turn:` and the second system message's paragraph that starts
    with `signals.AGENDA_HEADER`, when present.
  - `The assistant's previous answer:` and the last `assistant` message before the turn's `user`
    message, clipped to 600 characters.
  - `The user:` and that message, clipped to 1,000 characters.
  - `No tool was called in this turn.`
  - `Draft:` and the whole draft.
- It never reads a tool-backed answer. In m10's smoke, shown clipped results, it flagged 2 of 2
  supported answers.

**The answer is closed JSON:**
`{"unsupported_state", "unsupported_done", "empty_promise", "false_incapability": true|false, "why": "..."}`.
It is parsed from the first `{` to the last `}`. The flags are the keys that are exactly `true`.
- **Any failure lets the draft stand, with `check: "failed"` and no re-run:** unparseable JSON,
  an `LLMError`, any exception inside the verifier call (caught there alone), or a `cancelled`
  from an interrupt or a steer cut.
- **No flag** gives `check: "passed"`.
- **A flag** gives `check: "flagged"` and the re-run below.

**The streamed draft.** The deltas have already reached the screen, and a scrollback client
cannot erase them (D35: no cursor movement). So the draft is **withdrawn in the open**, never
silently replaced.
- **In the loop:**
  1. The loop drops the draft from `messages` and from `said`. `said` is what becomes the final
     `assistant`; this is a deliberate exception to "what was streamed reaches history".
  2. It emits `("retract", {"draft", "what", "why"})`.
  3. It appends `RERUN_NOTE` as a `system` message, as m10 did: "[Your draft answer was not shown
     to the user: it {what}, and no tool result in this turn supports it ({why}). Call the tool
     now, or say plainly that you cannot.]"
  4. It continues. The re-run is an ordinary step with tools: it counts against `max_steps` and
     may call tools, gate, or answer.
- **One re-run at most.** Its answer is not checked again.
- **Budget.** The verifier request is the harness's own call, like D30's final step: it is not a
  step, but its usage is added to the turn's usage.
- **In the daemon:** the `retract` branch records the draft as
  `assistant {final: false, withdrawn: {flags, why}}`. The ledger keeps what the owner saw; it
  never enters history, because only `final` assistants do. The branch then sends
  `{"type": "retract", "what", "why"}`. `turn_end` carries `check`. The withdrawn event is
  recorded first, so a re-run that gates loses nothing.
- **What the owner sees:**
  - **Passed** (most turns): nothing more. `turn.end` arrives after the verifier call, about one
    to three seconds (measured in (10)). Keys typed meanwhile stay in the buffer, as during any
    stream.
  - **Flagged:**
    1. The draft stays above.
    2. A dim line under it: "철회 · withdrawn: the answer above said what the reminders hold with
       no tool result in this turn - checking with the tools".
    3. The re-run streams below.
  - **Other clients:** `pathfinder say` prints the same line to stderr (`_stream`); `listen` goes
    through `tui.render`.

**Cost.** In m10, both fixes together took +21 calls over 40 turns (about +0.5 per user turn) and
about +800 tokens per turn on first steps.

**Lines.** `loop.py` +~45 (the constants ~20, the input builder, the parse, the flow), daemon +4,
`tui.py` and `__main__` +2 each.

**D51's reversal needs its denominator in the ledger:** `turn_end.check` (checked) and
`assistant.withdrawn` (flagged).

## History carries a record of the tool calls (D52, part 1)

**What is kept per call.** Only calls that ran are kept. A call that ran is a `tool_call` event,
which the broker emits only after policy allowed it or the owner approved it.
- **The line:** `name {args}` then ` ok` or ` failed`, with `name`, the compact JSON of `args`
  (`separators=(",", ":")`, `ensure_ascii=False`) clipped to 120 characters, and `ok` from the
  `tool_result` that immediately follows.
- **Never kept:** the result.
- **Only user-origin turns** are kept. A signal turn's notice is its own record.

**Where it is built: `state.py`, not the daemon.**
- `_project` inserts a `messages` row with role `calls` on a user-origin `tool_call`. It appends
  ` ok`/` failed` on the `tool_result`, updating the session's latest `calls` row; calls run one
  at a time in `settle`.
- `history()` folds consecutive `calls` rows into one `system` message before that turn's
  answer: "Tools you called here (from the ledger; names and arguments, not results): ...".
- A summary's `upto` drops old rows like any message.
- `session_messages` and `nth_newest_message_seq` gain `role IN ('user', 'assistant')`, so the
  summary input and its keep-the-last-four boundary do not move. `last_user_*` already filter.
- `reindex` rebuilds the rows for the past too.
- **~+20 lines.**

**Cost, measured on the live ledger** (2026-09-25..29: 137 user turns, 104 calls in 65 of them,
median arguments 30 characters, p90 59). Taken at each user turn's start with summaries applied,
the record adds:

| | Characters | Share of the prompt's messages (tool schemas excluded) |
|---|---|---|
| Median | 720 (about 200-250 tokens) | 7% |
| p90 | 1,247 | 13% |
| Maximum | 1,788 | 16% |

That is under D52's one-fifth line. Names alone would be 2.7% / 4.9% / 5.4%: the reversal's
fallback.

**A caveat.** The Anthropic adapter (experimental) folds every `system` message into its
top-level `system`, so there the record loses its position. The rerun note does too.

**This changes a blueprint invariant** (see "disagree").

## An approval typed into chat is answered by the harness (D52, part 2)

**How a gated turn differs from D17.**
- D17's steer applies while the owner's own turn is *running*.
- A gate *ends* the turn (D16: `turn_end {kind: "gated"}`, `session.control` is None), so a
  `say` after it starts a new turn from the lane.
- That turn is where the check sits: first thing in `Daemon.turn()`, before the `user` line is
  recorded. A steer is never touched.

**`broker.chat_approval(state, session, text) -> reply | None`** (~+26 lines):
- **An open gate:** `state.open_approvals(session)`, which is pending rows of this session whose
  `ts` is later than the session's last `user` message. This covers:
  - broker gates;
  - proposals the owner's own turn queued (`install_server`, `edit_memory`; T68 was an install);
  - a job's gate in this session.

  A gate from yesterday that has been talked past does not count.
- **The match is narrow: the whole message.**
  - Normalization: NFKC, lowercase, surrounding whitespace and trailing `.!~` stripped, spaces
    collapsed.
  - The normalized message must be one of a closed tuple of about 35 phrases:
    - approve: 승인, 승인해, 승인해줘, 승인합니다, 승인할게, 승인할게요, 허락, 허용, 진행해, 진행해줘, 네, 예, 응, ㅇㅇ,
      좋아, 그래, ok, okay, yes, y, approve, approved, i approve, i'll approve, go ahead, do it;
    - deny: 거부, 거절, 취소, 아니, 아니요, no, n, deny, cancel.
  - Anything longer ("yes, and also add X") goes to the model, which the adopted prompt tells
    that approval happens with `/approve`.
- **The reply is code-assembled and bilingual**, one line per open gate:
  "승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve <id> or /deny <id> (<tool>)".

**In `turn()`** (+7 lines):
- The daemon records the `user` line as usual, then `turn_start`.
- It sends `turn.start` and the reply as one `delta`, records it as `assistant` (final), records
  `turn_end {kind: "answer"}` with no usage, and returns. The lane sends `turn.end`.
- No model request is made. The history stays coherent, and the approval is still pending.
- The same line serves a deny word, because a denial also needs `/deny`.

`state.open_approvals` is ~+6 lines.

## pathfinder/mirror.py — the registry copy (new, <= 150 lines, D46)

**Where it lives.** `<home>/registry.db`, born 0600. It is not a table in `state.db`: that file
is the ledger's projection, and this one is the registry's, which D46 deletes when the server
lands. It imports only the standard library, `catalog` (`page`, `latest`) and `clock`. Only
`senses/catalog.py`, `installer.py` and `__main__` import it.

**Schema.**
```
servers(id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, status TEXT,
        updated_at TEXT, seen REAL, item TEXT)        -- item: the registry's JSON as served
servers_fts USING fts5(name, description, content='servers', content_rowid='id')
after-insert / after-delete triggers keep servers_fts in step; an upsert is delete-then-insert
meta(key, value): registry, cursor, complete, since, started_at, synced_at, failed_at, error
```
FTS5's tokenizer splits names on `.`, `/` and `-`, which is why "brave search" finds
`x/brave-search-mcp`. The query is built like `state.search`: prefix terms joined with OR, ranked
by bm25 with the name weighted over the description, `status = 'active'` only.

**The surface:**
- `refresh(home, registry)`: starts the sync on a daemon thread unless one is alive for this
  home, then returns at once.
- `search(home, query, registry=None, limit=10) -> (items, note)`.
  - `note` is `""` for a complete, healthy copy, else one sentence: still syncing (N so far,
    since HH:MM); empty (about eight minutes); or last sync failed at HH:MM, with the reason.
  - With `registry` given (the tool's call) and the copy incomplete or failed, it calls `refresh`
    first.
- `find(home, name) -> list`: the exact name, else every name ending in `/name`.
- `changed_since(home, ts) -> list`: rows an incremental sync wrote after `ts`.

**The sync:**
- **First sync.** `version=latest`, 100 per page, from the stored `cursor`. The registry's cursor
  is `name:version`, keyset pagination, so it resumes after a restart (seen 2026-09-29). Each page
  is one transaction: its upserts with `seen = 0`, plus the new cursor. At the end it sets
  `complete = 1` and `since = started_at - 3600`.
- **Incremental sync.** The same pages with `updated_since = since` (`catalog.rfc3339`) and
  `seen = now`. `since` moves only when the sync completes.
- **Status** comes from `_meta."io.modelcontextprotocol.registry/official".status` (U0).
  - The default listing omits `deleted` rows, so the first sync never sees them.
  - A status change moves `updatedAt`, and a listing with `updated_since` includes deleted rows.
    An incremental sync therefore receives a deletion as a row, and the copy drops that row on
    receipt.
  - `deprecated` rows (403) stay in the copy but out of `search` and out of candidates. `find`
    still returns one for an explicit name, and `propose_by_name`'s reply says "deprecated in the
    registry".
- **Paging facts (U0).**
  - `updated_since` takes RFC3339 only; a bare date is a 400.
  - The maximum `limit` is 100 (500 is a 422).
  - Rows are ordered by name, the cursor is `name:version`, and the same cursor returns the same
    page.
  - There is no documented rate limit: 372 requests in 517 s drew no 429.
  - A full crawl was 37,189 servers.
- **Failure.**
  - A failing page is retried after 10 s and 60 s. Then `error` and `failed_at` are stored, one
    WARNING goes to `daemon.log`, and the thread ends.
  - The cursor or `since` sits where the last good page left it, and the next `refresh` goes on
    from there.
  - A 4xx on a stored cursor drops the cursor and starts the first sync over. Upserts are
    idempotent, so this is safe.
- **Another registry.** A different `registry` empties the copy (D45's reversal door).
- **Shutdown.** The thread is a daemon thread that nothing waits for. Inside `SenseLoop`, a
  462-second first sync would be awaited by `stop()`, which would hold `service stop` past
  launchd's timeout: SIGKILL, and orphaned MCP children. A page cut off mid-transaction rolls
  back.
- **The CLI** never syncs; it only reads.

**Lifting it out (D44/D45).** This module is the server's indexer as it stands. `search_tools` is
deleted (D45), and `find` becomes the server's lookup.

## pathfinder/catalog.py (243 -> ~248) and senses/catalog.py (141 -> ≤ 145)

**`catalog.fetch()`** loses its last caller. In its place:
- `page(registry, cursor=None, since=None, limit=100, timeout=30) -> (items, next_cursor)`;
- `latest(name, registry, timeout=20) -> item | None`: `GET /v0.1/servers/<quoted name>/versions/latest`,
  which answered 200 in 0.7 s on 2026-09-29;
- `rfc3339(ts)`: the gmtime formatting, moved out of `fetch`.

**The sense.**
- **`poll`** calls `mirror.refresh(home, registry)`, then runs the candidate pass on
  `mirror.changed_since(home, since)` with the same `since` rule and filters as today.
  First-sync rows (`seen = 0`) never become candidates.
- **`_search_tools`** calls `mirror.search(HOME, query, REGISTRY_URL or catalog.REGISTRY)`. It
  takes any words, and the hyphenation workaround goes. It returns the same lines as today plus
  the note, and "no server in the registry copy matches ..." when nothing matches. The
  description and the query parameter say "names and descriptions" and give "gmail, brave
  search, weather" as examples.
- **Left as is:** `cadence_h` in `OPTIONS` sets only the first `since`, which predates this
  partition.

## pathfinder/installer.py (674 -> ~689)

- **`handle(msg)`** (+15): move 1 above.
- **`propose_by_name`** (−6):
  - It uses `mirror.find(self.home, name)` with today's exact, suffix and ambiguous logic.
  - When nothing is found, it makes one `catalog.latest(name, registry)` call. That covers a
    server newer than the last refresh, or a first sync still running.
- **`check` step 4, D40** (+5).
  - Every env variable that is secret **or required** resolves from `pathfinder-mcp-<id>`.
  - The registry omits `isSecret` when it is false. 54 of 3,660 required credential-named
    variables (1.5%, 37 servers) are not marked secret; Slack's token is one of them. The rule
    catches those, at no cost to the rest.
  - `env_from_keychain` takes the same set.
  - A missing required one stops the check **before the trial**:
    `fail("secrets", "needs NAME - store it with /secret <id> NAME in the screen, or pathfinder secret <id> NAME, then ask again; the value never goes through the chat")`.
  - A missing optional secret still runs the trial, and the row shows `needs:`.
  - A required variable with a registry `default` is still asked for. Only 81 of 6,131 carry one
    (U0), so carrying `default` is not worth a field.
- **A deprecated row found by name** is proposed with "deprecated in the registry" in the reply
  (+1).

## The secret intake — `keychain.store`, `pathfinder secret`, `/secret` (independent of Q1)

**`keychain.store(service, account, value, run=subprocess.run)`** (+15). U0 measured every rule
below:
- **It uses `security -i` on stdin.** It writes exactly one command:
  `add-generic-password -U -s "<service>" -a "<account>" -w "<value>"`. `ps` then shows only
  `security -i`.
- **It escapes `\` first, then `"`.** Inside the quotes, spaces, `'`, `#`, `$` and `;` are stored
  as typed, and `\"` and `\\` round-trip exactly. An *unescaped* `"` silently truncates the value
  and still exits 0: the dangerous case.
- **It refuses only a newline and NUL.** A real newline would split the value into a second
  command.
- **It checks the return code.** A single failing command exits nonzero: 45 for a duplicate
  without `-U`, 2 for bad arguments, 1 for an unknown command.
- **It reads the value back through `secret()` and compares.** A mismatch raises.

**`pathfinder secret ID NAME`** (`__main__`, +12):
- Checks ID against config's server-id rule and NAME as an env-variable name.
- Reads the value with `getpass`, then calls `keychain.store("pathfinder-mcp-" + ID, NAME, value)`.
- No socket, no daemon, no ledger.

**`/secret ID NAME` in the screen** (`tui.py`, +20):
- The same checks. The next line is read masked: the input line shows `*`, and the screen draws
  its own input line in cbreak mode.
- Enter stores the value in the screen's own process. Ctrl-C cancels. Nothing goes on the socket.
- The confirmation says an already-installed server picks the value up at the next
  `service restart`.
- [Q1 c] reuses this masked mode for `secret.ask`.

## A private home (independent of Q1)

**Born private:**
- `ledger.py`: `events/` 0700, `.lock` 0600, and a partition file opened with
  `os.open(path, O_WRONLY | O_APPEND | O_CREAT, 0o600)`. 0 net lines.
- `state.py`: the db is created 0600 before `sqlite3.connect` (+1). SQLite gives `-wal` and
  `-shm` the db's mode; all three came out 0600 in a local check.
- `memory.py`: `wiki/` 0700 (pages are already `mkstemp`'s 0600).
- `init.py`: `hooks/` 0700 and `pathfinder.zsh` 0600 (two literals).
- `mirror.py`: `registry.db` is born 0600.

**Existing files: `config.make_private(home)`** (+10):
- Drops group and other bits (`mode & 0o700`), skips symlinks, and does not descend into
  `workspace/`.
- Called once, in `__main__`'s `daemon` branch. That covers every upgrade, and launchd's
  `daemon.log`.
- Not in `config.home()`, which `exec.workspace()` calls on every tool call. Not a process umask,
  which children would inherit for the owner's own files.

**The owner's home today** (modes only):
- 0600: `config.json`, the socket.
- 0644: `state.db` (+wal, shm), every `events/*.jsonl`, `.lock`, `RULES.md`, `USER.md`,
  `daemon.log`.
- 0755: `events/`, `hooks/`.

## pathfinder/memory.py and prompts — the split pass (D47; 695 -> ~715)

**1. The write call.** `loop.run` with the memory tools and `consolidate_steps`.
- **No `final_step`:** its words were only ever the summary, so D30's list loses the
  consolidation pass.
- **Its goal:** the range, the RULES.md vocabulary, `self.context()` and the pending proposals.
  The previous summary and the "earlier lines" move to the summary call.
- **D39** applies to this call unchanged.

**2. The summary call.** `_summarize(sid, cfg)`, one request, no tools.
- **When:** only when `nth_newest_message_seq(sid, 5)` is past the previous summary's `upto`.
- **Input:**
  - `prompts/summarize.txt` (new, ~10 lines: today's closing paragraph as its own prompt);
  - the clock line;
  - one user message: the previous summary, then the session's messages in `(prev_upto, upto_s]`
    rendered by `render_line`, clipped by `tools.clip(..., 16000)`.
- **Output:** `session_summary` as today.
- **On an `LLMError`:** it is recorded as the pass's `error` and does not keep the slice. The next
  pass folds these messages.
- `memory_pass`'s `steps` and `usage` sum both calls.

**`consolidate.txt` becomes the write prompt:**
- The summary paragraph goes.
- It ends on m9's `no_summary` tail: "answer with one line: what you wrote down, or `nothing`".
- "What goes where" opens with **how the user wants you to work**: a `[user]` line saying "from
  now on", "always", "never", "ask me before" or "don't" becomes one `- ` line in USER.md, in the
  user's words, cited by its `#seq`, into the approval box (D28).

**Also in this unit:**
- **5e:** `self._retry_at = time.time() + _RETRY_S if kept else 0.0` (0 lines).
- `wiki/` 0700.

## Small live defects

- **5a. Include:** `state.presence()` returns the newest row across sessions, because the owner
  has one pair of hands.
  - The `session` parameter leaves `presence` and `today`.
  - The callers change in place: `deliver.hold`, and `daemon.py` line 573.
- **5b. Include,** through move 2.
- **5c. Include:** `_eventkit._get_store` takes a module lock (+2). The agenda thread, two polls
  and the tools can race it.
- **5d. Include, in `tui.render`:** `gate` and `job.gate` args are clipped at 300 characters with
  an `...(clipped; /approvals)` tail (+2). That covers every gate; `json.dumps` already escapes
  control characters.
- **5e. Include,** in the memory unit.
- **5f (U0). `keychain.secret` misreads a non-ASCII value: include, and fix the read.**
  - `find-generic-password -w` prints a non-ASCII value as bare hex with no marker. Verified
    2026-09-29: a throwaway `비밀abc` came back as `ebb984ebb080616263`. `secret()` returns that
    hex as the password.
  - Decoding any hex-looking output would corrupt a real all-hex token, which is a common key
    shape.
  - So when the `-w` output is even-length hex, one `-g` call decides. `password: 0x...` is the
    hex form and is decoded as UTF-8; `password: "..."` means the `-w` output was the value
    itself. The common path stays one call. ~+6 lines.
  - **Why fix the read rather than refuse non-ASCII at `store`:** the same reader serves entries
    the owner sets by hand. The mail password's README line is one, and a non-ASCII password is
    plausible there. A store-side refusal would never see those entries.

## ARCHITECTURE.md / ARCHITECTURE.ko.md, README pair (doc debt)

**The openings** (English "Built so far: partition 5", Korean 구획 5) become partition 8, one
paragraph covering 1-8.

**Counts live in §13 only.** Stale inline counts become the ceiling they are measured against:
- §8's `_eventkit.py (200 lines`;
- §11's `installer.py (core, ~480 lines)`;
- §13's "440 tests", "756 today" and "686 today".

§13's core command follows D49's rows.

**New and changed content:**
- **§1:** `registry.db`; the Keychain row, written by `/secret` and `pathfinder secret`, never
  chat; the home's modes.
- **§2:** the modes, and the Q1 backstop.
- **§6:** the second look.
- **§3/§7:** the tool record, and approvals typed in chat.
- **§10:** the two calls.
- **§11:** the copy and D40.
- **§12:** the new invariants.
- **§14:** `secret`, and `search` reading the copy.

Both languages, one commit. **The README pair:** `/secret` replaces the `security` line for MCP
servers.

## Where the decisions and the code disagree

1. **D40.** It says "the trial simply runs first", but `check` already reads the Keychain before
   the trial (step 4, then `resolved_env` into step 5). What blocks a token server is different:
   - there is no way in for the secret;
   - a missing required secret does not stop the check;
   - a required variable marked `isSecret: false` is never resolved.

   The fix is the intake plus a stop. The survey that D40's reversal names (U0) keeps D40: 4 of
   the owner's 7 services take a static token, 1 needs nothing, and 2 (Google) need OAuth.
2. **D49** names the per-module ceilings in `test_invariants.py` as the gate. Only daemon, memory
   and jobs of the 13 core rows have one; `state.py` (625, row 600) has none. U17 adds the 1.3x
   line for the other ten.
3. **D52 against blueprint §11,** "말한 것만 기억한다" ("only what was said is remembered"), and
   `test_state.test_restore_said_only`. History now carries the names and arguments of what was
   done. The invariant becomes "what was said and what was called; never a tool result or an
   envelope". The test is extended, not dropped.
4. **D51 against `loop.run`'s rule** that everything streamed reaches history (`said`). A
   withdrawn draft was streamed but stays out of history by design. The ledger keeps it as
   `assistant {final: false, withdrawn}`.
5. **D30** lists the consolidation pass. D47's split takes it out of the write call. The second
   look also needs a step left, so a draft at the last step stands.
6. **HISTORY's forced-`memory run` defect.** A forced run already ignores the wait. What persists
   is the wait after a forced run succeeded.
7. **m10** replayed the last 12 messages, while live sends the whole session. The exit re-run
   inherits this.

## Tests that must exist

- **`test_loop.*`** (scripted steps):
  - `test_second_look_passes_a_clean_draft`
  - `test_flagged_draft_is_withdrawn_and_rerun_once`: `retract` emitted, the draft out of `said`
    and `messages`, `RERUN_NOTE` appended, the re-run not re-checked
  - `test_second_look_skipped_after_a_tool_or_on_a_resume`
  - `test_second_look_needs_a_step_left`
  - `test_bad_json_or_llm_error_or_cancel_leaves_the_draft`
  - `test_verifier_input_names_every_tool_and_carries_the_agenda`
  - `test_verifier_usage_counts_but_not_as_a_step`
- **`test_daemon.*`:**
  - `test_retract_records_withdrawn_assistant_and_sends_line`
  - `test_history_holds_only_the_rerun`
  - `test_turn_end_carries_check`
  - `test_chat_approval_answered_without_a_model_call`
  - `test_chat_approval_needs_an_open_gate_newer_than_the_last_user_message`
  - `test_longer_message_still_reaches_the_model`
  - `test_steer_is_never_intercepted`
  - `test_install_and_uninstall_through_installer_handle` (the existing socket tests, unchanged)
  - `test_job_lines_reach_a_moved_screen`
  - `test_notice_held_while_typing_after_a_move`
- **`test_broker.*`:** `test_chat_approval_phrases` (each phrase, normalization, near misses)
- **`test_state.*`:**
  - `test_tool_record_folds_before_the_answer`
  - `test_tool_record_has_no_results_and_clips_args`
  - `test_record_skips_signal_and_job_calls`
  - `test_summary_boundary_ignores_record_rows`
  - `test_restore_said_only` (extended)
  - `test_open_approvals`
  - `test_presence_is_the_newest_row_across_sessions`
  - `test_db_wal_shm_born_0600`
- **`test_mirror.*`:**
  - `test_first_sync_pages_into_the_copy_and_completes`
  - `test_first_sync_resumes_from_the_stored_cursor`
  - `test_incremental_sync_uses_updated_since_and_marks_seen`
  - `test_search_matches_words_in_name_and_description`
  - `test_search_note_while_incomplete_and_after_a_failure`
  - `test_search_restarts_a_stalled_sync`
  - `test_failed_page_keeps_cursor_and_records_error`
  - `test_bad_cursor_restarts_the_first_sync`
  - `test_other_registry_empties_the_copy`
  - `test_find_exact_then_suffix`
  - `test_deleted_row_is_dropped_on_receipt`
  - `test_deprecated_not_searched_but_found_by_name`
  - `test_db_is_0600`
  - `test_sync_thread_is_a_daemon_thread`
- **`test_catalog.*`:**
  - `test_page_returns_items_and_cursor`
  - `test_latest_by_exact_name`
  - `test_poll_candidates_from_changed_rows_only`
  - `test_search_tools_reads_the_copy`
- **`test_installer.*`:**
  - `test_handle_install_and_uninstall_replies`
  - `test_propose_by_name_from_the_copy`
  - `test_propose_by_name_falls_back_to_the_exact_lookup`
  - `test_required_non_secret_env_resolves_from_keychain`
  - `test_missing_required_secret_stops_before_the_trial`
  - `test_missing_optional_secret_still_trials`
- **`test_keychain.*`, `test_tui.*`, CLI:**
  - `test_store_sends_the_value_on_stdin_not_argv`
  - `test_store_escapes_backslash_then_quote`
  - `test_store_refuses_newline_and_nul`
  - `test_store_checks_returncode_and_reads_back`
  - `test_secret_decodes_hex_only_when_g_says_0x`
  - `test_hex_looking_ascii_token_is_kept`
  - `test_slash_secret_masks_and_never_touches_the_socket`
  - `test_ctrl_c_cancels_secret_entry`
  - `test_gate_args_clipped`
  - `test_retract_rendered`
  - `test_cli_secret_uses_getpass_and_store`
- **[Q1]** (a): `test_token_redacted_in_file_and_returned_event`, `test_scanner_uses_the_package_pattern`.
  (b): `test_paste_vaulted_and_bound`, `test_unbound_paste_expires`,
  `test_keychain_failure_falls_back_to_redaction`. (c): `test_secret_ask_sent_on_missing_secret`,
  `test_secret_value_never_reaches_ledger_or_state`, `test_secret_stored_reproposes`. Token-shaped
  strings are built by concatenation.
- **File modes:**
  - `test_partition_and_lock_born_0600_dir_0700`
  - `test_make_private_drops_group_other_keeps_owner_x_skips_symlinks_and_workspace`
- **`test_memory.*`:**
  - `test_pass_makes_a_write_call_then_a_summary_call`
  - `test_no_summary_call_without_new_messages`
  - `test_summary_llm_error_does_not_keep_the_slice`
  - `test_write_call_has_no_final_step`
  - `test_write_prompt_names_standing_instructions`
  - `test_forced_pass_clears_the_retry_wait`
  - `test_wiki_dir_0700`
- **`test_invariants`:**
  - the 1.3x ceiling for every §6 row
  - `mirror.py` <= 150
  - `test_second_look_matches_the_bench`: `VERIFIER_PROMPT`, `RERUN_NOTE` and `FLAG_WORDS`
    byte-identical to `bench/m10_unsupported.py`, and `verifier_input` equal to the bench's for a
    fixture turn
  - `test_home_stays_private`
  - no new ledger type
  - only `catalog.py` names the registry URL

## Units and build order

Each unit is one implementer call: 1-3 files plus their tests. Briefs are files in the session
scratchpad. Units sharing a file run in the order given; the rest run in parallel.

| # | Unit | Files | Size | After |
|---|---|---|---|---|
| U0 | **Completed.** D40's survey: 4 of 7 services take a token, 2 need OAuth (Google). Required variables not marked secret: 54 of 3,660. Required variables with a `default`: 81 of 6,131. Deletions arrive through `updated_since`. Paging facts. `security -i`'s escaping and exit codes. The `-w` hex defect (5f). All folded into the sections above | — | done | — |
| U1 | The registry copy | `mirror.py`, `catalog.py` | medium: ~140 lines | — |
| U2 | Readers of the copy | `senses/catalog.py`, `installer.propose_by_name`, `__main__.search_cmd` | small: −15 net | U1 |
| U3 | Move 1 | `installer.handle`, `daemon.py` | small: daemon −21 | U2 |
| U4 | Move 2, 5b | `jobs.py`, `daemon.py` | small: daemon −10 | U3 |
| U5 | The tool record (D52-1) | `state.py` | small-medium: ~+20 | — |
| U6 | Approvals typed in chat (D52-2) | `broker.py`, `state.open_approvals`, `daemon.turn` | small: daemon +7 | U4, U5 |
| U7a | The second look (D51) | `loop.py`, `daemon._run_turn` | medium: loop +45, daemon +4 | U6 |
| U7b | The `retract` line | `tui.render`, `__main__._stream` | small | U7a |
| U8 | The adopted prompt | `prompts/system.txt` | small | anytime before U18 |
| U9 | The split pass, 5e | `memory.py`, `consolidate.txt`, `summarize.txt` | medium: ~+20 lines + prompts | — |
| U10 | The intake, D40, 5f | `keychain.py` (`store`, the fixed `secret` read), `installer.check` step 4, `__main__ secret` | small: ~+36 | U3 |
| U11 | `/secret`, 5d | `tui.py` | small | U7b, U10 |
| U12 | 5a | `state.py`, `deliver.py`, `daemon.py` line 573 | small | U6 |
| U13 | 5c | `_eventkit.py` | tiny | — |
| U14a | Modes, born private | `ledger.py`, `state.py` (0600 db) | small | U12 |
| U14b | Modes, existing files | `config.make_private`, `__main__` daemon branch, `init.py` literals | small | U10 |
| U15 [Q1] | The backstop at the ledger door, (a) or (b) | `ledger.py`, `keychain.py` (`TOKEN_RE`; b: vault/bind/sweep), `scripts/scan_personal.py` | small (a) / medium (b) | U14a, U10 |
| U16 [Q1 c] | The harness-asked secret | `daemon.py` (record hook, `secret` handler), `tui.py` (`secret.ask`), `installer.py` (event fields) | small-medium | U11, U15 |
| U17 | Ceilings | `tests/test_invariants.py` | small | any time; lands last |
| U18 | Exit runs | `bench/m9_memory_write.py`: `current` = the shipped write prompt, `no_summary` dropped, USER.md edits recorded, then (8) at ~40k tokens. `bench/m10_unsupported.py`: `baseline` = the adopted prompt, verify riding on it, the record present through `State.history`, then (13) at ~215k tokens. Both need the lead's go-ahead | — | U5, U7a, U8, U9 |
| Lead | Live (1)-(14); both doc pairs; HISTORY ("Before partition 8" with the owner's answers, "Partition 8"); the reviewer (opus, read-only). Defect classes: secrets reaching the ledger, the withdrawn draft and history, file modes, the sync thread at shutdown, FTS upserts, the chat-approval match. Then the scan and the commit on `build` | — | — | — |

Q1's answer is needed only by U15 and U16. Everything else builds without it.
