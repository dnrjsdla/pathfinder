#!/usr/bin/env python3
"""
m5b_proxy.py -- v2 M5b (docs/plans/v2-partition-2.md, Measurements): M1's store arm with the Notion tools behind
the product's `search_tools` (`search.find` over the registry m5_registry builds: natives + the fixture's 11 Notion
tools as mcp/tools.py makes them) and `call_tool {name, arguments}`, unwrapped here and dispatched to fake_notion's
world as M1 dispatches a direct call. Everything else (store, handles, render, scoring, fabricated IDs, 8 steps) is M1's.
Pass (pre-registered): Haiku fabricated IDs 0, passes >= M1 store's 5/9, first-call argument errors <= 2.
gemma4 (Ollama): the one task whose call carries an inner `properties`, 3 runs; recorded, not judged here.

Usage: python3 bench/v2/m5b_proxy.py --selfcheck
       <venv>/bin/python bench/v2/m5b_proxy.py --model M --embed-model ollama:E --max-requests 60 --price-in X --price-out Y
       (then --gemma M --embed-model ollama:E --out <that JSON> adds the gemma4 line to it)
"""
import argparse, datetime, itertools, json, os, re, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import m1_read_result as m1  # noqa: E402
from fake_notion import READ_RESULT, Store, World, dumps  # noqa: E402
from m1_tasks import TASKS, fabricated_ids, score  # noqa: E402
from m5_registry import SERVER, build_registry  # noqa: E402

from pathfinder import config, search, tools  # noqa: E402
from pathfinder.mcp.protocol import qualify  # noqa: E402

M1_TASKS = ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N9", "N10"]  # the nine M1's store arm ran (N8 is harness-only)
M1_STORE_PASSED = 5
SPECS = {d["name"]: d["inputSchema"] for d in json.loads((HERE / "fixtures" / "notion_tools.json").read_text("utf-8"))}
NOTION = {qualify(SERVER, n): n for n in build_registry()}  # qualified name -> fake_notion's bare name
TOOL_LIST = [READ_RESULT, search.SEARCH_TOOLS.schema(), {"type": "function", "function": tools.CALL_TOOL}]
TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "object": dict, "array": list}
GEMMA_TASK = "강의 노트 DB(ID {db})에 새 행 추가해줘: 이름 '9주차 보강', Status는 Not started"

_render = m1.render  # the product shows search_tools in full (results.INLINE); everything else as M1 renders it
m1.render = lambda res, name, arm, store: res if name == "search_tools" else _render(res, name, arm, store)


class ProxyWorld(World):
    def call(self, name, args):
        if name == "search_tools":
            return search.find(str(args.get("query") or ""), search.HOME)
        if name == "call_tool":
            if args.get("name") not in NOTION:
                return {"error": f"unknown tool {args.get('name')!r}: use a name search_tools gave"}
            if not isinstance(args.get("arguments"), dict):
                return {"error": "arguments must be an object"}
            return super().call(NOTION[args["name"]], args["arguments"])
        return super().call(name, args)


def arg_problems(args):
    """Why a call_tool's inner arguments fail the real spec (required keys, declared types); [] if they pass."""
    inner, a = NOTION.get(args.get("name")), args.get("arguments")
    if inner is None:
        return [f"unknown tool {args.get('name')!r}"]
    if not isinstance(a, dict):
        return ["arguments not an object"]
    props = SPECS[inner].get("properties") or {}
    bad = [f"missing {k}" for k in SPECS[inner].get("required") or [] if k not in a]
    for k, v in a.items():
        want = TYPES.get((props.get(k) or {}).get("type"))
        if want and (not isinstance(v, want) or (isinstance(v, bool) and want is not bool)):
            bad.append(f"{k}: {type(v).__name__} for {props[k]['type']}")
    return bad


def unwrap(calls):
    """call_tool entries as the inner call (bare name, inner action/args) so M1's scorers read them unchanged."""
    out = []
    for c in calls:
        if c["name"] == "call_tool":
            a = c["args"].get("arguments") if isinstance(c["args"].get("arguments"), dict) else {}
            c = dict(c, name=NOTION.get(c["args"].get("name"), c["args"].get("name")), action=a.get("action"), args=a,
                     via="call_tool", arg_problems=arg_problems(c["args"]))
        out.append(c)
    return out


