#!/usr/bin/env python3
"""bench/proactive.py -- labeled-set measurement for partition 3's `rules.decide`.

Calls the PRODUCTION `pathfinder.rules.decide` (never a copy) against `bench/proactive/labels.jsonl`,
a synthetic set of signals labeled with what a considerate resident assistant should do - not with
what the shipped `RULES.md` currently does. Where they disagree, the label wins and shows up as a
miss (see the report for the proposed fix).

No model call: `ask_model` is scored as a decision, not resolved - D13's judge() is measured
elsewhere, on the model's own turn.

Usage: python3 bench/proactive.py [--set PATH] [--rules PATH] [--verbose]
       python3 bench/proactive.py --selfcheck
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pathfinder import rules as rules_mod  # noqa: E402
from pathfinder import signals as signals_mod  # noqa: E402
from pathfinder.signals import Signal  # noqa: E402
from pathfinder.state import Today  # noqa: E402

HERE = Path(__file__).parent
DEFAULT_SET = HERE / "proactive" / "labels.jsonl"
DEFAULT_USER_LISTS = HERE / "proactive" / "user_lists.json"

# Every sense this partition ships, loaded so `rules.load` sees the full closed condition
# vocabulary and every kind's default-silence row - a sense's import has no side effect
# (EventKit/IMAP are only touched by poll(), never at import time).
ALL_SENSES = {"schedule": {}, "shell": {}, "mail": {}, "calendar": {}, "reminders": {},
              "attach": {}, "claude_code": {}}


def load_rules(rules_path: Path):
    """Parse `rules_path` with the real senses' combined CONDITIONS/KINDS, the way
    `rules.load` does - reusing `rules.load` itself on a scratch home pre-seeded with the
    file, so it just reads it instead of writing the shipped default."""
    senses = signals_mod.load({"senses": ALL_SENSES})
    with tempfile.TemporaryDirectory() as home:
        (Path(home) / "RULES.md").write_text(rules_path.read_text(encoding="utf-8"), encoding="utf-8")
        return rules_mod.load(home, senses)


def _today(rules, item: dict) -> Today:
    ctx = item["context"]
    idle_s = ctx.get("idle_s")
    away = idle_s is None or idle_s >= rules.idle_threshold_s
    recent_counts = {item["kind"]: ctx["recent_same_kind"]} if ctx.get("recent_same_kind") else {}
    return Today(spoken=ctx["spoken_today"], judgments=0, last_signal_ts=None, burst=0,
                 recent_counts=recent_counts, away=away)


def run(items: list, rules, user_lists: dict) -> list:
    from pathfinder import clock
    rows = []
    for item in items:
        signal = Signal(sense=item["sense"], kind=item["kind"], key=item["id"],
                        summary=item.get("note", ""), fields=item["fields"], trust=item["trust"])
        now, _ = clock.parse_local(item["at"])
        decision, why = rules_mod.decide(signal, rules, user_lists, _today(rules, item), now)
        rows.append({**item, "decision": decision, "why": why})
    return rows


def summarize(rows: list, verbose: bool) -> bool:
    speak_decided = [r for r in rows if r["decision"] == "speak"]
    speak_wanted = [r for r in rows if r["label"] == "speak"]
    speak_hits = [r for r in speak_decided if r["label"] == "speak"]
    decided = [r for r in rows if r["decision"] != "ask_model"]
    exact = [r for r in rows if r["decision"] == r["label"]]
    misses = [r for r in rows if r["decision"] != r["label"]]

    precision = len(speak_hits) / len(speak_decided) if speak_decided else 0.0
    recall = len(speak_hits) / len(speak_wanted) if speak_wanted else 0.0
    exact_acc = len(exact) / len(rows) if rows else 0.0
    decision_rate = len(decided) / len(rows) if rows else 0.0

    if verbose:
        for r in rows:
            mark = "o" if r["decision"] == r["label"] else "X"
            print(f"[{mark}] {r['id']:8} {r['kind']:18} decided {r['decision']:9} label {r['label']:9} {r['why']}")

    print("\n=== per kind (decision rate = fraction not ask_model) ===")
    for kind in sorted({r["kind"] for r in rows}):
        group = [r for r in rows if r["kind"] == kind]
        n = sum(1 for r in group if r["decision"] != "ask_model")
        print(f"  {kind:18} {len(group)} items, decided {n}/{len(group)}")

    print("\n=== metrics ===")
    print(f"  precision (speak)   {precision:.2f}  ({len(speak_hits)}/{len(speak_decided)})")
    print(f"  recall (speak)      {recall:.2f}  ({len(speak_hits)}/{len(speak_wanted)})  (recorded, not gated)")
    print(f"  exact-action acc.   {exact_acc:.2f}  ({len(exact)}/{len(rows)})")
    print(f"  decision rate       {decision_rate:.2f}  ({len(decided)}/{len(rows)})")

    if misses:
        print(f"\n=== misses ({len(misses)}) ===")
        for r in misses:
            print(f"  {r['id']:8} label={r['label']:9} decided={r['decision']:9} why={r['why']}  -- {r['note']}")

    ok = precision >= 0.95
    print(f"\n{'PASS' if ok else 'FAIL'} -- pass line: precision(speak) >= 0.95")
    return ok


_SELFCHECK_ITEMS = [
    {"id": "sc-1", "sense": "shell", "kind": "command_failed", "trust": "self", "at": "2026-09-23 10:00",
     "fields": {"cmd": "x", "exit": 1, "cwd": "p", "seconds": 1},
     "context": {"quiet": False, "spoken_today": 0, "idle_s": None, "recent_same_kind": 3},
     "label": "speak", "note": "third failure"},
    {"id": "sc-2", "sense": "schedule", "kind": "schedule_due", "trust": "user", "at": "2026-09-23 02:00",
     "fields": {"id": "s", "text": "t", "at": "2026-09-23 02:00"},
     "context": {"quiet": True, "spoken_today": 0, "idle_s": None, "recent_same_kind": 0},
     "label": "speak", "note": "user's own alarm at night"},
    {"id": "sc-3", "sense": "attach", "kind": "context", "trust": "self", "at": "2026-09-23 12:00",
     "fields": {"cwd": "p", "project": "p"},
     "context": {"quiet": False, "spoken_today": 0, "idle_s": None, "recent_same_kind": 0},
     "label": "silence", "note": "context never speaks"},
]


def selfcheck() -> int:
    rules = load_rules(rules_mod.DEFAULT_RULES_PATH)
    user_lists = {}
    rows = run(_SELFCHECK_ITEMS, rules, user_lists)
    ok = all(r["decision"] == r["label"] for r in rows)
    print("selfcheck", "OK" if ok else "FAILED")
    if not ok:
        for r in rows:
            print(" ", r["id"], "decided", r["decision"], "label", r["label"])
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="proactive rules bench (no model call)")
    ap.add_argument("--set", default=str(DEFAULT_SET))
    ap.add_argument("--rules", default=str(rules_mod.DEFAULT_RULES_PATH))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()

    items = [json.loads(line) for line in Path(args.set).read_text(encoding="utf-8").splitlines() if line.strip()]
    user_lists = json.loads(DEFAULT_USER_LISTS.read_text(encoding="utf-8"))
    rules = load_rules(Path(args.rules))
    print(f"[rules] {args.rules}  kinds {sorted(rules.by_kind)}")
    print(f"[set]   {args.set}  {len(items)} items\n")

    rows = run(items, rules, user_lists)
    ok = summarize(rows, args.verbose)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
