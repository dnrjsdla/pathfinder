# PATHFINDER — project instructions

The owner directs; agents are the dev team. The owner's global CLAUDE.md applies in full
(design decisions belong to the owner; whole structures, not increments; scan before commit).

## Read first
- `docs/decisions/DECISIONS.md` — D1–D15. Do not re-litigate; if reality contradicts one, report it and write it to `HISTORY.md`.
- `docs/blueprint/pathfinder-blueprint.html` — invariants (§11), module budgets (§6), partitions (§14), completion criteria (§15).
- `HISTORY.md` — what measurement changed.

## Guiding principle (D8)
PATHFINDER is a real assistant, not a problem-solving tool: look one step ahead. Do not cut a
capability that assistant-ness depends on just because it is unused today. Do cut settings slots,
taxonomies, extra ledgers and modules whose absence does not reduce the assistant experience.

## Hard rules
- One ledger: the only writer of `events/` is `ledger.append()`; the only place that maps a time to a partition is `ledger.partition()`.
- One config writer: `config.set()`.
- No model IDs, secrets, emails, home paths or account names in tracked files. Model names live in config; secrets in the Keychain.
- Code, comments, commits and docs in English. `ARCHITECTURE.ko.md` mirrors `ARCHITECTURE.md` in Korean and is updated in the same commit.
- No paid API keys are available (D7). Live models are Ollama cloud models on the free plan; credits are small — never run a full benchmark without the lead's go-ahead.
- Work on branch `build`. `main` takes finished structures only.

## Partition workflow
plan → owner approval → implement (implementer, worktree) → "it runs" check → review (reviewer) → personal-data scan → commit.
A structural change needs the owner's decision BEFORE code.

## Commands
```bash
python3 scripts/scan_personal.py --selfcheck   # scanner self-test
python3 scripts/scan_personal.py --all         # scan every tracked file
```
