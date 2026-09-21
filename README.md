# PATHFINDER

A resident, proactive AI assistant for people who build with AI but are tired of chasing new tools.
It lives on your computer, speaks first when something worth saying happens, and proposes
pre-verified tools (MCP servers) that fit what you are doing — one approval installs them.

**Status: pre-alpha, under construction.** Nothing here is usable yet. The structure was agreed
before any code was written; see below.

## Where to read

| What | Where |
|---|---|
| Why the structure is what it is (agreed before implementation, Korean) | `docs/blueprint/pathfinder-blueprint.html` |
| Decisions D1–D15, each with the condition that would reverse it | `docs/decisions/DECISIONS.md` |
| What changed once reality was measured | `HISTORY.md` |
| Research digest behind the decisions (Korean) | `docs/research/` |
| What the code does (added with the first working partition) | `ARCHITECTURE.md`, `ARCHITECTURE.ko.md` |

## Measurements

```bash
python3 bench/m1_toolcall.py --selfcheck   # scorer self-test, no network
python3 bench/m6_daycost.py --selfcheck    # parser/extrapolation self-test, no network
```

## Contributing hygiene

`git config core.hooksPath .githooks` enables the pre-commit personal-data scan
(`scripts/scan_personal.py`). Placeholders only: `admin@example.com`, `sk-...`.

License: to be decided before the repository becomes public.
