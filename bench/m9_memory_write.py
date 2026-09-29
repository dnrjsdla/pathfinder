"""m9 - does a consolidation pass write anything down?

Replays one slice of a real ledger through the consolidation prompt with the memory tools
faked (nothing is written anywhere), and records which tools the model called. Two variants:
`current` is prompts/consolidate.txt as shipped; `no_summary` drops its closing paragraph (the
session summary the pass is also asked for), to see whether the second job crowds out the first.

The result holds tool names, targets and counts only - never ledger text.

    python3 bench/m9_memory_write.py --home ~/.pathfinder --since 4882 --upto 5087 --variant current
"""
import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pathfinder import clock, config, llm, loop, memory  # noqa: E402
from pathfinder import tools as tools_mod  # noqa: E402

SUMMARY_MARK = "When you are done, answer with a summary"
NO_SUMMARY_TAIL = "When you are done, answer with one line: what you wrote down, or `nothing`.\n"


class _NoState:
    """render_line asks the state only about judgments, reactions and approvals."""
    def signal_kind(self, key): return None
    def reaction_kinds(self, delivery_id): return ""
    def approval(self, approval_id): return None


def prompt(variant: str) -> str:
    text = memory.CONSOLIDATE_PROMPT
    if variant == "current":
        return text
    head, mark, _ = text.partition(SUMMARY_MARK)
    assert mark, "consolidate.txt no longer has the summary paragraph this bench cuts"
    return head + NO_SUMMARY_TAIL


def slice_lines(home: Path, since: int, upto: int) -> list:
    lines = []
    for path in sorted(glob.glob(str(home / "events" / "*.jsonl"))):
        for raw in open(path, encoding="utf-8"):
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if since < event.get("seq", 0) <= upto:
                line = memory.render_line(event, _NoState())
                if line is not None:
                    lines.append((event["seq"], line))
    return lines


def run(home: Path, since: int, upto: int, variant: str) -> dict:
    cfg = config.load(home)
    lines = slice_lines(home, since, upto)
    calls = []

    def call_tool(call_id, name, args):
        calls.append({"name": name, "target": args.get("target"), "op": args.get("op")})
        if name == "read_ledger":
            after = int(args.get("after") or 0)
            page = [text for seq, text in lines if seq > after][:int(args.get("limit") or 80)]
            return "\n".join(page)
        if name == "search_memory":
            return "(no pages match)"
        if name == "read_memory":
            return f"no such page: {args.get('path')}. Pages: (no pages yet)"
        if name == "edit_memory":
            return "queued for the user's approval" if args.get("target") != "" and not str(
                args.get("target", "")).startswith("wiki/") else "applied"
        return f"unknown tool {name}"

    specs = tools_mod.for_origin("memory")
    schemas = [spec.schema() for spec in specs.values()]
    endpoint = llm.resolve(cfg, cfg.get("worker_model") or cfg["model"])
    goal = (f"Consolidate ledger events #{since + 1}..#{upto} ({len(lines)} readable lines; "
            "use read_ledger, paging with `after` until it returns nothing).\nNo current session.\n")
    messages = [{"role": "system", "content": prompt(variant)},
                {"role": "system", "content": clock.now_line()},
                {"role": "user", "content": goal}]
    out = loop.run(lambda m: llm.stream(endpoint, m, tools=schemas), messages,
                   call_tool=call_tool, emit=lambda kind, payload: None, budget=16)
    edits = [c for c in calls if c["name"] == "edit_memory"]
    return {"variant": variant, "since": since, "upto": upto, "readable_lines": len(lines),
            "user_lines": sum(1 for _, text in lines if "[user]" in text),
            "outcome": out.kind, "steps": out.steps, "usage": out.usage,
            "calls": [c["name"] for c in calls],
            "edits": [{"target": e["target"], "op": e["op"]} for e in edits]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--since", type=int, required=True)
    ap.add_argument("--upto", type=int, required=True)
    ap.add_argument("--variant", choices=("current", "no_summary"), default="current")
    a = ap.parse_args()
    print(json.dumps(run(Path(a.home).expanduser(), a.since, a.upto, a.variant), ensure_ascii=False))