def run_task(tid, chat, turns=None):
    w, L, store = ProxyWorld(fail_first_archive=tid == "N6"), m1.new_log(), Store()
    msgs, err = [{"role": "system", "content": m1.SYSTEM["store"]}], None
    try:
        for i, u in enumerate(turns or TASKS[tid]):
            msgs.append({"role": "user", "content": u})
            L["received"].append(u)
            L["texts"].append(m1.run_turn(chat, msgs, TOOL_LIST, w, "store", store, L, i))
    except Exception as e:  # HTTP error, request cap: recorded, task fails
        err = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
    L["fabricated_ids"] = fabricated_ids(L)
    L["calls"] = unwrap(L["calls"])
    proxied = [c for c in L["calls"] if c.get("via")]
    ok, reason = score(tid, L, w) if not err and tid in TASKS else (False, err or "")
    out = {k: v for k, v in L.items() if k not in ("raw", "received")}
    return {"id": tid, "passed": bool(ok), "reason": reason, "error": err, "requests": L["steps"],
            "searches": sum(c["name"] == "search_tools" for c in L["calls"]), "call_tools": len(proxied),
            "first_call_arg_error": bool(proxied and proxied[0]["arg_problems"]),
            "arg_error_calls": sum(bool(c["arg_problems"]) for c in proxied),
            "final_text": L["texts"][-1] if L["texts"] else "", **out}, msgs


