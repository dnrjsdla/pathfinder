# Partition 8 — trust in the answer and the security bundle: the rewritten prompt and a second look, a record of what was done, the registry copy, the split memory pass, secrets only through the Keychain, a private home, Gmail, skills

**Where this stands (2026-09-30).**
- **Code:** branch `partition-8` (worktree `.claude/worktrees/partition-8`). Merged, each after
  review: U0, U1, U2, U5 (U5b), U9, U13, and on 2026-09-30 U21, U22, U24, U3, U4, U23, U30, U6,
  U12, U25, U31, U7a, U7b, U8, U32, U33, U10, U14a, U11, U14b, D64, U34, U15a (D66). `daemon.py` 768/780, `tui.py`
  367/400 + `tui_secret.py` 71/150, `installer.py` 664/700, `loop.py` 295/520.
- **Next:** U15b, U16, U26, U27-U29,
  U17, U18. `tui.py` has room for U15b, U16 and U26 after D64.

The unit table at the end is where the next session starts. Every row carries its state, its
files, its size, what it waits for, and its tests.

**Deploy gate.** Nothing from partition 8 reaches the owner's daemon until U15a, U21, U31 and U33 have
landed (**all four merged, 2026-10-01**):
- **U15a (the pasted-token handle at the ledger door).** The tool record (U5, merged) re-sends up
  to 120 characters of every call's arguments to the cloud model on every later turn. A token
  inside an argument would ride along each time until the handle replaces it at the door.
- **U21 (the envelope filter).** Without it, third-party text reaches the model unfiltered and
  unclipped through a signal's `fields`.
- **U31 (the tool-result filter, D62).** A tool result is the larger path for the same text; the
  gate's purpose is that third-party text is filtered before the model.
- **U33 (the remaining raw exits).** U31's review surveyed every exit: calendar titles in the
  agenda and an MCP server's parameter schema still reach the model raw.

## The secret path (D53, decided)

A secret reaches the Keychain without passing through the chat. There are two paths.

