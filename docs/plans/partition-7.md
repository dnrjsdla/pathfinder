# Partition 7 — the installable form: `pathfinder init`, launchd, the screen, anthropic (experimental), README

**"It runs" =** (1) **the ten-minute check, done by a person**: a fresh standard user account on
a Mac (System Settings -> Users -> add; the closest thing to a clean Mac short of a wipe), the
README's steps and nothing else - prerequisites, `git clone`, `pathfinder init`, first `say`
answered by a cloud model - in ten minutes by the clock, without asking anyone; the time and
every place the person hesitated go into HISTORY (the owner's part, or a teammate's; agents
cannot make user accounts); (2) live: `pathfinder init` in a temp `$HOME`/`$PATHFINDER_HOME`
walks the checks in order (Python, Ollama reachable, a model chosen, `npx` present or warned),
sets the model through `config.set`, turns the secret-free senses on, installs the shell hook and
the Claude Code hooks only when answered yes and idempotently (running it twice changes nothing),
writes the launchd agent and starts it - the socket appears, `say` answers; (3) live: the
launchd-managed daemon: `pathfinder service stop` sends SIGTERM and the daemon closes its MCP
servers (no orphaned `npx` afterwards), `service start` brings it back, `service status` says
which, a config change followed by `service restart` takes effect; (4) live: `pathfinder tui`
attached for an hour of real use: deltas stream, a notice that fires while the user is typing
waits until the hands stop and then prints without tearing the input line, `+`/`-` on the last
notice records the reaction, `/approve ID` answers a gate inline, Ctrl-C during a stream
interrupts the turn (a second Ctrl-C quits), `/quit` leaves the daemon running; (5) live: a
`say` that arrives while a *signal* turn is running starts its own turn when that turn ends
instead of steering the signal's (partition 6's open defect; the TUI would hit it every day);
(6) `llm/anthropic` drives one recorded stream to a tool call and one to a 429 -> `blocked`
(tests only; never run live under D7, marked *experimental* everywhere it is named); (7) the
README in both languages carries the ten-minute path and nothing in it names a model.

Decisions this partition rests on: D2 (two adapters: OpenAI-compatible and Anthropic native),
D3 (macOS first), D6 (README and the screen in Korean and English; `ARCHITECTURE.ko.md` in the
same commit), D7 (no paid keys; direct providers are experimental, tested with recorded
responses), D15 (context sources turned on one by one in onboarding), D17 (a say during the
user's own turn is a steer - the fix in (5) narrows it to exactly that), D25 (one slot; the
adapter dispatch sits under it), D28 (the approval box), D33 (the installer already owns
`~/.claude.json`; onboarding owns `~/.claude/settings.json` the same way), D34 (core cap 7,000; the 1.3x
line is the gate), D35 (a scrollback line client), D36 (one foreign-file helper in `installer.py`)
and D37 (the Anthropic adapter now, last unit, recorded tests only).

