#!/usr/bin/env python3
"""
m5_registry.py -- the M5 re-check (P2 contract, Measurements): M5's 20 pre-registered queries through the product's
`search.find` over the real registry - every native (all senses imported) plus the fixture's Notion tools as
`mcp/tools.py` builds them under server `fake-notion`. v1's expected names are mapped to v2's; a query whose tool
no longer exists is dropped and reported. Plus the truncation probe: the longest `text(spec)` embedded whole and
cut in half; cosine < 0.999 between them -> not truncated.
Pass: top-3 >= 90% of the queries kept (M5: 18/20). Local Ollama only, no model calls, 0 credits.

Usage: python3 bench/v2/m5_registry.py --embed-model ollama:<name> [--out path]
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from m5_tool_search import QUERIES  # noqa: E402 - the pre-registered queries, never copied

from pathfinder import config, memory, search, signals, skills, tools  # noqa: E402,F401 - memory/skills register at import
from pathfinder.llm import openai_compat, resolve  # noqa: E402
from pathfinder.mcp import tools as mcp_tools  # noqa: E402
from pathfinder.mcp.protocol import qualify  # noqa: E402
from pathfinder.mcp.servers import Entry  # noqa: E402

SERVER = "fake-notion"
RENAMED = {"search_tools": "search_registry"}  # v1 -> v2 natives (U0); a Notion name gains its server prefix


def build_registry() -> set:
    """Fills `tools.TOOLS` as a daemon with every sense on and the fixture's server attached would."""
    for _name, mod in signals.sense_modules():
        tools.TOOLS.update(getattr(mod, "TOOLS", {}) or {})
    defs = json.loads((HERE / "fixtures" / "notion_tools.json").read_text(encoding="utf-8"))
    entries = [Entry(SERVER, d["name"], qualify(SERVER, d["name"]), d) for d in defs]
    passed = SimpleNamespace(passed={e.qualified_name for e in entries})  # the pin check's verdict: all pass
    tools.TOOLS.update(mcp_tools.specs(entries, None, passed))
    return {d["name"] for d in defs}


def v2_name(name: str, notion: set) -> str:
    return qualify(SERVER, name) if name in notion else RENAMED.get(name, name)


def top3(out: str) -> list:
    """The first three tool names of a `find` result: full specs are JSON lines, the rest `- name: ...`."""
    names = []
    for line in out.split("\n")[1:]:
        if line.startswith("{"):
            names.append(json.loads(line)["name"])
        elif line.startswith("- "):
            names.append(line[2:].split(":", 1)[0])
    return names[:3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed-model", required=True, help="provider:model, as config's embed_model takes it")
    ap.add_argument("--out", default=str(ROOT / "docs" / "measurements" / f"v2-m5-registry-{date.today()}.json"))
    args = ap.parse_args()

    notion = build_registry()
    names = set(tools.TOOLS) - {search.SEARCH_TOOLS.name}
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        config.set(home, "embed_model", args.embed_model)
        cfg = config.load(home)
        rows, dropped = [], []
        for lang, query, expected in QUERIES:
            want = {v2_name(n, notion) for n in expected} & names
            if not want:
                dropped.append({"query": query, "expected_v1": sorted(expected)})
                continue
            t0 = time.perf_counter()
            out = search.find(query, home)
            ms = round((time.perf_counter() - t0) * 1000, 1)
            if not out.startswith("Tools nearest"):
                sys.exit(f"search fell back, nothing measured: {out.splitlines()[0]}")
            got = top3(out)
            rows.append({"lang": lang, "query": query, "expected": sorted(want), "top3": got,
                         "top1_hit": got[:1] != [] and got[0] in want, "top3_hit": bool(want & set(got)),
                         "chars": len(out), "full_specs": out.count('"parameters"'), "ms": ms})
        longest = max((search.text(tools.TOOLS[n]) for n in names), key=len)
        whole, half = openai_compat.embed(resolve(cfg, args.embed_model), [longest, longest[:len(longest) // 2]])
    cosine = search._cosine(whole, half)
    hits = sum(r["top3_hit"] for r in rows)
    result = {
        "measurement": "M5 re-check (v2 partition 2)", "date": str(date.today()), "embed_model": args.embed_model,
        "tools": len(names), "natives": len(names) - len(notion), "notion": len(notion),
        "summary": {lang: {"n": sum(r["lang"] == lang for r in rows),
                           "top1": sum(r["top1_hit"] for r in rows if r["lang"] == lang),
                           "top3": sum(r["top3_hit"] for r in rows if r["lang"] == lang)} for lang in ("en", "ko")},
        "dropped": dropped,
        "chars": {"max": max(r["chars"] for r in rows), "median": statistics.median(r["chars"] for r in rows),
                  "limit": search.LIMIT},
        "ms": {"first_search_indexes_all": rows[0]["ms"], "median_after": statistics.median(r["ms"] for r in rows[1:])},
        "truncation_probe": {"longest_text_chars": len(longest), "cosine_whole_vs_half": round(cosine, 6),
                             "truncated": cosine >= 0.999},
        "verdict": {"top3": f"{hits}/{len(rows)}", "pass": hits >= 0.9 * len(rows),
                    "rule": "top-3 >= 90% of the queries whose expected tool still exists"},
        "rows": rows,
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("verdict", "summary", "dropped", "chars", "ms", "truncation_probe")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
