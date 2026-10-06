# Partition 6 — the tool path: spec v0, catalog, proposal, installer, hash acceptance

**"It runs" =** (1) m5 is in HISTORY: in a temp home (a fresh `$HOME` and `$PATHFINDER_HOME`,
standing in for a new macOS account), three npm servers from the official registry each go
check -> proposal -> `approve` -> installed into PATHFINDER's `config.json` and into the temp
`~/.claude.json`, 3/3, each under five minutes, and `pathfinder uninstall` rolls each back 3/3
with every other key of both files deep-equal to before; (2) live: `pathfinder daemon` with the
`catalog` sense on and an `attach {cwd}` from a Node project - a registry entry updated since the
last check whose name or description carries one of the project's tags becomes a `tool_candidate`
notice ("New tool for <project>: ... - say 'install it' or `pathfinder install <name>`"); (3)
live: "install it" (or the CLI) runs the checks - runtime, pinned version and integrity, license,
secrets in the Keychain, a sandboxed trial launch that lists the tools and hashes them - and the
proposal in `pathfinder approvals` shows every check's result and which tools would be granted;
`approve` installs and a `tool_installed` notice speaks; the next `say` can call the new server's
tools (`mcp__<server>__<tool>`), a read-only-hinted tool runs by itself, another gates; (4) live:
the server's package is replaced by a version whose tool description changed - on the next daemon
start the tool is **held** (not registered), a `tool_held` notice speaks, and `approve` of the
re-acceptance row registers it (`test_rug_pull_holds`); (5) live: an install whose trial fails
(a package that does not start) records the failed step, rolls back what it did, and speaks
`install_failed`; (6) `state.db` deleted and `reindex` restores accepted hashes, installs and the
pending proposals; (7) a stdio server of each protocol era (legacy `initialize`, 2026-07-28
stateless) is driven by the same client against the fake servers (tests); (8) the D21 review is in
HISTORY: what the registry holds for mail and whether anything meets D21's reversal.

Decisions this partition rests on: D1 (transplant `brain/mcp/`), D3/D4 (proposal -> one
approval -> install into PATHFINDER and Claude Code), D8, D14 (no reference server; the
registry directly, a spec document and fixtures), D15 (work context from `attach`/hooks), D16/D28
(one approval box, `approve`/`deny` only), D21 (mail review at this entrance), and the three this
partition adds (D31–D33, below).

