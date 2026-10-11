# PATHFINDER

[한국어](README.ko.md)

A resident, proactive AI assistant for people who build with AI. It lives on your Mac as a
background daemon, speaks first when something worth saying happens (a build that failed, a mail
that needs an answer, a tool that fits the project you just opened), remembers what you tell it
in plain Markdown files you can read, and proposes pre-verified tools (MCP servers) that fit what
you are doing - one approval installs them. Everything it does is one line in a ledger you own.

**Status: pre-alpha.** It runs; it is not yet packaged. macOS only for now.

## Ten minutes to the first conversation

Prerequisites (Homebrew). The Python that ships with macOS is too old; PATHFINDER needs 3.11+.

```bash
brew install python@3.14 pipx ollama node   # node only for tool installs (npx); skip it for now if you like
pipx ensurepath                          # once; then open a new terminal
brew services start ollama               # or: ollama serve
ollama signin                            # opens ollama.com in the browser; the free plan is enough
```

Install and set up:

```bash
git clone <this repository> pathfinder && cd pathfinder
pipx install .                           # puts `pathfinder` on PATH; the shell and Claude Code hooks call it by name
pathfinder init
```

(Homebrew's Python refuses a plain `pip install` into itself, PEP 668; `pipx` gives PATHFINDER its
own environment and a command on PATH.)

`init` checks Python and Ollama, lists the models your Ollama can reach and lets you pick one,
asks which senses to turn on, offers to hook your shell and Claude Code, and starts the daemon at
login through launchd. Then:

```bash
pathfinder tui                           # one screen: type, read, /approve, +/- on a notice, /quit
pathfinder say "hello"                   # or one message at a time
```

If a step fails, `init` prints the exact fix line and stops; run it again after the fix - it
changes nothing that is already in place.

## What runs where

| Place | What |
|---|---|
| `~/.pathfinder/` (`$PATHFINDER_HOME`) | everything PATHFINDER owns: the ledger (`events/`), `config.json`, `RULES.md`, `USER.md`, `wiki/`, the socket, `daemon.log` |
| `~/Library/LaunchAgents/pathfinder.daemon.plist` | the launchd agent `init` wrote; `pathfinder service start\|stop\|restart\|status` |
| `~/.zshrc` (one guarded line) and `~/.pathfinder/hooks/pathfinder.zsh` | the shell sense, only if you said yes |
| `~/.claude/settings.json` (two hook entries) | the Claude Code sense, only if you said yes |
| `~/.claude.json` (`mcpServers`) | tools you approved, so Claude Code sees them too |
| macOS Keychain | every secret (an MCP server's token, a mail password), entered with `/secret`; never in a file |

`ARCHITECTURE.md` (and `ARCHITECTURE.ko.md`) describe every module; `docs/decisions/DECISIONS.md`
is the list of decisions with the condition that would reverse each.

## Turning on more senses

`init` turns on the senses that need no secret and prints, for the two that do, the exact line
to paste later, one sense per line - the other senses stay on:

```bash
pathfinder config set senses.calendar '{}'
pathfinder config set senses.mail '{"host": "imap.example.com", "user": "you@example.com"}'
pathfinder secret mail                   # the IMAP password, typed hidden into the Keychain (or /secret mail in the screen)
```

While mail is on and its password is missing, the screen asks for it when it attaches.
macOS asks for Calendar access the first time the calendar sense runs.

## Installing a tool

```bash
pathfinder search "postgres"             # the local copy of the MCP registry, kept by the catalog sense
pathfinder install <registry name>       # proposes; the row shows the checks and the tools it found
pathfinder approve ID                    # installs into config.json and ~/.claude.json; pathfinder uninstall <id> reverses it
```

Or just say "install it" when the daemon proposes one for the project you have open.

A server that needs a token stops before its trial, and the screen asks for the token, masked.
To store one yourself: `/secret <id> NAME` in the screen, or `pathfinder secret <id> NAME` in a
shell, then ask for the install again. The value never goes through the chat. A token pasted
into the chat by mistake is stored in the Keychain as `pasted-<n>`, and the screen says how to
bind it (`/secret <id> NAME --from pasted-<n>`).

## Upgrading

After upgrading to this version, rebuild the derived state once, with the daemon stopped, so
past turns get the record of the tool calls (the `messages.ref` column):

```bash
pathfinder service stop
pathfinder reindex
pathfinder service start
```

`pathfinder service restart` (or a stop and a start) closes the screen's connection: start
`pathfinder tui` again to reattach.

## Model providers

Any OpenAI-compatible endpoint works: `pathfinder config set model provider:model-name`, with the
provider's `base_url` and `api_key_env` in `config.json`. An Anthropic-native adapter exists
(`providers.<name>.api = "anthropic"`) but is **experimental**: it is tested against recorded
responses only and has never been run live.

## Measurements

Every partition entered through a measurement (`HISTORY.md`); the benches under `bench/` re-run
them. `python3 -m unittest discover -s tests` runs the offline suite, no network.

## Contributing hygiene

`git config core.hooksPath .githooks` enables the pre-commit personal-data scan
(`scripts/scan_personal.py`). Placeholders only: `admin@example.com`, `sk-...`. No model names in
tracked files: they live in `config.json`.

License: to be decided before the repository becomes public.