def selfcheck():
    w, vac = World(), World().by_title["휴가 계획"]["id"]
    n, ws, pages = itertools.count(), qualify(SERVER, "workspace"), qualify(SERVER, "pages")

    def tool(name, args):
        return {"content": "", "tool_calls": [{"id": f"t{next(n)}", "type": "function", "function": {"name": name, "arguments": dumps(args)}}]}
    script = iter([tool("search_tools", {"query": ws}), tool("call_tool", {"name": ws, "arguments": {"action": "search"}}),
                   {"content": "1. 휴가 계획\n2. 강의 노트\n3. 독서 목록", "tool_calls": []},
                   tool("read_result", {"handle": "r:1", "query": "휴가 계획"}),
                   tool("call_tool", {"name": pages, "arguments": {"page_id": vac}}),  # no action: an arg error
                   tool("call_tool", {"name": pages, "arguments": {"action": "archive", "page_id": vac}}),
                   {"content": "지웠어요.", "tool_calls": []}])
    r, msgs = run_task("N2", lambda m, t: (next(script), (0, 0), 0.0) if t == TOOL_LIST else None)
    tool_msgs = [m["content"] for m in msgs if m["role"] == "tool"]
    assert not any(f["function"]["name"] in NOTION for f in TOOL_LIST)
    assert tool_msgs[0].startswith("The tool, called through call_tool:") and '"name": "' + ws in tool_msgs[0], tool_msgs[0]
    assert json.loads(tool_msgs[1])["handle"] == "r:1" and "unsupported" in tool_msgs[3], tool_msgs[1:4]
    assert r["passed"] and r["fabricated_ids"] == [] and r["first_call_arg_error"] is False and r["arg_error_calls"] == 1, r
    assert [c["name"] for c in r["calls"]] == ["search_tools", "workspace", "read_result", "pages", "pages"], r["calls"]
    assert arg_problems({"name": pages, "arguments": {"action": "get", "archived": "yes"}}) == ["archived: str for boolean"]
    assert arg_problems({"name": "pages", "arguments": {}}) == ["unknown tool 'pages'"] and w.archived == set()
    print(f"selfcheck ok: {len(NOTION)} Notion tools behind search_tools/call_tool, {len(TOOL_LIST)} tools listed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--model", default="", help="Anthropic model ID (the Haiku run)")
    ap.add_argument("--gemma", default="", help="Ollama model (the properties runs)")
    ap.add_argument("--embed-model", default="", help="provider:model, as config's embed_model takes it")
    ap.add_argument("--max-requests", type=int, default=60)
    ap.add_argument("--price-in", type=float, default=0.0, help="$ per Mtok")
    ap.add_argument("--price-out", type=float, default=0.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--dump", default="", help="write the messages per task here (outside the repo)")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    if not a.embed_model or not (a.model or a.gemma):
        sys.exit("--embed-model and --model or --gemma are required")
    with tempfile.TemporaryDirectory() as tmp:
        search.HOME = Path(tmp)
        config.set(search.HOME, "embed_model", a.embed_model)
        if a.model:
            haiku(a)
        if a.gemma:
            gemma(a)


def haiku(a):
    today, chat = datetime.date.today().isoformat(), m1.capped(m1.anthropic_chat(a.model), a.max_requests)
    results, dump = [], {}
    for tid in M1_TASKS:
        r, dump[tid] = run_task(tid, chat)
        results.append(r)
        print(f"{tid}: {'PASS' if r['passed'] else 'FAIL'} req={r['requests']} fab={len(r['fabricated_ids'])} "
              f"argerr1={r['first_call_arg_error']} {r['reason']}")
    pt, ct = sum(r["prompt_tokens"] for r in results), sum(r["completion_tokens"] for r in results)
    s = {"passed": sum(r["passed"] for r in results), "of": len(results), "m1_store_passed": M1_STORE_PASSED,
         "fabricated_total": sum(len(r["fabricated_ids"]) for r in results),
         "first_call_arg_errors": sum(r["first_call_arg_error"] for r in results),
         "arg_error_calls": sum(r["arg_error_calls"] for r in results), "requests": sum(r["requests"] for r in results),
         "prompt_tokens": pt, "completion_tokens": ct, "cost_usd": round((pt * a.price_in + ct * a.price_out) / 1e6, 4),
         "by_task": {r["id"]: r["passed"] for r in results}}
    s["pass"] = s["fabricated_total"] == 0 and s["passed"] >= M1_STORE_PASSED and s["first_call_arg_errors"] <= 2
    doc = {"measurement": "M5b proxy call (v2 partition 2)", "provider": "anthropic", "model": a.model, "date": today,
           "embed_model": a.embed_model, "max_steps": m1.MAX_STEPS, "max_requests": a.max_requests,
           "prices_per_mtok": {"in": a.price_in, "out": a.price_out}, "summary": s, "results": results,
           "rule": "fabricated 0, passes >= M1 store (5/9), first-call argument errors <= 2"}
    path = a.out or str(HERE.parents[1] / "docs" / "measurements" / f"v2-m5b-{re.sub(r'[^A-Za-z0-9.]+', '-', a.model)}-{today}.json")
    Path(path).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if a.dump:
        Path(a.dump).write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False), "->", path)


def has_properties(x):
    """A key literally named `properties` holding an object, at any depth of the dispatched arguments."""
    inside = x.values() if isinstance(x, dict) else x if isinstance(x, list) else []
    return isinstance(x, dict) and isinstance(x.get("properties"), dict) or any(has_properties(v) for v in inside)


def gemma(a):
    """3 runs, at most 2 requests each (search_tools, then the call): does the inner `properties` arrive intact?"""
    runs = []
    for _ in range(3):
        r, _msgs = run_task("P1", m1.capped(m1.ollama_chat(a.gemma), 2), [GEMMA_TASK.format(db=World().db["id"])])
        runs.append({"requests": r["requests"], "calls": [{"name": c["name"], "args": c["args"]} for c in r["calls"]],
                     "properties_intact": any(has_properties(c["args"]) for c in r["calls"] if c.get("via")),
                     "error": r["error"], "final_text": r["final_text"]})
        print(json.dumps(runs[-1], ensure_ascii=False))
    doc = json.loads(Path(a.out).read_text("utf-8"))
    doc["gemma4"] = {"model": a.gemma, "task": GEMMA_TASK, "runs": runs,
                     "properties_intact": f"{sum(r['properties_intact'] for r in runs)}/{len(runs)}"}
    Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