NOT in this partition: Linux and Windows (D3); a signed `.pkg`, a Homebrew formula or `pipx`
publishing (a `git clone` + `pip install .` is the install; packaging is post-public work);
self-update; a menu-bar app, a second screen or a shared render layer (blueprint §17); a
locale switch (D6 says both languages side by side, so the screen has no language setting);
OAuth for any provider; Keychain-backed provider keys (D7: there are no keys - `api_key_env`
stays as it is, and the experimental adapter reads the same); curses or any full-screen layout
(D35); scrolling history or line editing beyond backspace and a redraw in the TUI (a real editor
is `readline`'s job when the owner wants it); the mail, calendar and reminders senses inside
`init` beyond printing the line that turns each on (they need a password or a system permission
prompt; `init` must not block on either); anything in the partition-8 list (two weeks of use).

Line budgets (blueprint §6 / §15): `tui.py` <= 400 (the client layer - outside the core, like
`mcp/`); `init.py` ~250 (new, core - no budget row in §6; the invariant test gets a 300 line
ceiling); `llm/anthropic.py` ~150 within `llm/`'s 600 adapter budget (225 used today);
`daemon.py` 756 today, hard line 780 (+~15: the SIGTERM handler and the non-user-turn queue);
`__main__.py` +~40 (`init`, `service`, `tui`). **The core is 6,379 lines against the
blueprint's 5,500 cap already; this partition adds about 300 core lines; D34 raised the cap to
7,000 and made the per-module 1.3x line the measured gate.**

## Entrance measurement

None is listed for partition 7 in the blueprint (§13 ends at the partition 6 entrance). The
measurement that belongs to this partition is the exit one - (1) above, a person with a clock -
and it is recorded in HISTORY as `m8 - ten minutes to the first conversation` with the elapsed
time, the Python and Ollama versions the account had, and each hesitation. It does not gate the
commit (a person's hour cannot be re-run by a test); it gates the partition-8 start.

## pathfinder/init.py — onboarding and the service (~250 lines)

`pathfinder init` reads answers from stdin (`input()`; the test pipes them), prints one line per
check - `ok` or the exact fix line - and stops at the first hard failure:

1. **Python** >= 3.11 (`pyproject.toml`'s floor; the researcher confirms the code actually runs
   on 3.11 - `match`, `ExceptionGroup`, `tomllib` and f-string nesting are the usual traps - or
   the floor moves up and the README says so).
2. **Ollama**: `GET <providers.ollama.base_url>/models` answers (the OpenAI-compatible list);
   not answering -> the fix line (`brew install ollama` / open the app / `ollama signin` for
   cloud models - the exact 2026 flow is the researcher's first deliverable). The answer's
   model ids are listed with numbers and the user picks one; the pick goes through
   `config.set("model", "ollama:<id>")`. No model name is in the code or the README (the list
   comes from the machine). An empty list prints the fix line and stops. `embed_model` is
   offered from the same list when an embedding model is present (the mail-sense way: an
   `embedding` in the id, or the user says which); skipping it is fine (FTS only).
3. **`npx`** (`shutil.which`): missing is a warning with the fix line, not a stop - the tool path
   needs it, the first conversation does not.
4. **Home**: `config.home()` (0700), `RULES.md`/`USER.md` are the daemon's first-start job
   already - `init` does not write them.
5. **Senses**: the secret-free ones are offered on with one yes/no each - `attach`, `catalog`,
   `schedule`, `reminders`, `shell`, `claude_code` - merged into `senses` through one
   `config.set` (never replacing what is there). `mail` and `calendar` print the line that turns
   them on (the Keychain line, the permission note) and are not asked.
6. **Shell hook** (only when `shell` was answered yes): the zsh snippet from `docs/senses.md`
   is written to `<home>/hooks/pathfinder.zsh` and one guarded line
   (`[ -f "<home>/hooks/pathfinder.zsh" ] && source "<home>/hooks/pathfinder.zsh"`) is appended
   to `~/.zshrc` after a yes, skipped when the line is already there. Appending to `~/.zshrc`
   is the one foreign edit that is not a JSON merge: backup `~/.zshrc.pathfinder-<ts>` first,
   append with a trailing newline, never rewrite the file.
7. **Claude Code hooks** (only when `claude_code` was answered yes): the two hook entries from
   `docs/senses.md` merged into `~/.claude/settings.json` (`hooks.Stop[]`, `hooks.Notification[]`)
   with the installer's discipline - backup, atomic write, read-back deep-compare of every other
   key - and idempotent: an entry whose command already contains `pathfinder signal claude_code`
   is not added again. The helper that does backup + atomic write + read-back lives in
   `installer.py` today, shaped for `mcpServers`; it becomes one function taking the path and
   a `mutate(dict) -> dict` and both callers use it; it stays in `installer.py` (D36).
8. **launchd** (after a yes): `~/Library/LaunchAgents/pathfinder.daemon.plist` - `ProgramArguments`
   = the running interpreter + `-m pathfinder daemon`, `EnvironmentVariables` with `PATH` (the
   interpreter's directory, `/opt/homebrew/bin`, `/usr/local/bin`, the system path - `npx` and
   `ollama` must be findable without a login shell) and `PATHFINDER_HOME` when it was set,
   `RunAtLoad`, `KeepAlive`, `StandardOutPath`/`StandardErrorPath` = `<home>/daemon.log`
   (the daemon's stderr is its log today; a pipe nobody reads is the m5 deadlock). Then
   `launchctl bootstrap gui/<uid> <plist>` (macOS 26 syntax - the researcher confirms;
   `bootout` to stop). Written with `plistlib`. `init` ends by waiting up to ten seconds for the
   socket and printing `say` as the next step.

`pathfinder service start|stop|restart|status` (~40 lines in the same file): `bootstrap` /
`bootout` / both / `launchctl print gui/<uid>/pathfinder.daemon` reduced to one line plus
whether the socket answers (`daemon.is_serving`). The `launchctl` calls are one injectable
function so the tests never touch the real launchd.

**The daemon under launchd** (`daemon.py`, +~15): `loop.add_signal_handler(SIGTERM, ...)` sets
the same shutdown the KeyboardInterrupt path runs today (`close()`: MCP servers closed, MCP
children killed as a group, the outbox flushed) - without it `service stop` orphans every `npx`
(m5's bench had to `killpg` by hand for this reason). And the partition-6 defect: in `handle`,
`say` steers only when the running turn's `origin` is `user`; otherwise it is queued as its own
turn (`session.queue.put_nowait`), which the lane picks up when the signal turn ends. One
attribute on the session records the running turn's origin (set where `control` is set).

## pathfinder/tui.py — the screen (<= 400 lines, stdlib only)

One attached client for the whole sitting. `pathfinder tui` connects, sends `attach {cwd}` (the
context source the catalog reads - today only the CLI's one-shot `say` sends it), puts the
terminal in cbreak mode (`tty`, `termios`), and multiplexes stdin and the socket with
`selectors`. The screen is the terminal's own scrollback - no curses, no panes (D35): output
prints above, one input line stays at the bottom and is redrawn (`\r\033[K` + the prompt + the
buffer) after anything prints. What it does with each key: printable -> the buffer, `presence
{idle_s: 0}` sent at most once per second while keys arrive and `presence {idle_s: n}` every 30 s
of silence (this is the **only real producer of `presence`** - the deliver state machine's
"the user is typing" branch and the `away` condition have had no live source since partition 3);
Enter -> `say` (or a slash command); Backspace; Ctrl-C while a stream is running -> `interrupt`,
otherwise quit; Ctrl-D -> quit. Slash commands, each one socket message the CLI already sends:
`/approve ID`, `/deny ID`, `/approvals` (prints the box the CLI way, through `__main__`'s
renderer), `/jobs`, `/quit`; `+` and `-` alone on the line -> `react up|down` on the latest
delivery. Each server message renders the way `listen` renders it today (delta, notice, gate,
job.*, error, turn.end) - the renderer moves out of `__main__.listen` into `tui.render(msg) ->
str` and `listen` calls it too (one renderer). Fixed strings are bilingual side by side
(`승인 대기 · waiting for approval on ...`), about a dozen of them, in one table at the top of the
file (D6; no setting). Notices are sanitized before the terminal (`mcp.protocol.sanitize`, the
partition-6 rule) - a signal's summary carries third-party text. A dropped socket prints one
line and exits 1; the terminal mode is restored in a `finally` in every path.

Not a widget toolkit: no history, no cursor movement, no completion, no colours beyond one
dim prefix for notices. When that hurts in the two weeks of use, `readline` is the next rung.

## pathfinder/llm/anthropic.py — the second adapter, experimental (~150 lines, D2/D7)

`stream(endpoint, messages, tools=None, cancel=None, timeout=120)` yielding the same
`(kind, payload)` tuples `openai_compat.stream` yields, so the loop and the slot never know:
`POST <base_url>/v1/messages` with `anthropic-version: 2023-06-01`, `x-api-key` from the
endpoint, `stream: true`; the OpenAI-shaped history the loop keeps is converted at the edge
(`system` messages -> the top-level `system`, `tool` results -> `tool_result` blocks inside a
user turn, assistant tool calls -> `tool_use` blocks); tools -> `input_schema`; SSE events
(`content_block_delta` text and `input_json_delta`, `content_block_start` `tool_use`,
`message_delta` usage/stop) assembled into the same tool-call dicts; HTTP errors -> `LLMError`
(429/402 -> `blocked`). No embeddings (Anthropic has none; `embed` stays with the compatible
adapter). Dispatch: `providers[name].api = "openai" | "anthropic"` (default `openai`, so every
existing config is unchanged), carried on `Endpoint.api`, and one function `llm.stream()`
picking the module - the two call sites (`slot.stream_step`, `loop`) call that. Tests are
recorded SSE fixtures only, synthetic ids, scanned; `README`, `ARCHITECTURE` and the config
comment say *experimental - not run live*. D7's reversal (a key appears -> m1 on that provider
-> first-class) is what this adapter exists for (D37).

## README.md / README.ko.md (D6)

`README.md` is rewritten from the pre-alpha stub: one paragraph of what it is; **the ten-minute
path** (macOS 14+, Homebrew: `python@3.x` (the researcher's floor), `ollama`, `node`; the Ollama
account and cloud-model step; `git clone`, `pip install .` (or `python3 -m pathfinder` from the
clone), `pathfinder init`, `pathfinder tui`, "say hello"); what runs where (the `<home>` table's
short form, the launchd agent, the log); turning the other senses on (the mail Keychain line, the
calendar permission); installing a tool (`search`, `install`, `approve`); measurements; hygiene;
the experimental adapter's status; license line unchanged. `README.ko.md` mirrors it, linked
from the top of each, updated in the same commit - the `ARCHITECTURE` rule extended (the
invariant test checks both exist and have the same heading count).

## ARCHITECTURE.md / ARCHITECTURE.ko.md

New §15 "Installing and the screen" (init's order, the two foreign files it edits and how, the
launchd agent, `service`, the TUI's protocol use and the presence it produces, the anthropic
adapter's status); §1 table rows for `~/Library/LaunchAgents/pathfinder.daemon.plist`,
`<home>/daemon.log`, `<home>/hooks/pathfinder.zsh`, `~/.zshrc` (one line), `~/.claude/settings.json`;
§7 gains the SIGTERM path and the origin-aware steer; §12 the new invariants; §13 the counts;
§14 the new commands. Both languages, one commit.

## Tests that must exist

- `test_init.*` (temp `HOME` + `PATHFINDER_HOME`, answers piped, a fake OpenAI-compatible
  `/models` server, `launchctl` injected): `test_checks_run_in_order_and_stop_at_the_first_fix`
  (no Ollama -> the fix line, nothing written), `test_model_pick_goes_through_config_set` (the
  ledger has the config event; the value is the fake server's id), `test_senses_merge_not_replace`,
  `test_shell_hook_is_appended_once` (twice -> one line; the backup exists), `test_claude_hooks_
  merge_idempotently` (two unrelated keys survive deep-equal; a second run adds nothing),
  `test_plist_content` (interpreter path, `PATH` contains the interpreter's dir and Homebrew,
  log path, KeepAlive), `test_service_status_without_launchd` (the injected `launchctl` fails
  -> a plain line, exit 1), `test_no_model_name_in_init_or_readme` (the scanner's model pattern
  over `init.py`, both READMEs).
- `test_tui.*` (a fake daemon on a temp socket, a pty or a pipe for stdin): `test_deltas_and_
  notice_redraw_the_input_line`, `test_typing_sends_presence_zero_and_silence_sends_idle`,
  `test_enter_sends_say_and_slash_approve_sends_approve`, `test_plus_minus_send_react`,
  `test_ctrl_c_interrupts_a_running_stream_then_quits`, `test_terminal_mode_restored_on_error`,
  `test_render_is_shared_with_listen`, `test_notice_text_is_sanitized`, `test_tui_under_400_lines`
  (in `test_invariants`).
- `test_daemon.*`: `test_sigterm_closes_mcp_servers` (the fake stdio server's process is gone
  after the handler runs), `test_say_during_signal_turn_starts_its_own_turn` (a signal turn is
  running; `say` arrives; the reply is a `turn.start` of its own after the signal turn ends, not a
  steer), `test_say_during_user_turn_still_steers` (D17 unchanged).
- `test_llm_anthropic.*`: `test_text_stream_from_recorded_sse`, `test_tool_use_assembled_from_
  input_json_deltas`, `test_history_conversion_round_trip` (system, tool result, assistant tool
  call), `test_429_is_blocked`, `test_dispatch_defaults_to_openai` (an existing config resolves
  exactly as before), `test_no_live_call_in_the_suite` (no test opens a socket to the internet -
  the fixture is the only source).
- `test_invariants`: `init.py` <= 300, `tui.py` <= 400, `daemon.py` <= 780 (unchanged), the
  README pair exists with equal heading counts, `presence` has a producer in `tui.py` (a grep - the
  branch stays live), `config.set` is still the only config writer (init goes through it), no
  new ledger type (this partition adds none; `presence`, `config` and `reaction` already exist).

## Build order

0. **Researcher** (first, parallel with nothing): (a) what a fresh macOS 26 account has - the
   CLT `python3` version, whether Homebrew's `python3` is 3.13 or 3.14 today, and whether this
   code runs under `python3.11` (it is on this machine: run the suite with it); (b) the Ollama
   cloud flow on a clean account in 2026 - install, `ollama signin`, whether `/v1/models` lists
   cloud models before a first run, what the free plan needs; (c) `launchctl bootstrap gui/<uid>`
   syntax and `KeepAlive` behaviour on macOS 26, and whether a LaunchAgent inherits Homebrew's
   `PATH`; (d) Claude Code's 2026 `settings.json` hook shape (is `docs/senses.md`'s block still
   right?); (e) the Anthropic Messages streaming event list as of 2026 for the fixture. One facts
   file, sources named, no product code.
1. **Implementer A**: `init.py` (+ the shared foreign-file helper, D36) + `daemon.py`'s two
   changes + `__main__` wiring for `init`/`service` -> tests -> live (2), (3), (5).
2. **Implementer B** (parallel with A; owns `tui.py` and `tests/test_tui.py` only; exposes
   `tui.main(home)` and `tui.render`; the lead wires `__main__` and moves `listen` onto the
   renderer): the screen -> tests -> live (4).
3. **Implementer C** (parallel; owns `llm/anthropic.py`, `llm/__init__.py`'s dispatch, the
   fixture and `tests/test_llm_anthropic.py`; touches `slot.py`/`loop.py` only at the one call
   each): the adapter -> tests (6). Dropped first if the partition runs long (D37).
4. **Lead**: README both languages, ARCHITECTURE both languages, HISTORY ("Before partition 7"
   with the owner's answers; "Partition 7" with the live checks, defects by whom, what changed
   against the plan); reviewer pass (opus, read-only, reproduce, the partition-6 defect classes:
   foreign-file writes, idempotency, terminal state, orphaned processes); scan; commit; push;
   draft PR #8 on `partition-6`.
5. **Owner or teammate**: the ten-minute check on a fresh account -> HISTORY `m8` -> the
   partition-8 go/no-go.

Briefs as files (`brief-{r,a,b,c}.md` in the session scratchpad), each unit one to three files,
turn budgets in the brief, the partition-5/6 lesson.