**The main path: the assistant asks through the harness** (U16).
- **Trigger.** D40's check stops on a missing required secret. `Daemon.record` sees
  `install_failed` carrying `names` (and the check's own `server_id`) and sends every attached
  screen `secret.ask {server_id, name, server}` for the first name. *As built:* the trigger is
  `names`, set only by the secrets stop; the id is the check's, never computed again.
- **The prompt.** The screen switches to the masked input `/secret` uses: "NOTION_TOKEN for
  easy-notion (hidden; stored in the Keychain, never shown to the assistant)".
- **Enter** sends `secret {server_id, name, value}` over the 0600 socket. The daemon then:
  - stores the value on a worker thread - *as built,* through `keychain.take`, so a value of
    `--from pasted-<seq>` binds that pasted item instead (the prompt lists the live handles);
  - writes no ledger event that holds the value;
  - answers `secret.stored` - *as built,* to every attached screen, so a screen that did not
    answer leaves its entry;
  - runs `installer.propose_by_name(server)` again, and the proposal's gate line follows -
    *as built,* in the background, cancelled (not awaited) when the daemon stops.
- *As built,* the handler is `installer.secret` (the U3 pattern; `daemon.py` was 779/780). It
  refuses a missing or mistyped field before anything is stored, and also takes
  `{server_id: "mail", value}` with no name, the mail password (U26). A screen with an entry
  already open drops a new ask with a line, and the line being typed waits aside and comes back.
- **The value never reaches the ledger or the model.** `pathfinder say` has no screen, so it
  gets the `/secret` line instead.
- **Cost:** daemon +~9 (the `record` hook, the `secret` handler), `tui.py` +~12, `installer.py`
  +~2, tests ~80.
- **What it gives up:**
  - A secret value now crosses a daemon handler. A future bug that logs a message there would
    leak it, and `test_secret_value_never_reaches_ledger_or_state` guards against that.
  - Three new socket message types.
- **What the owner sees:** "Notion needs a token", the masked prompt at once, a paste into it,
  then the proposal with `/approve`.

**The backstop: vault and handle, at the ledger door** (U15a, U15b).
- **At `ledger.append` (D66).** A token-shaped substring (`keychain.TOKEN_RE`) in text the owner
  typed (an event with `trust: user`) goes into the Keychain as service `pathfinder-pasted`,
  account `pasted-<seq>`, the event's own ledger seq; further tokens in the same event are
  `pasted-<seq>-2`, `-3`. The text carries `[secret:pasted-<seq>]` instead, and the event
  `append` returns carries the handle, so `state`, history, the tool record, the screen's gate,
  job and steer lines, and every later turn see only the handle. In every other event (a tool's
  call or result, a signal, a job's or the model's words, memory) the substring is written as
  `[redacted]`, with no Keychain call (`keychain.mask`).
- **One pattern.** The scanner's `api-key`/`private-key` patterns become `keychain.TOKEN_RE`, with
  Notion's `ntn_` and `secret_`, Slack's `xapp-`, Stripe keys, a JWT, a Telegram bot token and a
  PEM or PGP private key (the whole block, or the header and its base64 when the END line was
  cut off) added. A prefix counts only at a word's start, and the pattern stays linear.
  `scripts/scan_personal.py` imports it.
- **Binding a handle.** `pathfinder secret <id> NAME --from pasted-<seq>` and
  `/secret <id> NAME --from pasted-<seq>` move the value to `pathfinder-mcp-<id>`/`NAME` and delete
  the pasted item. The masked prompt (U16) lists the live handles, so a pasted token can be
  bound where the secret is asked for.
- **Expiry.**
  - An index `<home>/pasted.json` (0600), `{"pasted": {handle: created}}`, holds handles and
    creation times, never values.
  - Every new paste, and every daemon start, deletes pasted items older than 24 hours and drops
    a handle whose item is gone. An index that is missing or unusable reads as empty; an item
    whose handle it lost stays in the Keychain, unknown (the known limit, D66).
  - Binding an expired handle says "expired - enter it again with /secret". A seq never recurs,
    so an old handle never comes to name a new value.
- **When the Keychain write fails** (a locked keychain, no `security` binary), the substring is
  written as `[redacted]`, so the append still lands and the value still never reaches the
  ledger.
- **What it gives up:**
  - The ledger writer calls `security` under its lock, about 30-50 ms, and only for an owner's
    event that holds a token: one `find` per token first, so an item already there (another
    home's, or a reset ledger's) is never replaced. The sweep runs at daemon start and on a
    paste, never when a ledger is opened, and asks once per live handle whether its item is
    still there.
  - A token in third-party text is redacted and cannot be read back (D66).
  - One more file under the home.
  - A false positive can be read back for 24 hours, then it is gone.
- **What the owner sees:** "you pasted a token; it is stored as pasted-<seq> - bind it with
  /secret <id> NAME --from pasted-<seq>".
- **Within the same turn,** a signal envelope or a tool result still reaches the model raw,
  because those travel beside the ledger, not through it (a steer goes on as recorded). The handle protects every later
  turn and the record.

**Encryption was considered and dropped (D53).** The model cannot use ciphertext, so encrypting
the token only produces a handle with a key to guard.

---

**"It runs" =** each of the following is checked live.
- (1) **The first sync.** A temp home with the catalog sense on. The socket answers `say` at
  once while the registry copy fills in the background. `search_tools` during the first sync
  answers from the partial copy with a line saying so. The first sync's time, row count and file
  size go into HISTORY (so far: 462 s, 517 s, and 1,076 s under load; 54 MB for about 37,200
  rows). A `service restart` mid-sync resumes from the stored cursor. A `service stop` mid-sync returns inside launchd's timeout and
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
- (5) **D40 and D53's main path, on a server that costs nothing (D55): `easy-notion`,** the
  community Notion server U0 found, with a free internal-integration token.
  - `propose_install` with the token missing: no trial runs, and the screen's masked prompt
    appears unasked.
  - Once the token is entered, the same server trials and proposes. Approve it, and
    `pathfinder tools` lists it as enabled.
  - Through `pathfinder say`, the same request answers with the `/secret` line instead.
  - **What the owner does by hand, once:** on Notion's integrations page, create an internal
    integration (free), then share one test page with it (the page's `...` menu, then
    Connections), and paste the integration's token into the masked prompt when it appears.
- (6) **A token-shaped string typed at the prompt.** The events file, `state.db` and the next
  turn's history hold `[secret:pasted-<seq>]`. The Keychain holds the value.
  `pathfinder secret <id> NAME --from pasted-<seq>` binds it, and the pasted item is gone afterwards.
  An unbound handle is swept after 24 hours; test this with a back-dated index entry.
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
  appears in the history. A call the owner denied shows as denied, and a gated one as waiting
  for approval.
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
- (15) **Gmail (D57), on the owner's own account, after the check in the Gmail section passes.**
  - `/secret mail` stores the app password masked.
  - After `service restart`, "what's in my inbox?" calls `list_mail`.
  - `search_mail` with a Gmail query (`from:<a sender> newer_than:30d`) and with a Korean word
    returns headers only.
  - Gmail's web page still shows those messages unread.
  - A second restart announces no old unread mail.
- (16) **Skills (D58, D61).**
  - On a fresh home, the seed is copied: `<home>/skills/connect-gmail.md` exists after the first
    start, and `read_skill`'s description lists it.
  - A skill the owner has edited survives a restart and an upgrade unchanged.
  - "Connect my Gmail" makes the model call `read_skill` and walk the steps.
  - An `edit_memory` aimed at `skills/` from a signal, job or memory origin is refused.
  - From a user turn the same edit goes to the approval box, even with `grants` allowing
    `edit_memory`.

Decisions this partition rests on:
- D8: the exact-name fallback, the resumable sync, and D53's masked prompt.
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
- D53: the secret path. The harness asks, the screen takes the value masked, and a pasted token
  becomes a handle.
- D54: OAuth is inside this partition, and D57 chose its way in for Gmail.
- D55: the live check (5) uses a free server.
- D56: `mirror.py`'s ceiling is 200, and a file past it is cut, not raised.
- D57: Gmail comes through the mail sense over IMAP with an app password, reading and searching.
  A local Google server is the fallback. Google Calendar and a generic MCP OAuth client are out.
- D58: skills are a wiki of their own, shipped with a default set; it amends the blueprint.
- D59: mail is read and searched, not sent.
- D60: D36's foreign-file helper lives in `config.py` (U30).
- D61: skills live in one place, `<home>/skills/`; shipped skills are seeds copied when missing;
  the index rides in `read_skill`'s description; the assistant may propose a skill from a user
  turn, always through the approval box.
- D62: every tool result passes `tools.clean` on its way to the model (U31).
- The owner's direction for the server (D44/D45): the server hands over packages, not single
  services. See the skills section.

NOT in this partition:
- **Correcting a session summary that asserts something false** (D52 leaves it for later; D41
  already ends a session after two quiet hours).
- **Tool results in history.** The record keeps names, arguments and states only.
- **A second look for signal turns, jobs and the memory pass.** Their text is not an answer to
  the owner.
- **Buffering answers** so a draft is never seen: it would hide streaming for half of all turns.
- **Google Calendar.** The owner does not use it (D57); the calendar sense reads macOS Calendar.
- **Sending mail** (D59). It waits until the second look has been measured in live use. When it
  comes, it is a write tool that asks every time, showing the recipient, the subject and the
  body.
- **A generic MCP OAuth client** (D57). Measured, it would reach one official remote (Notion),
  which a token server already reaches.
- **Header secrets of remote servers.** One of the owner's token services (news) is a remote
  with a header key, and `catalog._package` reads no `remotes[].headers`. It waits.
- **Skills from anyone but the owner and the package**, and a skill marketplace. The server
  (D44/D45) is where packages come from later.
- m3 re-run over the adopted prompt and the agenda (credits; the lead decides).
- The discovery server (D44/D45).
- A live reload of an installed server after `/secret`: a restart does it.
- A live registry search while the first sync runs.
- The screen's redesign.
- The shell sense announcing `pathfinder tui`.
- The model's wrong times.
- `presence` volume.
- **Additions this partition makes to shared vocabularies.**
  - No new config key, sense option, RULES.md knob or ledger type.
  - The ledger gains two fields only: `assistant.withdrawn`, `turn_end.check`.
  - New socket lines:
    - `retract`, `secret.ask` and `secret.stored` (server to client);
    - `secret` (client to server).

The token of 2026-09-26 (D50): the owner revoked it on 2026-09-29, so every copy is inert. Those
copies are the ledger, `state.db`, the other app's config file, backups, and what the cloud model
received. The ledger stays append-only. A secret that cannot be revoked would get a one-time
offline scrub by the owner's hand, from `scripts/`, never a product command.

**Line budgets.**

**The count rule (D49).** The core is the blueprint's §6 rows: ledger, state, daemon, loop,
broker, policy, exec, tools, signals, rules, deliver, jobs, memory. It is **3,833 lines** on
`partition-8` today (3,762 at c749e17) and about **3,955** after this plan. The adapter layer
(`llm/`, `senses/`, catalog, installer, mirror) and the infrastructure (`config`, `clock`,
`keychain`, `init`, `__main__`) sit outside it and keep their own ceilings.

**The gate** is the per-module ceiling in `test_invariants.py`. U17 adds the 1.3x line for the
ten core rows that have none, D56's 200 for `mirror.py`, and 250 for the shared sense adapters
(`senses/_*.py`).

Counts are `splitlines()` on `partition-8` at 939c831; "with U2" is the review worktree.

| Module | Now | Ceiling | After this plan |
|---|---|---|---|
| `daemon.py` | **779** | 780 | ~771 (U3 −21, U4 −10, U6 +7, U7a +6, U16 +9, U27 +1) |
| `state.py` | 681 | 780 (added) | ~688 (U6 +6, U14a +1) |
| `memory.py` | 710 | 780 | ~711 (U23) |
| `loop.py` | 197 | 520 (added) | ~244 (U7a) |
| `broker.py` + `policy.py` | 84 + 57 | 455 (added) | ~116 + 57 (U6 +26, U31 +6) |
| `ledger.py` | 122 | 520 (added) | ~142 (U24 +8, U15a +12) |
| `tools.py` | 212; 225 after U21 | 390 (added) | ~230 (U21 moved `oneline` here; U31 +5) |
| `signals.py` + `rules.py` | 201 + 220 | 585 (added) | ~205 + 220 (U21) |
| `jobs.py` | 267 | 350 | ~283 (U4 +15, U23 +1) |
| `exec.py`, `deliver.py` | 217, 86 | 390, 325 (added) | unchanged |
| `mirror.py` | 188; 191 with U2 | **200** (D56) | 191 |
| `catalog.py` | 275; 278 with U2 | 300 | ~272 (U21 moves `oneline` out) |
| `installer.py` | 674; 677 with U2 | **700** | ~698 (U3 +15, U10 +5, U16 +1; U22 net 0) |
| `senses/catalog.py` | 141; 137 with U2 | 150 | 137 |
| `senses/mail.py` | **148**; 141 with U25 | 150 | ~140 (U25 moves the IMAP code out; U31 −1) |
| `senses/_imap.py` | new | 250 (adapters, U17) | ~65 (U25) |
| `senses/_eventkit.py` | 210 | none today; 250 (U17) | 210 |
| `mcp/tools.py` | 116 | `mcp/` 1,858 | ~119 (U22) |
| `tui.py` | 314 | 400 | ~357 |
| `tui_secret.py` | new; 71 (D64 moved the masked input out of `tui.py`, 392 -> 367) | 150 (new, D64) | 128 (U15b, U16, U26) |
| `init.py` | **297** | 300 | 297 |
| `skills.py` | new | 120 (new, D61) | ~80 (U27) |
| `keychain.py`, `config.py`, `__main__.py` | 14, 169, 455 (463 with U2) | none | ~82, ~179, ~490 |

**`installer.py` lands at about 698/700.** If it crosses, the only movable block is D36's
foreign-file helper (`backup_file`, `_atomic_write_json`, `merge_json_file`, about 40 lines).
D36 keeps that helper in `installer.py` until a third caller appears, so moving it needs the
owner's word ("disagree" 12).

## Entrance and exit measurements

**The entrances** are m9 (D43) and m10 (D51, D52).

**The first sync** has been measured three times: 462 s (m9), 517 s (U0) and 1,076 s (U1's
review, under load). The duration depends on the registry and the machine, so no single figure
is a fact to design against. The database is 54 MB for about 37,200 rows.

**The two exits**, both needing the lead's go-ahead for credits:
- m10 re-run on the adopted prompt, with the record and the second look: (13), about 215,000
  tokens.
- m9's memory replay on the split pass: (8), about 40,000 tokens.

m10 replayed the last 12 messages of each session. Live sends the whole session, and the re-run
inherits that simplification.

## pathfinder/daemon.py — what moves out first (779/780)

Three items want daemon lines: the second look's wiring (+4), approvals typed in chat (+7), and
D53's `secret` handler (+9). A mid-stream model error's text (+2, see "disagree" 8) adds a
fourth. Two moves come first. Each moves protocol code to the module that
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
| D51's wiring in `_run_turn`, plus recording a mid-stream error's text | +4 +2 | 761 |
| D53's `record` hook and `secret` handler | +9 | 770 |

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
  - the run has called no tool and is not a resume (decided at build: stricter than the line
    above, so that "No tool was called in this turn." is always true, also after a steer);
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
    2. A dim line under it: "철회 · withdrawn: the answer above says what the calendar,
       reminders, memory or registry hold without calling a tool - trying again with the tools"
       (`{what}` from `FLAG_WORDS`; the verifier's own `why` is never shown).
    3. The re-run streams below.
  - **Other clients:** `pathfinder say` prints the same line to stderr (`_stream`); `listen` goes
    through `tui.render`.

**Cost.** In m10, both fixes together took +21 calls over 40 turns (about +0.5 per user turn) and
about +800 tokens per turn on first steps.

**Lines.** `loop.py` +~45 (the constants ~20, the input builder, the parse, the flow), daemon +4,
`tui.py` and `__main__` +2 each.

**D51's reversal needs its denominator in the ledger:** `turn_end.check` (checked) and
`assistant.withdrawn` (flagged).

## The two amended principles — the invariant text (D51, D52)

Both replace wording in blueprint §11 and in ARCHITECTURE. The text below is binding. Each
clause names the test that enforces it.

**Invariant 1. History is what was said and what was called — nothing else.**

> History, the messages a turn is built from, holds exactly two things:
> 1. what the user and the assistant said: a `user` event with origin `user`, and an `assistant`
>    event with `final` set;
> 2. for each tool call the assistant made in a user turn, the tool's name, its arguments, and
>    one state of four: succeeded, failed, denied, or waiting for approval.
>
> History never holds a tool's result text, a signal envelope, or a draft the second look
> withdrew.
>
> The record of a call is projected only from the ledger's own events about that call:
> `tool_call`, `tool_result`, `approval_pending` and `approval`. It is never built from anything
> the model wrote. A call with no such event can never appear in it. That includes a call that
> never ran, a call refused by policy before it ran, and a call the model only claims to have
> made. An assistant message that describes a call stays an assistant message and never becomes
> a record line.
>
> Arguments are compact JSON cut to 120 characters. They are taken from the event exactly as
> the ledger door wrote it, so a pasted token appears only as its `[secret:pasted-<seq>]` handle,
> and a token the owner did not type as `[redacted]` (D66).

| Clause | Test |
|---|---|
| Said, plus the record, and nothing else | `test_state.test_history_is_said_and_called_only`. It replaces `test_restore_said_only` and keeps its signal-envelope and non-final-assistant cases, adding a withdrawn draft and a tool result, and asserts the whole history list. |
| Name, arguments, one of four states | `test_state.test_tool_record_states`: ok, failed, denied, waiting, then waiting → ok after an approve. |
| Never a result's text | `test_state.test_tool_record_holds_no_result_text`: a marker string inside a `tool_result` never appears in history. |
| Only from ledger events; a call that did not happen never appears | `test_invariants.test_no_tool_record_without_tool_call_events`. A ledger with no `tool_call`/`tool_result`/`approval_pending`/`approval` events, including an assistant message that copies the record line word for word, yields a history with no record message. `test_broker.test_refused_call_emits_nothing` covers a policy refusal. |
| Clipped | `test_state.test_tool_record_clips_arguments` |
| Through the secret door | `test_ledger.test_pasted_token_reaches_the_record_as_a_handle` (U15a) |

**Invariant 2. Everything streamed is in the ledger; only a withdrawn draft stays out of
history.**

> Every piece of text streamed to the owner's screen is recorded in the ledger, whatever the
> ending: an answer, a spent budget, an interrupt, a gate, a model error in mid-stream, or a
> withdrawal.
>
> The only streamed text kept out of history is a draft the second look withdrew. It is
> recorded as `assistant {final: false, withdrawn: {flags, why}}`: marked as withdrawn, with the
> reason. Like every ledger line, it is never deleted or rewritten.

| Clause | Test |
|---|---|
| Streamed means in the ledger, every ending | `test_daemon.test_every_streamed_delta_is_in_the_ledger`: scripted steps for answer, exhausted, interrupt, gate, `LLMError` mid-stream and withdrawal; the deltas the socket received are contained in the turn's `assistant` events. `test_exhausted_turn_still_records_what_was_said` stays. |
| Only a withdrawn draft is out of history, marked with its reason | `test_daemon.test_withdrawn_draft_marked_with_reason_and_out_of_history` |
| Never deleted | `test_invariants.test_single_writer` (unchanged: only `ledger.append` writes events). `test_state.test_withdrawn_draft_survives_rebuild`: after `rebuild`, the line is in the ledger and still not in history. |

**Exactly which lines change** (at 32f7dda; the lead edits them in the docs commit):
- **`docs/blueprint/pathfinder-blueprint.html`**
  - **Line 313.** The §11 row `말한 것만 기억한다 · 재부착은 봉투를 되살리지 않는다 | 유지 | test_restore_said_only`
    is replaced by:

    `<tr><td>이력에는 두 가지만 실린다 — 사용자와 비서가 한 말(origin 이 user 인 user, final 인 assistant), 그리고 비서가 사용자 턴에서 부른 툴 호출마다 이름 · 인자 · 상태(성공 · 실패 · 거부 · 승인 대기). 툴 결과 본문 · 신호 봉투 · 두 번째 확인이 철회한 초안은 절대 실리지 않는다. 호출 기록은 장부에 적힌 그 호출의 사건(tool_call · tool_result · approval_pending · approval)에서만 만들어지고 모델이 자기에 대해 쓴 말에서는 절대 만들어지지 않는다 — 일어나지 않은 호출은 기록에 나타날 수 없다. 인자는 120자로 잘리고 다른 모든 글과 같은 비밀 손잡이 문을 지난 모습으로만 실린다</td><td><span class="tag new">개정 (D52)</span></td><td><code>test_history_is_said_and_called_only</code> · <code>test_no_tool_record_without_tool_call_events</code></td></tr>`
  - **Inserted after it:**

    `<tr><td>화면에 흘린 글은 어떤 결말이든(답 · 예산 소진 · 중단 · 게이트 · 스트림 도중 모델 오류 · 철회) 전부 장부에 남는다. 이력에서 빠지는 흘린 글은 두 번째 확인이 철회한 초안 하나뿐이며, 그것도 장부에 철회 표시와 이유를 달고 남고 지워지거나 고쳐지지 않는다</td><td><span class="tag new">새로 (D51)</span></td><td><code>test_every_streamed_delta_is_in_the_ledger</code> · <code>test_withdrawn_draft_marked_with_reason_and_out_of_history</code></td></tr>`
- **`ARCHITECTURE.md`**
  - **Lines 101-102** ("**Only what was said becomes history:** ..."): replaced by Invariant 1's
    text and the record's projection.
  - **Line 239** ("Every outcome carries all text streamed in the turn."): gains "- except a draft
    the second look withdrew, which the daemon records as `assistant {final: false, withdrawn}`".
  - **Line 657**, the §12 row "... only what was said is remembered | `test_state.*`": split into
    the row "State is rebuildable, apply is idempotent, a turn is counted once by two processes"
    and a row for Invariant 1 naming its tests.
  - **Line 671**, the §12 row "What was streamed reaches history even when the budget runs out
    ...": replaced by Invariant 2's row naming its tests.
- **`ARCHITECTURE.ko.md`:** the same four places. Lines 96-97 ("**말한 것만 이력이 된다:** ..."),
  line 228 ("어떤 결말이든 그 턴에 흘려보낸 텍스트를 전부 싣는다."), line 637 ("... 말한 것만 기억") and
  line 647 ("예산이 바닥나도 흘려보낸 말은 이력에 남음 ...").
- **Code comments that restate the old rule:**
  - `pathfinder/state.py` line 127 ("only what was said is remembered") changes with the record
    (U5/U5b).
  - `pathfinder/loop.py` line 106 ("what was said must reach history") changes with the second
    look (U7a).

**Built:** U5 projects all four states (U5b was folded into it), and the two tests `test_history_is_said_and_called_only` and
`test_no_tool_record_without_tool_call_events` exist on `partition-8`. The other tests named here land
with U7a (Invariant 2), U15a (the handle) and U17.

## History carries a record of the tool calls (D52, part 1) — built in U5

**Built on `partition-8`** (U5, with U5b folded in).
- `state._project` writes a `messages` row with role `calls` for a user-origin `tool_call`, and
  marks it ` ok` or ` failed` from its `tool_result`.
- A broker gate (`approval_pending`) writes the call as ` waiting for approval`. `approval
  {denied}` marks it ` denied`, and the resumed call's own row takes over after an approve. The
  row keeps its approval id in a `ref` column.
- `history()` folds consecutive `calls` rows into one `system` message placed before that turn's
  answer.
- Arguments are compact JSON cut to 120 characters. No result text is ever kept.
- `test_state.test_history_is_said_and_called_only` and
  `test_invariants.test_no_tool_record_without_tool_call_events` exist.

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

**Caveats.**
- The Anthropic adapter (experimental) folds every `system` message into its top-level `system`,
  so there the record loses its position. The rerun note does too.
- A handler that returns an error as a string still gets ` ok` in the record until U22 and U23
  land.
- A reindex is needed once after the upgrade ("known limits").

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

**As built (U6, U32).**
- `state.open_approvals(session, max_age_s)`: the asking session's own pending gates, plus a job's
  pending gate from any other session while it is fresh (`now - ts <= session_resume_s`, the two
  hours that end a session), each only if newer than the asking session's last `user` message.
  A job gate older than that is approved with `/approve <id>`, never by a phrase.
- The reply, one line per open gate, the way out once at the end:
  `승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve <id> or /deny <id>
  (<tool>; shell: pathfinder approve|deny <id>) - if you meant something else, say it again ·
  다른 뜻이었다면 다시 말해 주세요`.
- The first phrase gets the line; a second one reaches the model (the gate is then older than
  the last `user` message). A curly apostrophe matches like a plain one.

## pathfinder/mirror.py — the registry copy (D46; built in U1: 188 lines, ceiling 200 by D56)

**Built as specified below.** The review added handling for four failures it reproduced (D56):
- an endless sync on a 4xx;
- a repeated cursor;
- a fresh database opened by several readers at once;
- a 2,000-word query.

U2 brings the file to 191.

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
  first sync of 8 to 18 minutes would be awaited by `stop()`, which would hold `service stop` past
  launchd's timeout: SIGKILL, and orphaned MCP children. A page cut off mid-transaction rolls
  back.
- **The CLI** never syncs; it only reads.

**Lifting it out (D44/D45).** This module is the server's indexer as it stands. `search_tools` is
deleted (D45), and `find` becomes the server's lookup.

## pathfinder/catalog.py (275 after U1; 278 with U2) and senses/catalog.py (139 with U2)

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

## pathfinder/installer.py (674; 677 with U2; ~698/700 after this plan)

- **`handle(msg)`** (+15): move 1 above.
- **`propose_install_by_name` raises on an error** (U22, net 0), so a failed proposal is a failed
  call.
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

## The secret intake — `keychain.store`, `pathfinder secret`, `/secret`

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
- D53's `secret.ask` (U16) reuses this masked mode. Its Enter sends the value to the daemon
  instead of storing it locally, so the proposal can follow at once.

**As built (U10).**
- `keychain.store` trims the value, refuses an empty one, a newline or a NUL (in the service and
  account too), sends the command on stdin, checks the exit code and reads the value back through
  the same injected `run`. A value that cannot be encoded is refused before anything is sent, and
  the error holds no copy of it (not on `__context__` either).
- `check` reports every missing required name at once: `verification["names"]`, reason `needs A,
  B - store each with /secret <id> NAME ...`. U16 asks for each in turn and re-proposes once.
- `pathfinder secret <id> NAME`: Ctrl-C or EOF at the prompt stores nothing and exits 1.
- Measured on the real Keychain with throwaway items: a crafted value, service or account cannot
  write a second item or run a second command; the value never shows in `ps`.
- **Decided (D65, U34):** an installed server's entry recorded only `{NAME: "keychain:NAME"}`,
  so start could not tell a required variable from an optional one or know a registry `default`:
  a required variable with a default stopped the check and was asked for as a secret, and a
  missing optional secret disabled the whole server at start although the check trialled without
  it. The value grammar now says both: `keychain:NAME` required, `keychain?:NAME` optional,
  `=<default>` for a non-secret variable, read by one resolver (`mcp/servers._resolve`) for the
  check and the start. Entries written before it are not migrated; a reinstall writes the grammar.

## A private home

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

## pathfinder/memory.py and prompts — the split pass (D47; built in U9, 710 lines)

**Built as specified below.** A known limit remains: the summary call's usage is lost when that
call errors.

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

- **5a.** `state.presence()` returns the newest row across sessions, because the owner has one
  pair of hands. The `session` parameter leaves `presence` and `today`, and the callers change in
  place: `deliver.hold`, and `daemon.py` line 573. **U12, not started.**
- **5b.** Job lines reach a moved screen, through move 2. **U4, not started.**
- **5c.** One EventKit store, built and reset under a lock. **Done (U13).**
- **5d.** `tui.render` clips `gate` and `job.gate` args at 300 characters with an
  `...(clipped; /approvals)` tail. **Done (U11).**
- **5e.** A forced pass that succeeds clears the retry wait. **Done (U9).**
- **5f.** `keychain.secret` misreads a non-ASCII value. **U10, not started.**
  - `find-generic-password -w` prints it as bare hex (a throwaway `비밀abc` came back as
    `ebb984ebb080616263`).
  - When the `-w` output is even-length hex, one `-g` call decides: `password: 0x...` is decoded
    as UTF-8, and `password: "..."` means the output was the value. A real all-hex token stays
    as it is.
  - Why fix the read rather than refuse non-ASCII at `store`: the same reader serves entries the
    owner sets by hand, which a store-side refusal never sees.

## Found by the reviews: five units and the known limits

**U21. The signal envelope filters third-party text.**
- **The defect.** `signals.render_block` dumps a signal's `fields` with
  `json.dumps(ensure_ascii=False)` and no clip, and passes `key` raw. Bidi marks, zero-width
  characters, ANSI sequences and unlimited length reach the model in a signal turn. That holds
  for every sense that carries someone else's text: catalog candidates, mail subjects and
  senders, calendar titles, Claude Code hook messages.
- **The fix** sits at that one place:
  - `summary` passes `oneline(…, 2200)` (raised from 1,000 at review: a `job_done` summary
    carries up to about 2,000 characters of result), `key` passes `oneline(…, 200)`, and every string inside
    `fields` passes `oneline(…, 300)`, recursively.
  - The fields' JSON is then cut to 2,000 characters.
  - `oneline` already turns a newline into a space, so a summary can still never forge a
    `trust:` line.
  - A clean single-line summary is unchanged, so the m3-measured shape holds.
- **Where the filter lives.** `oneline` (U2 wrote it in `catalog.py`) moves to `tools.py`, next to
  `clip`, the other text helper. `tools` imports only `clock` and `exec`, and `signals` already
  imports `tools`, so there is no cycle. `catalog.py` imports it back
  (`from .tools import oneline`), so every existing `catalog.oneline(...)` call keeps working.
- **Files and size:** `tools.py` +8, `catalog.py` −6, `signals.py` +4.
- **After:** U2.
- **Tests:**
  - `test_signals.test_envelope_strips_bidi_zero_width_and_ansi_from_fields`
  - `test_envelope_clips_long_fields_and_summary`
  - `test_single_line_summary_block_unchanged`
  - `test_third_party_senses_pass_the_filter` (a mail, calendar, catalog and claude_code sample
    each)

**U22. An MCP error and a failed proposal are failed calls.**
- **The defect.** `mcp/tools.flatten` returns an `isError: true` result as the string
  `"tool error: ..."`. The handler therefore returns normally, the broker records `ok=True`, and
  the tool record says "ok" for a 401.
- **The same class:** `installer.propose_install_by_name` returns `"no such server; try
  search_tools"` (and every other error) as a result string.
- **The fix.** `flatten` raises `McpToolError(text)` on `isError`, and `propose_install_by_name`
  raises `LookupError(result["error"])`. The broker already turns an exception into
  `{"ok": false, "error": ...}` and `tool_result {ok: false}`, and the model reads the same text.
- **Files and size:** `mcp/tools.py` +3, `installer.py` net 0.
- **After:** U2.
- **Tests:**
  - `test_mcp_is_error_is_a_failed_call` (the record says `failed`)
  - `test_propose_install_error_is_a_failed_call`

**U23. Refusals are failed calls.**
- **The defect.** Two more handlers return a refusal as a success: `memory.Manager.edit` returns
  its vet reasons ("old does not match any line", ...), and `jobs.Manager.start` returns "a job
  is already running" and "the day's job budget is spent".
- Each would put "ok" in the record for something that did not happen, which Invariant 1
  forbids.
- **The fix.** Both raise `ValueError(reason)`. "Already queued for approval" stays a result: the
  proposal exists.
- **Files and size:** `memory.py` +1, `jobs.py` +1.
- **After:** U4.
- **Tests:**
  - `test_edit_memory_refusal_is_a_failed_call`
  - `test_start_job_refusal_is_a_failed_call`
  - the existing tests that compared those strings become `assertRaises`

**U24. A lone surrogate cannot stop the daemon.**
- **The defect.** A lone surrogate (`"\ud83d"` from a JSON client or a model's tool arguments) is
  written by `ledger.append` as a JSON escape. It is read back as a lone surrogate, and SQLite
  refuses to bind it (`UnicodeEncodeError: surrogates not allowed`, reproduced 2026-09-30). The
  projection raises, so `catch_up` at every daemon start, and `reindex`, fail on that line
  forever.
- **The fix** is at the ledger. One `_clean(value)` walks strings in the event and replaces a
  lone surrogate with exactly one U+FFFD (`encode("utf-16", "surrogatepass").decode("utf-16",
  "replace")`; the utf-8 route gives three per surrogate and was dropped at review). It
  runs in `append`, before the line is written, and in `read`, after each line is parsed, so a
  ledger written before the fix also catches up. A valid pair is untouched, because `json.loads`
  already joins it into one character.
- **Files and size:** `ledger.py` +8.
- **After:** nothing. It goes first, before U14a and U15a touch `ledger.py`.
- **Tests:**
  - `test_lone_surrogate_replaced_at_append`
  - `test_old_ledger_with_a_lone_surrogate_catches_up_and_reindexes`
  - `test_astral_characters_untouched`

**U31. Every tool result passes a character filter (D62).**
- **The defect.** Every tool result reaches the model through `broker._clip_for_model`
  (`loop.settle` sends `json.dumps` of what it returns), and it only clips for length. Bidi
  marks, zero-width characters, ANSI sequences and control characters from web pages, mail
  bodies and MCP servers reach the model as they came. U21 filters only the signal envelope
  ("disagree" 14, found at U22's review).
- **The fix:**
  - `tools.clean(text)`: whole ANSI sequences out, every non-printable character (controls, bidi
    marks, zero-width) dropped, a newline and a tab kept, any other whitespace character (a
    carriage return, a no-break space) one space. Nothing is collapsed, so code and page text
    keep their shape.
  - `oneline` becomes `" ".join(clean(text).split())[:n]`: one character rule, and `oneline`'s
    output is unchanged.
  - `_clip_for_model` passes a string result through `clean` before `clip`, and every string in
    a structured result (keys too, however deep) before it is measured. The error text of a
    failed call is a string and takes the same path.
  - One walker, `tools.map_strings(value, fn)`: `signals._clean` (U21) and the broker both use
    it, so the walk over dicts, lists and tuples exists once outside the ledger. `ledger._clean`
    stays its own: the ledger imports nothing above it.
  - The mail body's per-line `oneline` (U25), which collapses runs of spaces, is deleted: the
    body reaches the model only as a tool result, which the broker now filters.
- **What the ledger keeps is unchanged.**
  - The broker emits `tool_result` with what the tool returned, before `_clip_for_model` runs,
    so the ledger keeps the raw result. The filter is on the way to the model.
  - No model reads that raw result: the memory pass's slice skips tool events, and Invariant 1's
    record keeps names, arguments and states only.
  - This is a choice; the reviewer confirms it against Invariant 1.
- **Files and size:** `tools.py` +5 (`clean` +7, `oneline` −2), `broker.py` +6 (the walk, as
  `signals._clean` walks `fields`), `senses/mail.py` −1.
- **After:** U25 (the body filter it replaces) and U6 (`broker.py`).
- **Tests:**
  - `test_broker.test_tool_result_loses_bidi_zero_width_ansi_and_controls` (a string and a
    structured result)
  - `test_tool_result_keeps_newlines_tabs_and_spacing`
  - `test_error_text_passes_the_filter`
  - `test_ledger_keeps_the_raw_result`
  - `test_oneline_is_clean_then_flatten` (the U21 envelope tests pass unchanged)
- **Reversal (D62):** a character a tool's legitimate output needs is allowed back in `clean`,
  in one place.

**U32. Only the owner's activity keeps a session alive (D63); a notice is clean; the approval
line says how to get past it.**
- **The defect (D63).** `state.apply` moves a session's `last_ts` for every event with a session
  except origin `memory`, `presence`, and a signal event that is not `assistant`. Job events
  (origin `job`) and the approval of a job's gate are not excepted, so a job revives a session
  D41 had ended. Reproduced with the real daemon: session A aged three hours, its job emits
  `approval_pending` or `job_done`, and the next `say` lands in A with A's whole history.
- **The fix:**
  - `state.apply`: an event of origin `job`, or one that carries a `job`, does not move
    `last_ts`. The `approval` events for a job's gate (`daemon.py` and `jobs.py`) carry `job`.
  - `state.open_approvals(session)` also returns a pending job gate from any session, so a
    phrase typed in the screen's session gets the harness line (D52) and never reaches a model
    that knows nothing of the job.
  - **The notice text** (`daemon.py`, where a `Notice` is built from a signal's summary) passes
    `tools.clean` (D62): every sense's summary reaches the owner's screen, and until now only
    the model's side was filtered. Found at U25's review.
  - **The approval line** (U6) ends with the way past it, decided 2026-09-30: `... - if you
    meant something else, say it again · 다른 뜻이었다면 다시 말해 주세요`. A short reply to the
    assistant's own question ("응", "no") is otherwise swallowed once with no hint.
- **Files and size:** `state.py` +2, `daemon.py` +2, `jobs.py` +1, `broker.py` +1.
- **After:** U7a (`daemon.py`), U31 (`tools.clean`).
- **Tests:**
  - `test_state.test_job_events_do_not_keep_a_session_alive`
  - `test_daemon.test_say_after_a_job_gate_in_an_aged_session_lands_in_the_new_session`
  - `test_daemon.test_job_done_notice_lands_in_the_screens_session`
  - `test_state.test_open_approvals_includes_a_job_gate_from_another_session`
  - `test_daemon.test_notice_text_passes_clean`
  - `test_broker.test_chat_approval_line_says_how_to_get_past_it`

**U33. The exits U21 and U31 do not cover (found by U31's review, a survey of every exit).**
- **The defect.** Third-party text still reaches the model or the owner's terminal raw through:
  - **The agenda** (`senses/calendar.py` `context`, the second system message of every turn): a
    calendar title is only whitespace-flattened. An invitation is someone else's text; ANSI,
    bidi, zero-width and tag characters ride in every turn. `senses/reminders.py` shares the code.
  - **A sense's summary** for calendar, reminders, schedule and shell (mail and catalog already
    pass `oneline`): it becomes notice text and then a final `assistant` event, so it enters
    history as the assistant's own words. U32 cleans the notice where it is built; the summaries
    themselves pass `oneline` here, so the ledger's signal holds the clean line too.
  - **An MCP tool's parameter schema** (`mcp/tools.py`): parameter names, parameter descriptions
    and `enum` values go to the model as the server sent them; only the top-level description
    is filtered. The `coerce` errors then quote those names.
  - **The gate and job.gate lines** on the screen (`tui.py`): `json.dumps` escapes C0 only, so a
    bidi override can reorder the line the owner approves from.
  - **`mcp/protocol.sanitize`** is a second character rule next to `clean` (it drops tabs and
    leaves ANSI remnants), against D62's one rule.
- **The fix:** each of these passes `tools.oneline` (one line) or `tools.clean` (text that keeps
  its lines); `sanitize` becomes a call to the one rule with its own flattening and length kept.
  No new function.
- **As built, beyond the list above:**
  - **An approval line shows exactly what runs.** `tools.visible(text)` writes every
    non-printable character as an escape (`\u200b`, `\n`); it is used for the `gate` and
    `job.gate` lines, the `say` client's gate line, `pathfinder approvals`, the pin-mismatch
    argument list and `pathfinder tools`' names. `clean` drops those characters, so the owner
    would approve `boss@…` while the call ran with a zero-width character inside it. Notice
    text and a job's goal still pass `clean`.
  - **One predicate, `mcp.tools.refused(entry)`, at every door:** the trial, the pin check and
    `specs()`. A tool is refused when its name breaks the provider's rule, it takes a `control`
    argument, or a top-level parameter name changes under `clean` or the 1,200-character cut.
    A refused tool is never counted, granted, pinned or held; the proposal names it as skipped.
  - Every string in a parameter schema is cut at `MAX_DESCRIPTION`.
  - `Daemon.close()` closes every connection, not only attached ones: a push sense's
    connection could make `wait_closed()` wait forever (a `service stop` that never returns).
  - `senses/claude_code.py`'s summary passes `oneline`.
- **Left as they are** (the review's nits, recorded): a nested key, an `enum` or a `const` value
  with a hidden character is cleaned, not refused, so the server receives the cleaned form; a
  server installed before this partition whose tool is now refused shows only a log warning
  (an `error` event), not a notice; the model's own `delta` is printed as it comes; variation selectors and a few blank-looking
  letters survive `clean`; a zero-width non-joiner is lost (Persian, Hindi); a result nested a
  thousand deep raises; `clean` runs before `clip` on the whole result (0.13 s for 5 MB).
- **Files and size:** `senses/calendar.py`, `senses/reminders.py` (or their shared
  `_eventkit.py`), `senses/schedule.py`, `senses/shell.py`, `mcp/tools.py`, `mcp/protocol.py`,
  `tui.py`; about +10 net.
- **After:** U31 (`clean`), U7b (`tui.py`). **Deploy gate.**
- **Tests:**
  - `test_agenda_lines_pass_the_filter`
  - `test_sense_summaries_are_one_clean_line` (calendar, reminders, schedule, shell)
  - `test_mcp_parameter_schema_passes_the_filter`
  - `test_gate_line_has_no_bidi_or_c1`
  - `test_sanitize_is_the_one_rule`

**Known limits** (listed, not units):
- **A secret whose value starts with `--from ` cannot be stored through a masked input** (U16):
  `keychain.take` reads it as a handle to bind. `pathfinder secret <id> NAME` from a shell
  stores any value.
- **One Keychain read per attach while the mail sense is on** (U26), `say` clients included:
  the ask on attach reads whether the item is there, found or not.
- **A stop during a re-proposal still waits for its trial thread** (U16): `close()` cancels the
  re-proposal at once, but the interpreter joins the worker thread, so the process exits when the
  sandboxed trial ends (at most its 60 s timeout).
- **A steer is recorded when it arrives, not when the model takes it.** A steer typed while the
  second look runs (or between the last chunk and the turn's end) has its `user` event before
  the draft's `assistant` in the ledger and in history, and a steer that gets its own turn is
  seen twice by that turn. Older than this partition; the second look widens the window from
  milliseconds to a request. Fixing it changes how the ledger records a turn: a later
  partition's decision.
- **Nothing shows while the second look runs.** After the last delta the owner waits one request
  before `turn.end`. A steer typed then waits for the look; an interrupt cuts it. (10) measures
  the wait; an indicator needs a socket line this partition does not add.
- **The summary call's usage is lost when it errors (U9).** `memory_pass`'s `usage` sums both
  calls only when the summary call returns.
- **A reindex is needed once after the upgrade.** The tool record (U5) is projected only from
  events applied after it landed, so an existing `state.db` has no record for past turns until
  `pathfinder reindex` runs with the daemon stopped. The README's upgrade note says so.
- **`senses/_eventkit.py` (210 lines) is under no ceiling.** `test_sense_files_under_150_lines`
  skips every `_` file. U17 should cover it: shared sense adapters get a ceiling of 250. That
  matters more now that U25 adds `_imap.py`, so an underscore does not become the way around the
  sense budget.

## Gmail (D57, D59): the mail sense over IMAP, reading and searching

**First, the owner's check. Everything below depends on it.**
- Signed in to the university account, open `myaccount.google.com/apppasswords`.
- If the page offers to create an app password, this section applies.
- If it says the setting is not available for the account, go to the fallback section.
  - App passwords need 2-Step Verification on.
  - A Workspace administrator can switch off 2-Step Verification, app passwords, or IMAP itself.

**What exists** (`senses/mail.py` on `partition-8`, 148 lines):
- `poll`: every 180 s, `UNSEEN` headers only, through `BODY.PEEK` on a read-only `SELECT`.
- `list_mail(limit)`: the newest headers.
- `read_mail(id)`: one message's plain body by Message-ID, cut at 8,000 characters. The id is
  checked before it enters a `SEARCH` string.
- The password is read from the Keychain on each call (`pathfinder-mail`/`<user>`), never kept
  in config.

**What is added (U25):**
- **`search_mail(query, limit=10)`.**
  - **Parameters.** `query` is a string of at most 200 characters. On Gmail, it takes Gmail's
    own search syntax: `from:`, `subject:`, `after:2026/09/01`, `newer_than:7d`, plain words.
    `limit` is an integer from 1 to 20.
  - **The IMAP command.** `UID SEARCH CHARSET UTF-8 X-GM-RAW <literal>` when the server
    advertises `X-GM-EXT-1` (Gmail), else `UID SEARCH CHARSET UTF-8 TEXT <literal>`.
  - **The query goes as an IMAP literal** (`conn.literal = query.encode("utf-8")`) and is never
    interpolated into the command. There is no quoting to get wrong and no CRLF to inject, and a
    Korean query works.
  - **What it returns.** The newest `limit` matches, headers only (`BODY.PEEK[HEADER.FIELDS
    (FROM SUBJECT DATE MESSAGE-ID)]`), as `[{id, from, subject, date}]`. The mailbox stays
    read-only and nothing is marked read.
  - **Known limit:** it searches the configured mailbox (`INBOX`). Mail archived out of the inbox
    is found only when the `mailbox` option names Gmail's All Mail folder, whose name is
    localized.
- **Third-party text passes the same filter as U21.** `from`, `subject` and `date` in
  `list_mail`, `read_mail` and `search_mail` pass `oneline(…, 300)`. A body keeps its newlines
  but loses controls, bidi and zero-width characters, and is still cut at 8,000. (U31 turns the
  body's per-line `oneline` into `clean`, which keeps runs of spaces.)
- **The tools read the sense's bound `HOME`,** as `schedule.py`'s do. Today `list_mail` and
  `read_mail` call `config.home()`, which `signals.load` says a sense must not do.
- **The process-local defect** (`_last_uid`, noted in `mail.py`).
  - The watermark lives in the process, so every restart re-announces all `UNSEEN` mail older
    than the 24-hour dedupe.
  - A Gmail inbox with a thousand unread messages would put a thousand `new_mail` signals in
    the ledger, and in the memory pass's slice, on every restart.
  - Fix: a process's first poll of a `(host, user, mailbox, UIDVALIDITY)` takes the current
    highest UID as its watermark and announces nothing. Mail that arrived while the daemon was
    down is found with `list_mail`/`search_mail`, not announced. +2 lines.
- **The 150-line ceiling.** `mail.py` is at 148, and search, the filter and the watermark add
  about 30.
  - The IMAP plumbing moves to `senses/_imap.py`, an adapter like `_eventkit.py`: `_decode`,
    `_connect`, `_logout`, `_fetch`, `_message_id` and the new search helper, about 65 lines,
    under U17's 250 for adapters.
  - `mail.py` keeps `poll`, the three tools and their specs, about 118 lines.

**The app password reaches the Keychain without the chat (D53) (U26):**
- **Commands.** `/secret mail` in the screen, `pathfinder secret mail` in a shell. The masked
  input and `keychain.store` are the ones U10 and U11 build.
- **One argument means the mail password.** An MCP secret always names a variable
  (`<id> NAME`), so arity tells the two apart, and an MCP server whose id happens to be `mail`
  is not shadowed.
- **One function resolves the target:** `keychain.target(home, id, name=None)`. With no `name`
  it returns the mail sense's `keychain_service` and `user`; otherwise
  `("pathfinder-mcp-" + id, name)`. If the mail sense is not configured, it says "turn the mail
  sense on first".
- **The harness asks (owner's decision, 2026-09-30).** At daemon start, a configured mail sense
  whose Keychain item is missing makes the daemon send `secret.ask {name: "mail"}` to every
  attached screen, the same line U16 sends for an MCP secret; the masked prompt reads "app
  password for <user> (hidden; stored in the Keychain, never shown to the assistant)". The
  skill's step still names `/secret mail` for the case where no screen is attached. The value
  never passes through the chat, the ledger or the model. A model-callable ask for any procedure
  stays later work ("disagree" 11). Cost: `daemon.py` +~4, after U3 and U4 have made room.
  - **As built (U26):** the daemon asks when a screen attaches and the item is missing. At a start
    no screen is attached yet (the socket is bound inside `start()`), so an ask sent then reaches
    no one; `attach` asks the attaching screen instead, while the mail sense is on and
    `keychain.missing` finds no item - one Keychain read per attach while the sense is on. The
    answer is `/secret mail`'s masked entry, stored in the screen's own process.

**What the owner does by hand: 7 steps, 3 of them on Google's pages.**
1. The check above (`myaccount.google.com/apppasswords`).
2. 2-Step Verification on, if it is not.
3. Create an app password named PATHFINDER, and keep the 16 characters on screen.
4. `pathfinder config set senses '...'`. This is the merged `senses` object with
   `"mail": {"host": "imap.gmail.com", "user": "<the address>"}` added. `config set` replaces the
   whole object, so the line must carry the existing senses too; `init` prints that merged line.
5. `/secret mail`, then paste the app password.
6. `pathfinder service restart`: senses load at start.
7. "What's in my inbox?" A login refusal here means IMAP is off for the account.

The shipped skill `connect-gmail` walks these steps (the skills section).

**Units.**
- **U25:** `senses/_imap.py` (new) and `senses/mail.py`: search, the filter, `HOME`, the
  watermark, the move. About 65 + (−30) lines. After U21. Tests:
  - `test_search_mail_uses_x_gm_raw_on_gmail_and_text_elsewhere`
  - `test_query_goes_as_a_literal_utf8`
  - `test_search_never_marks_read`
  - `test_headers_pass_the_filter`
  - `test_first_poll_takes_the_watermark_silently`
  - `test_mail_tools_use_the_bound_home`
  - `test_mail_under_150`
- **U26:** `keychain.target`, `tui.py` (`/secret mail`) and `__main__` (`secret mail`), about
  +12. After U15b. Tests:
  - `test_secret_mail_resolves_service_and_user`
  - `test_secret_mail_unconfigured_says_so`
  - `test_mcp_server_named_mail_is_not_shadowed`

## Gmail's fallback: a local Google server with its own OAuth (only if the check fails)

This is designed only if the owner's account refuses an app password. The path is a registry MCP
server for Gmail that runs Google's OAuth itself, installed through the tool path (D3). What it
costs:
- **A Google Cloud project, made by hand.** Enable the Gmail API, configure the OAuth consent
  screen, create a desktop OAuth client, download its JSON, and point the server at it. That is
  about ten steps in Google's console.
- **A refresh token that expires every 7 days** while the consent screen is in "testing" status,
  so consent is repeated weekly. Leaving testing needs Google's verification for Gmail's
  restricted scopes.
- **The server keeps its tokens in a plain file outside the Keychain,** against D53 and the
  "secrets in the Keychain" rule, unless the chosen server supports a keyring.
- **The administrator can block this too.** A Workspace administrator can refuse third-party
  OAuth clients. Then D57's reversal applies: Gmail waits for the external server's design.

## Skills (D58, D61)

This amends the blueprint, which cut `skills/`: lines 86, 92 and 338 (§12) of
`docs/blueprint/pathfinder-blueprint.html` listed it among the things taken out. Since D61 those
three places say D58 and D61 brought it back, as one directory of procedures the owner approved.

**What a skill is.** A procedure the owner has checked, which the assistant reads when asked to
do that thing, so it stops inventing one. On 2026-09-26 it invented a way to get a Slack token.
A skill is an instruction, so it is trusted in a way a wiki page (knowledge, data) is not.

**Where skills live: one place (D61).**
- **Every skill is `<home>/skills/<name>.md`** (0600, like the rest of the home), whatever its
  source: the owner's hand, an approved proposal, or a seed.
- **Seeds:** `pathfinder/skills/<name>.md`, package data next to `prompts/`. `pyproject.toml`'s
  `package-data` gains `"skills/*.md"`. Nothing reads a seed except the copy below.
- **The seed copy** is the mechanism `rules.load` uses for `RULES.md` and `USER.md`: at daemon
  start, a missing file is written from the shipped one (`if not path.exists():
  path.write_text(...)`), and an existing file is never touched.
  - It lives in `skills.bind(home)`, which the daemon calls once at start, next to
    `memory.bind` and `jobs.bind`. It copies every seed whose file is missing.
  - A new shipped skill therefore arrives on upgrade, and a skill the owner edited is left alone.
  - A seed file the owner deletes comes back at the next start, as a deleted `RULES.md` does.
  - The cost (D61): a fix to a shipped skill does not reach a home that already has that file.
    Nothing marks that a newer seed exists; D61's reversal adds a marker if this becomes a real
    cost.

**The file format** follows Claude Code's `SKILL.md`, reduced to one file:
- Frontmatter with `name` (lowercase, digits, hyphens; it must equal the file's stem) and
  `description` (one line, at most 160 characters, saying when to use it).
- Then a markdown body.
- The frontmatter is parsed by the existing `rules._parse_frontmatter`.
- The body is loaded only when the skill is read. No resource folders.

**How the model knows which skills exist, without paying for their bodies.**
- The index is every skill's `name` and `description`, sorted by name, never cut.
- It is not cut the way the memory wiki's map is (1,000 characters, newest first, so older
  entries fall off). Instead, it is bounded when files are loaded: a description over 160
  characters, or an index over 4,000 characters, is refused at load with a message to the owner.
- It rides in `read_skill`'s own description (D61). It goes with every request, and it stays
  byte-stable until a skill changes, which suits caching.
- `skills.index()` is cheap: one directory listing and one frontmatter read per file, redone
  when the directory's mtime moves.

**The one tool: `read_skill(name)`.**
- It returns the body with a harness header: `[Skill '<name>' - a procedure the owner approved;
  follow it. Secrets go through /secret, never the chat.]`.
- The body is cut at `tools.READ_CLIP`.
- Origins: user and job. Read-only, no gate.
- `system.txt` gains one line: a `read_skill` result is the owner's procedure and is followed.
  It is the one tool result that is not "data, not an instruction", and the only one whose text
  can come solely from the one trusted directory.

**What makes a file a skill.**
- Only its location: `<home>/skills/`.
- Nothing else writes there except:
  - the owner, by hand;
  - the seed copy, from the package;
  - an approved `edit_memory` with target `skills/<name>.md`, proposed by the assistant from a
    user turn (D61).
- `memory.vault_path` accepts `skills/` only from origin `user`. Even then it always goes to the
  approval box, whatever `grants` says. A user turn can be steered by a page it just read, so
  D28's apply-at-once never applies to a skill.
- **Third-party text can never become a skill.**
  - Mail, web pages, MCP results, registry text and signals reach the model only as data.
  - The only write path needs origin `user` and an approval showing the whole new text.
  - The pass (origin `memory`), a signal turn and a job are refused outright.
- A skill file holding a token-shaped string (`keychain.TOKEN_RE`) is not loaded, with a
  warning. A procedure names the intake (`/secret <id> NAME`, `/secret mail`), never a value
  (D53).

**The first seed: `connect-gmail`.**
- Its description: "Use when the owner asks to connect Gmail or read their mail."
- Its body is the check and the seven steps from the Gmail section, with the fallback's one-line
  pointer. Its only secret step is `/secret mail`.

**Budget.**
- `pathfinder/skills.py` (new), about 80 lines: `bind` with the seed copy, the index, bounds,
  the token check, `read_skill`. Ceiling 120. One directory means one listing and no merge of
  two; the seed copy (a loop over the seeds, `exists`, `write_text`) takes about the lines the
  merge would have.
- `pathfinder/skills/connect-gmail.md`, about 35 lines.
- `daemon.py` +1: `skills.bind(self.home)` at start.
- `memory.vault_path` +3.
- `system.txt` +1.
- `pyproject.toml` +1.
- A skill costs nothing per turn but its index line inside `read_skill`'s description: about 40
  tokens each.

**Units:**
- **U27:** `skills.py`, `pyproject.toml`, `daemon.py` (`skills.bind`) and tests.
- **U28:** `memory.vault_path` (the `skills/` target, user-only, always approved) and
  `system.txt`, plus tests.
- **U29:** `pathfinder/skills/connect-gmail.md`, after U25 and U26.

**Tie to the owner's direction for the server (D44/D45).** A package the server hands over is
tools plus the skill that uses them. This file format is what the server would deliver beside
the connection spec: installing a package would place its skill in the same `<home>/skills/`
(one directory, whatever the source; D61) under the same one approval as the install (D3), with
its source named in the frontmatter. The server's design starts from that pair and is not
designed here.

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
- **§2:** the modes, the vault-and-handle door, and Invariant 2.
- **§6:** the second look.
- **§3/§7:** the tool record (Invariant 1), approvals typed in chat, and D53's `secret.ask` /
  `secret`.
- **§10:** the two calls.
- **§11:** the copy and D40.
- **§12:** the new invariants: the rows listed under "the invariant text" above, plus the others.
- **§11:** also the OAuth way in, once decided.
- **§14:** `secret`, and `search` reading the copy.

- **§8:** `search_mail`, the filter, `/secret mail`, and `_imap.py`.
- **§1 and a new section:** `<home>/skills/`, and the seeds and their copy (D61).

Both languages, one commit.

**The README pair:** `/secret` replaces the `security` line for MCP servers, and `/secret mail`
replaces it for the mail password. `docs/senses.md` lines 96-102 change the same way. The
README's upgrade note says to run `pathfinder reindex` once.

## Where the decisions and the code disagree

1. **D40.** It says "the trial simply runs first", but `check` already reads the Keychain before
   the trial. What blocks a token server is different:
   - there is no way in for the secret;
   - a missing required secret does not stop the check;
   - a required variable marked `isSecret: false` is never resolved.

   The fix is the intake plus a stop (U10). U0's survey keeps D40: 4 of the owner's 7 services
   take a static token.
2. **D49** names the per-module ceilings in `test_invariants.py` as the gate. Several have no
   test:
   - 10 of the 13 core rows;
   - `mirror.py`'s 200 (D56);
   - `senses/_eventkit.py` (210 lines; the sense test skips `_` files).

   U17 adds all of them.
3. **D52 against blueprint §11,** "only what was said is remembered". Amended to Invariant 1.
   Built in U5, which replaced `test_restore_said_only`.
4. **D51 against `loop.run`'s rule** that everything streamed reaches history. Amended to
   Invariant 2. The code lands in U7a.
5. **D30** lists the consolidation pass. D47's split took it out of the write call (U9). The
   second look needs a step left, so a draft at the last step stands.
6. **HISTORY's forced-`memory run` defect** was the wait after a successful forced run. Fixed in
   U9.
7. **m10** replayed the last 12 messages, while live sends the whole session. The exit re-run
   inherits this.
8. **Invariant 2's "always" is not true today.** A model error in the middle of a stream drops
   the text already streamed. U7a attaches `said` to the error and records it.
9. **Invariant 1's record says "ok" for a call that failed or refused.** Four handlers return an
   error or a refusal as a result string: MCP `isError`, `propose_install`, `edit_memory`'s vet,
   and `start_job`'s refusals. U22 and U23 fix them.
10. **D58 against the blueprint,** which cut `skills/` (lines 86, 92 and 338 of
    `pathfinder-blueprint.html`). Resolved on 2026-09-30 by D61: the owner confirmed the design,
    and those three places now say skills came back as one directory of approved procedures.
11. **D53 against the mail password.** D53's main path is the harness asking. Resolved on
    2026-09-30: the daemon asks at start when the mail sense is configured and its Keychain item
    is missing (U26). A model-callable ask that any procedure could use is later work.
12. **D36 against `installer.py`'s ceiling.** The installer reached 700/700 at U3 (it started at
    680, not the 677 this plan assumed). Resolved on 2026-09-30 by D60: D36's foreign-file
    helper moves to `config.py` (U30), before U10.
14. **MCP tool results against the envelope filter (found at U22's review).** `mcp/tools.py`
    clips a tool result's text at `READ_CLIP` and passes everything else raw: bidi marks, ANSI
    sequences, newlines and `trust:` lines reach the model as a tool result. U21 filters only
    the signal envelope. Resolved on 2026-09-30 by D62: every tool result and error string
    passes `tools.clean` in `broker._clip_for_model` (U31). Newlines are kept on purpose (a
    result may be code or a page); a tool result is its own `tool` message, apart from the
    signal block whose `trust:` line U21 protects.
13. **The mail tools against `signals.load`'s rule.** `list_mail` and `read_mail` read
    `config.home()`, where the rule says a sense uses the home it was loaded with. U25 fixes it.

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
- **D53's backstop** (vault and handle):
  - `test_paste_vaulted_and_handle_returned` (the file, the returned event and history hold the
    handle)
  - `test_bind_moves_and_deletes_the_pasted_item`
  - `test_unbound_paste_expires_after_24h`
  - `test_expired_handle_says_so`
  - `test_keychain_failure_falls_back_to_redaction`
  - `test_scanner_uses_the_package_pattern`
  - `test_pasted_token_reaches_the_record_as_a_handle`
- **D53's main path:**
  - `test_secret_ask_sent_on_missing_secret`
  - `test_secret_value_never_reaches_ledger_or_state`
  - `test_secret_stored_reproposes`
  - `test_say_client_gets_the_slash_secret_line`
- Token-shaped strings are built by concatenation.
- **The invariant tests:** every test named under "the invariant text", with
  `test_restore_said_only` replaced.
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
  - `mirror.py` <= 200 (D56)
  - `test_second_look_matches_the_bench`: `VERIFIER_PROMPT`, `RERUN_NOTE` and `FLAG_WORDS`
    byte-identical to `bench/m10_unsupported.py`, and `verifier_input` equal to the bench's for a
    fixture turn
  - `test_home_stays_private`
  - no new ledger type
  - only `catalog.py` names the registry URL
- **U21 (envelope):**
  - `test_envelope_strips_bidi_zero_width_and_ansi_from_fields`
  - `test_envelope_clips_long_fields_and_summary`
  - `test_single_line_summary_block_unchanged`
  - `test_third_party_senses_pass_the_filter`
- **U22, U23 (failed calls):**
  - `test_mcp_is_error_is_a_failed_call`
  - `test_propose_install_error_is_a_failed_call`
  - `test_edit_memory_refusal_is_a_failed_call`
  - `test_start_job_refusal_is_a_failed_call`
- **U24 (surrogates):**
  - `test_lone_surrogate_replaced_at_append`
  - `test_old_ledger_with_a_lone_surrogate_catches_up_and_reindexes`
  - `test_astral_characters_untouched`
- **U31 (tool results):**
  - `test_tool_result_loses_bidi_zero_width_ansi_and_controls`
  - `test_tool_result_keeps_newlines_tabs_and_spacing`
  - `test_error_text_passes_the_filter`
  - `test_ledger_keeps_the_raw_result`
  - `test_oneline_is_clean_then_flatten`
- **U25, U26 (Gmail):** as listed in the Gmail section.
- **U27-U29 (skills):**
  - `test_seed_is_copied_when_missing_and_never_overwrites`
  - `test_index_lists_every_skill_sorted_and_uncut`
  - `test_index_over_bound_is_refused_at_load`
  - `test_read_skill_header_and_clip`
  - `test_skill_with_a_token_is_not_loaded`
  - `test_skills_target_user_only_and_always_approved`
  - `test_connect_gmail_is_shipped_and_parses`
- **U17 (the ceilings):**
  - 1.3x for every §6 row
  - `mirror.py` <= 200
  - `senses/_*.py` <= 250
  - `skills.py` <= 120
  - plus the invariant tests listed above

## Units and build order

Each unit is one implementer call: 1-3 files plus their tests, briefed from a file in the session
scratchpad. Units that share a file run one after another, in table order; the rest may run in
parallel. **Deploy gate: U15a, U21, U31 and U33.**

| # | State | Unit | Files | Size | After | Key tests |
|---|---|---|---|---|---|---|
| U0 | **done** | Survey: D40 kept (4 of 7 services take a token), secret-or-required, deletions arrive through `updated_since`, paging facts, `security -i` escaping, the `-w` hex defect | — | — | — | — |
| U1 | **done, merged** | The registry copy | `mirror.py`, `catalog.py` | 188 + 275 | — | `test_mirror.*`, `test_catalog.*` |
| U5 | **done, merged** (U5b in it) | The tool record, four states | `state.py` | 681 | — | `test_history_is_said_and_called_only`, `test_no_tool_record_without_tool_call_events` |
| U9 | **done, merged** | The split pass, 5e | `memory.py`, `consolidate.txt`, `summarize.txt` | 710 | — | `test_memory.*` |
| U13 | **done, merged** | 5c: one EventKit store under a lock | `senses/_eventkit.py` | 210 | — | `test_calendar.*` |
| U2 | **done**, merged at 429752d | Readers of the copy | `senses/catalog.py`, `installer.propose_by_name`, `__main__.search_cmd` (and `mirror.py`/`catalog.py` touches) | mirror 191, catalog 278, installer 680, sense 139 | U1 | `test_catalog.*`, `test_installer.*` |
| U24 | **done, merged** | Lone surrogates (one U+FFFD each, utf-16 route) | `ledger.py` | 132 (+10) | — | `test_old_ledger_with_a_lone_surrogate_catches_up_and_reindexes` |
| U21 | **done, merged** | The envelope filter (**deploy gate**); summary cap 2,200 | `tools.py`, `catalog.py`, `signals.py` | 225, 268, 211 | U2 | `test_envelope_strips_bidi_zero_width_and_ansi_from_fields` |
| U22 | **done, merged** | MCP error and failed proposal as failed calls | `mcp/tools.py`, `installer.py` | 122, 682 (+2; the contract's net 0 was not reachable) | U2 | `test_mcp_is_error_is_a_failed_call` |
| U3 | **done, merged** | Move 1: `install`/`uninstall` into the installer | `installer.py`, `daemon.py` | daemon −21, installer +15 | U22 | `test_handle_install_and_uninstall_replies` |
| U4 | **done, merged** | Move 2, 5b | `jobs.py`, `daemon.py` | daemon −10, jobs +15 | U3 | `test_job_lines_reach_a_moved_screen` |
| U30 | **done, merged** | D60: the foreign-file helper moves to `config.py` | `config.py`, `installer.py`, `init.py` | installer −40, config +40 | U3 | the existing `merge_json_file` tests, import path changed |
| U23 | **done, merged** | Refusals as failed calls | `memory.py`, `jobs.py` | +2 | U4 | `test_start_job_refusal_is_a_failed_call` |
| U6 | **done, merged** | Approvals typed in chat (D52-2) | `broker.py`, `state.py` (`open_approvals`), `daemon.py` (`turn`) | daemon +7, broker +26, state +6 | U4 | `test_chat_approval_answered_without_a_model_call` |
| U7a | **done, merged** | The second look (D51), plus a mid-stream error's text | `loop.py`, `daemon.py` (`_run_turn`) | loop +47, daemon +6 | U6 | `test_flagged_draft_is_withdrawn_and_rerun_once`, `test_every_streamed_delta_is_in_the_ledger` |
| U7b | **done, merged** | The `retract` line | `tui.py` (`render`), `__main__.py` (`_stream`) | +4 | U7a | `test_retract_rendered` |
| U8 | **done, merged** | The adopted prompt | `prompts/system.txt` | — | before U18 | `test_second_look_matches_the_bench` (in U17) |
| U10 | **done, merged** | Intake, D40, 5f | `keychain.py` (`store`, fixed `secret`), `installer.py` (`check` step 4), `__main__.py` (`secret`) | +36 | U3, U7b | `test_store_escapes_backslash_then_quote`, `test_missing_required_secret_stops_before_the_trial` |
| U11 | **done, merged** | `/secret`, 5d (bracketed paste, a 50 ms drain) | `tui.py` | +22 | U7b, U10 | `test_slash_secret_masks_and_never_touches_the_socket` |
| U12 | **done, merged** | 5a | `state.py`, `deliver.py`, `daemon.py` line 573 | ~0 | U6 | `test_presence_is_the_newest_row_across_sessions` |
| U14a | **done, merged** | Born private | `ledger.py`, `state.py` | +1 | U12, U24 | `test_partition_and_lock_born_0600_dir_0700` |
| U14b | **done, merged** | Existing files private | `config.py` (`make_private`), `__main__.py` (daemon branch), `init.py` (literals) | +11 | U10 | `test_make_private_drops_group_other_keeps_owner_x_skips_symlinks_and_workspace` |
| U15a | **done, merged** | D53's backstop: the vault at the ledger door (**deploy gate**); D66: handles are `pasted-<seq>[-i]`, only the owner's text is vaulted | `ledger.py`, `keychain.py` (`TOKEN_RE`, vault, mask, sweep, index), `scripts/scan_personal.py`, `daemon.py` (gate and steer lines from the recorded event), `jobs.py` (the same; a job's own masked copies) | +127 (`keychain.py` +114 to 172, `ledger.py` +7 to 140, scanner +3, `daemon.py` +2 to 768, `jobs.py` +1) | U14a, U10 | `test_paste_vaulted_and_handle_returned`, `test_handle_is_the_event_seq`, `test_a_token_in_a_tool_result_is_redacted_without_the_keychain`, `test_pasted_token_reaches_the_record_as_a_handle` |
| U15b | done, in review | Binding a handle; the `pasted {handles}` line tells the owner the handle a pasted token got | `keychain.py` (`bind`), `__main__.py` (`secret --from`), `tui_secret.py` (`/secret --from`), `daemon.py` (`record`: `pasted`), `tui.py`/`__main__.py` (render it) | keychain +20, tui_secret +9, `__main__` +5, daemon +2, tui +2 | U15a, U11, U14b | `test_bind_moves_and_deletes_the_pasted_item` |
| U16 | done, in review | D53's main path: the harness asks (trigger on `names`; stored through `keychain.take`; `secret.stored` to every screen; the handler `installer.secret` also takes `mail`) | `daemon.py` (`record` hook, `_ask_secret`, `secret` branch), `tui.py`/`tui_secret.py` (`secret.ask`), `installer.py` (`secret`, `close`, event fields), `keychain.py` (`take`, `live`), `ledger.py` (`locked`) | daemon +9, tui +18, tui_secret +25, installer +25, keychain +20, ledger +12 | U15b | `test_secret_value_never_reaches_ledger_or_state` |
| U25 | **done, merged** | Gmail: `search_mail`, the filter, `HOME`, the watermark, the IMAP move | `senses/_imap.py` (new), `senses/mail.py` | +65 / −30 | U21 | `test_query_goes_as_a_literal_utf8`, `test_first_poll_takes_the_watermark_silently` |
| U31 | **done, merged** | Every tool result passes `tools.clean` (D62); `oneline` is `clean` plus flattening; the mail body filter becomes `clean` | `tools.py`, `broker.py`, `senses/mail.py` | tools +5, broker +6, mail −1 | U25, U6 | `test_tool_result_loses_bidi_zero_width_ansi_and_controls`, `test_tool_result_keeps_newlines_tabs_and_spacing`, `test_error_text_passes_the_filter`, `test_ledger_keeps_the_raw_result`, `test_oneline_is_clean_then_flatten` |
| U26 | done, in review | The mail password intake; the daemon asks when a screen attaches and the item is missing | `keychain.py` (`target`), `tui.py`, `__main__.py`, `daemon.py` | +12, daemon +4 | U16, U15b | `test_secret_mail_resolves_service_and_user`, `test_missing_mail_password_is_asked_at_start` |
| U32 | **done, merged** | D63: only the owner's activity keeps a session alive; notice text passes `clean`; the approval line's way out | `state.py`, `daemon.py`, `jobs.py`, `broker.py` | +6 | U7a, U31 | `test_say_after_a_job_gate_in_an_aged_session_lands_in_the_new_session` |
| U33 | **done, merged** | The remaining raw exits: the agenda, sense summaries, MCP parameter schemas, gate lines, `sanitize` (**deploy gate**) | `senses/*.py`, `mcp/tools.py`, `mcp/protocol.py`, `tui.py` | +10 | U31, U7b | `test_agenda_lines_pass_the_filter` |
| U34 | **done, merged** | An installed server's entry records which variables are optional and which have a default; start agrees with the check | `installer.py`, `catalog.py`, `mcp/servers.py` | ~+7 | U10 | `test_required_with_a_default_does_not_stop_and_gets_the_default` |
| U27 | not started | Skills (D58, D61): the seed copy, the index, `read_skill` | `skills.py` (new), `pyproject.toml`, `daemon.py` (`skills.bind`) | ~80, +1, daemon +1 | — | `test_seed_is_copied_when_missing_and_never_overwrites`, `test_index_lists_every_skill_sorted_and_uncut` |
| U28 | not started | Skills: the write path (a proposal from a user turn, always approved) | `memory.py` (`vault_path`), `prompts/system.txt` | +4 | U27, U8 | `test_skills_target_user_only_and_always_approved` |
| U29 | not started | The first seed | `pathfinder/skills/connect-gmail.md` | ~35 | U27, U25, U26 | `test_connect_gmail_is_shipped_and_parses` |
| U17 | not started | Ceilings and invariant tests | `tests/test_invariants.py` | — | last | the U17 list above |
| U18 | not started | Exit runs: m9 on the split pass (~40k tokens), m10 on the adopted prompt with the record and the second look (~215k tokens), each with the lead's go-ahead | `bench/m9_memory_write.py`, `bench/m10_unsupported.py` | — | U5, U7a, U8, U9 | — |
| Lead | — | Live (1)-(16); both doc pairs, `docs/senses.md`; HISTORY; the reviewer (opus, read-only); the scan; the commit on `build`; deploy only past the gate | — | — | all | — |
