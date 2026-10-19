#!/usr/bin/env python3
"""m3_gates.py -- v2 M3 (docs/plans/v2-partition-2.md, "Measurements"): the effect map cuts the Notion gates.
Offline, 0 model requests.

Run:       python3 bench/v2/m3_gates.py --ledger <home>/events --session <id or prefix> [--grants <home>/config.json]
Selfcheck: python3 bench/v2/m3_gates.py --selfcheck   (a synthetic ledger with fake ids; asserts the counting)

The v1 ledger is only read, in place (m8_fold.py's pattern): calls = `tool_call` ∪ `approval_pending`, deduped by
call id; an approval that is not a tool call (`install_server`) is counted apart. Each call goes through the real
`policy.verdict` (origin user, no session grants) with the natives from the registry (every sense imported; v1
`search_tools` -> `search_registry`) and the Notion specs `mcp/tools.py` builds from bench/v2/fixtures/notion_tools.json
under the session's server id. Map arms: no_map (annotations only), drafted (`draft_server` over the fixture), home
(`tools` in --grants' config, after P2's first start there; required to equal drafted). Policy arms: v1 (the ledger's
own approvals), manual, manual_grants (the config's grants), auto. Output docs/measurements/v2-m3-<date>.json: counts
per (tool, action verb, grade, decision) - no arguments, no ids, no content.
Pass (Q6 (b)) on drafted, and on home once it exists: manual (no grants) gates 0 reads and asks for every write,
0 calls graded unknown; auto gates <= 4. manual_grants is reported beside.
"""
import argparse, collections, datetime, json, re, sys, tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))  # this tree's pathfinder, whichever interpreter runs the bench
from pathfinder import memory, policy, signals, skills, tools  # noqa: E402,F401 - memory/skills register at import
from pathfinder.mcp import tools as mcp_tools  # noqa: E402
from pathfinder.mcp.protocol import qualify  # noqa: E402
from pathfinder.mcp.servers import Entry  # noqa: E402

RENAMED = {"search_tools": "search_registry"}  # v1 -> v2 natives (U0)
NOT_A_TOOL = "install_server"
POLICIES = {"manual": ("manual", False), "manual_grants": ("manual", True), "auto": ("auto", False)}
EXPECTED = {"calls": 31, "tool_calls": 30, "installs": 1, "read": 16, "write": 14, "v1_gates": 19,
            "no_map_manual_gates": 19, "drafted_manual_gates": 14, "drafted_auto_gates": 0}