NOT in this partition: sending outcomes to the teammate's server (D14: the spec names the field,
v1 does not send); Cursor and Claude Desktop targets (D4); skills and plugins; OAuth flows for
remote servers (a remote entry whose `remotes[].url` needs a bearer token installs with the token
from the Keychain, nothing more); automatic updates (a newer version is a new candidate, proposed
like any other); the mcpb/dxt bundle type (npm and pypi only - registry counts show they cover
the vast majority); scanning tool descriptions for injection (the m3 envelope rule already treats
a tool's text as data, and the description carries the Mark.5 "written by that server" prefix);
the TUI (7).

Line budgets (blueprint §6): `mcp/` 1,858 transplanted (adapter budget, outside the core);
`catalog.py` 300 and `installer.py` 700 (new, core); `senses/catalog.py` <= 150; `daemon.py`
701/600 today (+~15: the proposal-handler table replaces partition 5's `edit_memory` branch, the
MCP load/close lines; hard line 780); `config.py` +~30 (`mcp_servers`); `state.py` 552 (+~60:
pins, installs); `ledger.py` +5 types; `__main__.py` +~60. Expected: about 1,150 new core lines,
about 1,500 transplanted, one bench, one spec document with fixtures, tests.

## Entrance measurement

### m5 — one approval installs in five minutes (`bench/m5_install.py`, zero model credits)

Three npm servers from the official registry (chosen at run time from `GET /v0.1/servers?search=`
among the `io.github.modelcontextprotocol/*` reference servers: filesystem, memory, and one more
that needs no secret), in a temp `$HOME` holding a hand-written `~/.claude.json` with two unrelated
keys and one unrelated `mcpServers` entry, and a temp `$PATHFINDER_HOME`. For each: the CLI path
`pathfinder install <name>` -> the proposal row -> `approve` (the bench answers the approval
itself; no model runs) -> timing of every step (runtime check, version + integrity, license, trial
launch, config merge x2, pin accept) -> `pathfinder uninstall <id>` -> both files compared
deep-equal to their pre-install content, plus `HOME=<tmp> claude mcp list` when the `claude`
binary is present (reported, not gated). Pass (blueprint §13): 3/3 installed, each <= 5 min, 3/3
rolled back, every pre-existing key preserved. Byte-level preservation is not claimed: a JSON
round trip re-serializes; **deep-equal of every key we did not add** is what is measured, and
the backup file the installer writes is what a byte-level restore uses. Fail: D4's reversal -
install into PATHFINDER only.

### D21 review (with the live check, no build)

`GET /v0.1/servers?search=mail` and `search=gmail`/`imap`: for each hit, does it need an app
password or an OAuth client secret, what does it fetch (headers only? bodies?), and does it come
from a verified namespace. Written into HISTORY with a recommendation; D21 flips only if one
needs no app password AND its tool descriptions would pass m3's rule (data, not instruction).

## docs/spec/connection-v0.md and docs/spec/fixtures/

The connection spec v0 (D14): one JSON object per candidate, the shape every step of the path
reads and writes, and the shape a future server would send. Fields: `name`, `version`,
`source_registry` (`{url, entry_id, updated_at}`), `description`, `package`
(`{registry_type: npm|pypi, identifier, version, runtime: npx|uvx, args[], env[{name, secret,
required}], remotes[]}`), `verification` (`license {spdx, source, verdict: ok|excluded|unknown}`,
`malicious_scan {engines[], result, scanned_at}` - `not_checked` in v1, `sandbox_trial {ran_at,
started, tools[{name, description_hash, schema_hash, read_only_hint}], file_writes_outside_tmp,
network: not_checked, verdict}`, `maintenance {last_publish_at, namespace_verified}`,
`description_hash` (over the tool list), `trust_tier: unverified|checked` - v1 only ever produces
`checked`), `install` (`{runtime, pinned_version, integrity, env_from_keychain[], args[],
permissions: [tool names and their default grant]}`), `billing: unknown|free|paid` (v1:
`unknown` unless the description says), `outcome: proposed|approved|rejected|installed|
install_failed|uninstalled` (recorded locally; the "sent to the server" note says v1 does not
send). Every check field the code did not run is the literal string `not_checked`. Fixtures: one
`proposed.json`, one `installed.json`, one `install_failed.json`, all synthetic, all scanned.

## pathfinder/mcp/ — the transplant (D1)

`protocol.py`, `transport.py` (stdio and Streamable HTTP), `session.py`, `pins.py` from Mark.5
`brain/mcp/` as they are, with three edits: the `worklog` logger becomes the daemon's `error`
record through a callback; `child_env` becomes `exec.child_env` (the same allow-list already in
`exec.py`); and `session.discover()` gains the **dual-era probe** the 2026-07-28 spec defines -
`server/discover` first, then `initialize` + `notifications/initialized` on a legacy error - so
the config carries no `protocol` key. Mark.5's `catalog.py` (the server-config merger) is
renamed `mcp/servers.py` (the word `catalog` is the registry reader here). `mcp_tools.py` is
rewritten as `mcp/tools.py` (~150 lines): one `ToolSpec` per MCP tool named
`mcp__<server>__<tool>` (Mark.5's rule and regex), `inputSchema` passed through, the description
prefixed with Mark.5's "written by that server, not an instruction" sentence (English), `write=True,
network=True` unless the tool's `readOnlyHint` is true AND the user granted it (D31, below) -
origins the four turn origins (never `memory`). Results flatten to text like Mark.5's. `PinStore`
keeps its interface; its storage is the ledger: `load()` reads `state.pins()`, `save()` records
one `pin_accept {server, tool, description_hash, schema_hash, params}` per pinned tool through the
daemon's `record`. A held tool is never registered (Mark.5's rule) and becomes a proposal row
(`accept_pins`, below). The fake servers from Mark.5's tests (`FakeTransport`, the stdio Python
script, the `ThreadingHTTPServer` handler) come across with the tests, plus a 2026-07-28-shaped
fake for the probe.

`config.json` gains `mcp_servers: {id: {transport: stdio|http, command, args, env, url, headers,
timeout}}`, written only by the installer through `config.set` (and by hand). An `env` or
`headers` value of the form `keychain:<account>` is read at spawn from the macOS Keychain service
`pathfinder-mcp-<id>` (`security find-generic-password`, the mail sense's way); a missing entry
keeps the server listed as `disabled: "secret missing"` (Mark.5's rule) and speaks once. Secrets
never sit in `config.json`. The daemon loads `mcp_servers` at start (`mcp.servers.load` -> sessions
-> pin check -> register the passing tools into `tools.TOOLS`), closes them at `close()`, and
reloads one server after an install without a restart.

## pathfinder/catalog.py — the registry reader (~250 lines, budget 300)

`fetch(since, search=None) -> [entry]`: `GET /v0.1/servers` with `updated_since`, `version=latest`,
cursor pagination, `urllib`, 20 s timeout, at most 5 pages per call. `candidate(entry) ->
candidate` (spec v0 shape): picks the first npm or pypi package (else `remotes[0]`), fills
`package`, `source_registry`, `maintenance.namespace_verified` (the registry's own claim - true
for every active entry, recorded as such), `description`, everything else `not_checked`.
`tags(state, home) -> set[str]`: the coarse work-context tags - the project names of the last
seven days' `context` signals (`state`'s `signals` + their fields), plus marker words from
listing each such cwd once (`package.json` -> node, javascript, npm; `pyproject.toml`/
`requirements.txt` -> python; `Cargo.toml` -> rust; `go.mod` -> go; `.git` -> git, github;
`docker-compose*` -> docker; `*.ipynb` -> jupyter; `Gemfile` -> ruby) - a listing, never file
contents. `matches(candidate, tags) -> [tag]`: tags that appear as whole words in the lowercased
name or description. `license(candidate) -> verdict`: `npm view <pkg>@<ver> license` /
PyPI JSON `info.license` + classifiers -> `spdx`; `excluded` for GPL-*, AGPL-*, LGPL-*, CC-BY-NC-*,
"UNLICENSED"/none (the application's rule), `ok` for the rest, `unknown` when the field is missing.
`declined(state, name)` is true when a proposal for that name was denied within 30 days.

## pathfinder/senses/catalog.py (poll, ~80 lines)

`OPTIONS = {"registry": "https://registry.modelcontextprotocol.io", "cadence_h": 24}`,
`TRUST = "third_party"` (the registry's text is someone else's), `CADENCE_S` from the option. `poll`:
`catalog.fetch(since=last check)`, keep entries whose candidate has a `license` other than
`excluded` (the license lookup runs only for entries that match a tag - one npm/PyPI request each,
at most 10 per poll), that are not installed and not declined -> `Signal(kind="tool_candidate",
key=f"catalog:{name}@{version}", summary=f"New tool for {project}: {name} — {description[:80]}",
fields={candidate, matched_tags}, trust=third_party)`. The last check time is
`state`'s latest `signal` of sense `catalog` (or the newest `tool_candidate` key) - no second
cursor file. Shipped RULES.md: `## tool_candidate`: `count_over 3 -> silence` (a flood of matches
is a registry dump, not news), `always -> speak` (the daily budget and quiet hours still apply).
A tool the user asks about in conversation is fetched on the spot by `search_tools(query)`
(origins user; `catalog.fetch(search=)`, network - policy `network` is `allow` for the user).
Second kind `tool_held` (pushed by the daemon's pin check, `trust self`, `always -> speak`) and
third `install_done`/`install_failed` (pushed by the installer, `trust self`, `exempt` like a job's
outcome: the user asked for it; `always -> speak`). `senses/catalog.py` documents all four kinds;
only `tool_candidate` is polled.

## pathfinder/installer.py — check, propose, install, roll back (~600 lines, budget 700)

**`check(candidate) -> candidate`** fills the verification block, in order, stopping at the first
hard failure: runtime (`shutil.which("npx")`/`uvx`; missing -> `install_failed` with the fix
line); pinned version + integrity (`npm view <pkg>@<ver> dist.integrity dist.tarball --json` /
PyPI JSON `digests.sha256`; the version is the candidate's, never `latest`); license (as above;
`excluded` is a hard failure for a catalog proposal, a warning shown on the row for an explicit
request - D32); secrets: every `env` marked secret must already be in the Keychain service
`pathfinder-mcp-<id>` (missing -> the proposal still lands, marked `needs: <NAME>` with the
`security add-generic-password` line; the trial runs without it); **trial launch**: the stdio
command run through `exec.py`'s Seatbelt profile with writes confined to a fresh temp dir and the
allow-listed env, `session.discover()` + `tools/list` with a 60 s timeout, the tool list hashed
(pins), `read_only_hint` per tool from annotations, the process killed, the temp dir diffed for
writes (`file_writes_outside_tmp` is always false under the profile; recorded anyway), network
`not_checked` (the profile allows it; observing it is later work). A remote (http) entry skips
the trial's process and does `discover` + `tools/list` over HTTP with the same timeout. `verdict:
ok` when it started and listed at least one tool.

**`propose(candidate, origin)`**: records `approval_pending {tool: "install_server", args:
candidate, messages: []}` - the one approval (D3) - and returns the id; `pathfinder approvals`
prints the checks as lines (`license MIT ok · integrity sha512 ok · trial ok, 6 tools (3
read-only) · needs: GITHUB_TOKEN`). Callers: the `propose_install(name)` tool (origins user - the
model calls it when the user says "install it"; the check's network and process run under the
user's own policy) and the CLI `pathfinder install <name>`. `check` runs on a thread; the
proposal lands when it ends (the CLI waits and prints the id).

**`install(approval_id)`** (the proposal handler for `install_server`, on a thread): step events
`install_step {id, step, ok, detail}` for: (1) `config.set("mcp_servers", ...)` with the server
id = the registry name's last path segment, sanitized to Mark.5's regex; (2) the Claude Code
merge: read `~/.claude.json` (or create `{}`), back it up to `~/.claude.json.pathfinder-<ts>`,
set `mcpServers[<id>] = {command, args, env}` (user scope, D33; secrets as `${VAR}` references
never values - Claude Code reads its own env, so the row's `env` names the variable and the
proposal says so), write atomically, verify by reading back and deep-comparing every other key
to the backup; (3) pin accept: one `pin_accept` per tool from the trial's hashes; (4) load into
the running daemon (`mcp.servers.load_one` -> register; the tools appear on the next turn); (5)
`install_done {id, name, version, tools}` and the `install_done` signal. Any step failing runs
`rollback(id)` over the steps already done, in reverse (restore the config value, restore the
Claude Code file from the backup byte-for-byte, record `pin_revoke`, unload) and records
`install_failed {id, step, reason}` + its signal. `uninstall(id)` (CLI) is the same reverse path
from a completed install, recorded as `uninstalled`. Grants (D31): at approval the installer
writes `grants[mcp__<id>__<tool>] = "allow"` for every tool whose `readOnlyHint` was true in the
trial, through `config.set`; the proposal row listed them.

## The proposal box (daemon.py, -10 +15 lines)

Partition 5's `edit_memory` branch in `approve`/`deny` becomes a table: a row with empty
`messages` is a proposal, and `PROPOSALS = {"edit_memory": memory_mgr.decide, "install_server":
installer.install_or_drop, "accept_pins": mcp.accept_or_drop}` names its handler by tool. Lane
rows and job rows are unchanged. `pathfinder approvals` prints each kind its own way.

## ledger.py / state.py

`TYPES` gains `pin_accept`, `pin_revoke`, `install_step`, `install_done`, `install_failed`,
`uninstalled`. `state`: table `pins(server, tool, description_hash, schema_hash, params_json,
ts)` (`pin_accept` upserts, `pin_revoke` deletes; `pins(server)` reads), table `installs(id, name,
version, state, ts, detail)` (from the three install events + `uninstalled`), `installed_names()`,
`declined_since(name, ts)`; all rebuilt by `reindex`.

## __main__.py

`pathfinder install NAME` (check -> proposal id printed), `pathfinder uninstall ID`, `pathfinder
tools` (installed servers, their tools and grants, held tools), `pathfinder search TEXT` (the
registry on the spot, candidates printed with license and package), `pathfinder mcp accept ID`
is NOT added - a held tool is an `accept_pins` proposal, `approve` takes it.

## Tests that must exist

- `test_mcp_*`: the transplanted 76 (adapted names), plus `test_dual_era_probe` (a fake that
  answers `server/discover` and one that rejects it and wants `initialize`), `test_pins_live_in_
  the_ledger` (a pin survives `state.db` deletion), `test_secret_from_keychain_never_in_config`
  (a `keychain:` reference in config resolves through an injected lookup; `config.json` never
  holds the value; a missing entry disables the server and records one error).
- `test_catalog.*`: `test_candidate_from_registry_entry` (the fixture entry -> spec shape, every
  unchecked field `not_checked`), `test_tags_from_context_and_markers`, `test_matches_whole_words`,
  `test_license_verdicts` (GPL excluded, MIT ok, missing unknown), `test_poll_skips_installed_
  declined_and_excluded`, `test_fetch_paginates_and_stops_at_five_pages` (fake HTTP server).
- `test_installer.*`: `test_check_fills_verification_in_order_and_stops` (missing runtime ->
  failed with the fix line), `test_trial_runs_in_the_sandbox_and_hashes_tools` (the fake stdio
  server; `file_writes_outside_tmp` false; a server that never answers -> timeout -> verdict
  failed), `test_proposal_row_lists_checks_and_grants`, `test_install_merges_claude_code_non_
  destructively` (two unrelated keys and one unrelated server survive deep-equal; a backup
  exists), `test_install_rollback` (a failing step 3 restores config and the Claude Code file
  byte-for-byte and records `install_failed`), `test_uninstall_is_the_reverse`, `test_rug_pull_
  holds` (a changed description on restart -> held, not registered, an `accept_pins` proposal;
  approve -> registered), `test_read_only_hint_grants_only_at_approval`, `test_secret_missing_
  proposal_says_what_to_add`.
- `test_daemon.*`: `test_proposal_table_routes_by_tool` (edit_memory, install_server, accept_pins
  each reach their handler; a lane row still resumes), `test_mcp_tools_registered_at_start_and_
  after_install`, `test_signal_turn_cannot_propose_install` (the tool is user-only).
- `test_spec_fixtures_validate` (every fixture has every field of v0; the scanner passes).
- `test_invariants`: `catalog.py` <= 300, `installer.py` <= 700, `senses/catalog.py` <= 150,
  `daemon.py` <= 780; no registry URL other than the option's default in source; only
  `ledger.append` writes events; `config.set` is the only config writer (the installer goes
  through it).

## Build order

1. `mcp/` transplant + `mcp/tools.py` + config `mcp_servers` + Keychain + pins in the ledger +
   daemon load/close + the proposal table -> tests (76 + new) -> live: a hand-configured stdio
   server from `npx` works in a `say`; a changed description holds it.
2. `docs/spec/connection-v0.md` + fixtures; `catalog.py`; `senses/catalog.py`; `search_tools`;
   RULES.md rows -> tests -> live (2).
3. `installer.py` + `propose_install` + CLI -> tests -> `bench/m5_install.py` -> run (temp HOME;
   no model credits) -> HISTORY -> D4 stands or flips -> live (3)–(5), the D21 review.
4. Reviewer pass -> ARCHITECTURE both languages -> scan -> commit -> push -> draft PR #7 on
   `partition-5`.
Three implementer units per the partition-5 lesson (one to three files each, briefs as files).

## The owner's answers (2026-09-24) -> D31–D33

Q1 = c (the install approval grants `allow` to the trial's read-only-hinted tools; the rest ask).
Q2 = b (the catalog never proposes an excluded license; an explicit request shows the verdict and
the owner approves). Q3 = a (user scope, `~/.claude.json` top-level `mcpServers`). This contract
is binding.