NOT_YET = "not yet: run after P2's first start"
LEAK = re.compile(r"/Users/|@|[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)


def v1_calls(events_dir, session) -> tuple[list, int]:
    """([(name, args, gated)] of the session's tool calls in seq order, installs), deduped by call id."""
    found, sessions = {}, set()
    for path in sorted(Path(events_dir).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not str(e.get("session", "")).startswith(session) or e.get("type") not in ("tool_call", "approval_pending"):
                continue
            sessions.add(e["session"])
            pending = e["type"] == "approval_pending"
            call = found.setdefault(e["call_id"] if pending else e["id"], {"seq": e["seq"], "gated": False})
            call["gated"] |= pending
            call["name"] = e["tool"] if pending else e["name"]
            if not pending or "args" not in call:  # the ran call's own args win over the approval's copy
                call["args"] = e.get("args") if isinstance(e.get("args"), dict) else {}
    if len(sessions) != 1:
        sys.exit(f"--session matches {len(sessions)} sessions, not 1")
    calls = sorted(found.values(), key=lambda c: c["seq"])
    tool_calls = [(c["name"], c["args"], c["gated"]) for c in calls if c["name"] != NOT_A_TOOL]
    return tool_calls, len(calls) - len(tool_calls)


def build_registry(server: str) -> list:
    """`tools.TOOLS` as a daemon with every sense on and the fixture attached as `server` fills it; the definitions."""
    for _name, mod in signals.sense_modules():
        tools.TOOLS.update(getattr(mod, "TOOLS", {}) or {})
    defs = json.loads((HERE / "fixtures" / "notion_tools.json").read_text(encoding="utf-8"))
    entries = [Entry(server, d["name"], qualify(server, d["name"]), d) for d in defs]
    tools.TOOLS.update(mcp_tools.specs(entries, None, SimpleNamespace(passed={e.qualified_name for e in entries})))
    return defs


def run_arm(calls, tools_cfg, grants) -> dict:
    """{policy: {"totals", "rows"}} and the grade counts, for one map; `_calls` (dropped before output): each
    call's (grade, {policy: decision}), in order."""
    counts = {p: collections.Counter() for p in ("v1", *POLICIES)}
    grades, per_call = collections.Counter(), []
    for name, args, gated in calls:
        spec = tools.TOOLS[RENAMED.get(name, name)]
        verb = args.get("action", "") if isinstance(args.get("action", ""), str) else "?"
        grade, decided = None, {}
        for pol, (mode, with_grants) in POLICIES.items():
            cfg = {"tools": tools_cfg, "approval_mode": mode, "grants": grants if with_grants else {}}
            decided[pol], grade, _unknown = policy.verdict(spec, "user", args, cfg, set())
            counts[pol][(spec.name, verb, grade, decided[pol])] += 1
        per_call.append((grade, decided))
        counts["v1"][(spec.name, verb, grade, "ask" if gated else "allow")] += 1
        grades[grade] += 1
    out = {}
    for pol, c in counts.items():
        by = collections.Counter()
        for (_t, _v, grade, decision), n in c.items():
            by[decision] += n
            by["unknown"] += n if grade == "unknown" else 0
        totals = {"gates": by["ask"] + by["deny"], "asks": by["ask"], "allows": by["allow"], "denies": by["deny"],
                  "unknown": by["unknown"]}
        rows = [{"tool": t, "verb": v, "grade": g, "decision": d, "count": n} for (t, v, g, d), n in sorted(c.items())]
        out[pol] = {"totals": totals, "rows": rows}
    return {"grades": dict(grades), "policies": out, "_calls": per_call}


def passes(arm, kinds) -> bool:
    """Q6 (b): manual (no grants) allows every call `kinds` (the drafted grades) calls read and asks for every
    other, none graded unknown; auto gates <= 4."""
    manual = [d["manual"] for _g, d in arm["_calls"]]
    return (all(m == ("allow" if k == "read" else "ask") for k, m in zip(kinds, manual))
            and arm["policies"]["manual"]["totals"]["unknown"] == 0 and arm["policies"]["auto"]["totals"]["gates"] <= 4)


def measure(events_dir, session, config_path) -> dict:
    calls, installs = v1_calls(events_dir, session)
    servers = {n.split("__")[1] for n, _a, _g in calls if n.startswith("mcp__")}
    if len(servers) != 1:
        sys.exit(f"the session's calls name {len(servers)} MCP servers, not 1")
    server = servers.pop()
    defs = build_registry(server)
    home_cfg = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {}
    grants = home_cfg.get("grants") or {}
    drafted = {server: mcp_tools.draft_server(defs)}
    arms = {"no_map": run_arm(calls, None, grants), "drafted": run_arm(calls, drafted, grants)}
    if "tools" in home_cfg:
        arms["home"] = run_arm(calls, home_cfg["tools"], grants)
        arms["home"]["equals_drafted"] = home_cfg["tools"].get(server) == drafted[server]
    else:
        arms["home"] = NOT_YET
    kinds = [grade for grade, _d in arms["drafted"]["_calls"]]
    for arm in arms.values():
        if isinstance(arm, dict):
            arm["pass"] = passes(arm, kinds)
            del arm["_calls"]
    ok = arms["drafted"]["pass"] and (arms["home"] == NOT_YET or (arms["home"]["pass"] and arms["home"]["equals_drafted"]))
    got = {"calls": len(calls) + installs, "tool_calls": len(calls), "installs": installs,
           "read": arms["drafted"]["grades"].get("read", 0), "write": arms["drafted"]["grades"].get("write", 0),
           "v1_gates": arms["drafted"]["policies"]["v1"]["totals"]["gates"],
           "no_map_manual_gates": arms["no_map"]["policies"]["manual"]["totals"]["gates"],
           "drafted_manual_gates": arms["drafted"]["policies"]["manual"]["totals"]["gates"],
           "drafted_auto_gates": arms["drafted"]["policies"]["auto"]["totals"]["gates"]}
    return {"measurement": "v2-m3", "date": datetime.date.today().isoformat(), "model_requests": 0,
            "server_tools": len(defs), "grant_names": sorted(grants), "counts": got,
            "mismatches": {k: {"expected": v, "got": got[k]} for k, v in EXPECTED.items() if got[k] != v},
            "arms": arms, "verdict": "pass" if ok else "fail"}


def selfcheck() -> int:
    """A fake session on the fixture under a fake server: one ran read, one gated-and-ran archive, one gated archive
    that never ran, one native read, one install approval, and another session's call that must not count."""
    fake = "mcp__fake-srv__pages"
    def ev(seq, kind, cid, name, args, session="sess-a"):
        key = {"call_id": cid, "id": f"ap{seq}", "tool": name} if kind == "approval_pending" else {"id": cid, "name": name}
        return {"seq": seq, "type": kind, "session": session, "origin": "user", "args": args, **key}
    events = [ev(1, "tool_call", "c1", fake, {"action": "get", "page_id": "x"}),
              ev(2, "approval_pending", "c2", fake, {"action": "archive"}), ev(3, "tool_call", "c2", fake, {"action": "archive"}),
              ev(4, "approval_pending", "c3", fake, {"action": "archive"}),
              ev(5, "tool_call", "c4", "search_tools", {"query": "x"}),
              ev(6, "approval_pending", "c5", NOT_A_TOOL, {"name": "x"}),
              ev(7, "tool_call", "c9", fake, {"action": "archive"}, session="sess-b")]
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "2026-01-01.jsonl").write_text("\n".join(map(json.dumps, events)) + "\nnot json\n")
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"grants": {fake: "allow"}}))
        out = measure(tmp, "sess-a", cfg)
    c, d = out["counts"], out["arms"]["drafted"]["policies"]
    assert (c["calls"], c["tool_calls"], c["installs"], c["read"], c["write"], c["v1_gates"]) == (5, 4, 1, 2, 2, 2), c
    assert d["manual"]["totals"] == {"gates": 2, "asks": 2, "allows": 2, "denies": 0, "unknown": 0}, d["manual"]
    assert d["manual_grants"]["totals"]["asks"] == 0, "an allow grant lifts a write"
    assert d["auto"]["totals"]["gates"] == 0 and out["arms"]["home"] == NOT_YET
    assert out["arms"]["no_map"]["policies"]["manual"]["totals"]["gates"] == 3, "no map: pages is a whole-tool write"
    assert out["verdict"] == "pass" and out["arms"]["drafted"]["pass"] and not out["arms"]["no_map"]["pass"]
    assert not LEAK.search(json.dumps(out))
    print("selfcheck ok: 5 calls (4 tool + 1 install), 2 read / 2 write, v1 2 gates, drafted manual 2, auto 0")
    return 0


def main():
    ap = argparse.ArgumentParser(description="v2 M3: the effect map cuts the Notion gates (offline)")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--ledger", help="the v1 ledger's events directory (<home>/events); only read")
    ap.add_argument("--session", help="the session id, or a prefix that matches exactly one session")
    ap.add_argument("--grants", help="<home>/config.json: its grants (names only in the output) and `tools` map")
    ap.add_argument("--out", default=str(ROOT / "docs" / "measurements"))
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    if not (a.ledger and a.session):
        ap.error("--ledger and --session are required")
    out = measure(a.ledger, a.session, a.grants)
    text = json.dumps(out, ensure_ascii=False, indent=1) + "\n"
    if LEAK.search(text):
        sys.exit("the output holds a path, an address or an id: not written")
    path = Path(a.out) / f"v2-m3-{out['date']}.json"
    path.write_text(text, encoding="utf-8")
    for name, arm in out["arms"].items():
        print(name, arm if isinstance(arm, str) else {p: v["totals"] for p, v in arm["policies"].items()})
    print("counts", out["counts"], "mismatches", out["mismatches"], "verdict", out["verdict"], "->", path)
    return 0 if out["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
