#!/usr/bin/env python3
"""
m1_read_result.py -- v2 measurement M1 (blueprint s13, decision V3): does the model re-find
exact IDs through a result store (r:N + read_result)? A/B: inline (8,000-char structure-aware
cut, tool messages kept) vs store (handle + minimal summary). Tasks N1-N10, pre-registered.
Pass line: Haiku fabricated IDs 0 and >= 9/10 passed; gemma4 recorded only.
Usage: python3 bench/v2/m1_read_result.py --provider ollama --models M --arm store --tasks N2
       <venv>/bin/python bench/v2/m1_read_result.py --provider anthropic --models M --arm both
       python3 bench/v2/m1_read_result.py --selfcheck
"""
import argparse, datetime, json, os, re, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_notion import READ_RESULT, Store, World, cut, dumps, notion_tools, summary
from m1_tasks import TASKS, fabricated_ids, n8_check, score, selfcheck

MAX_STEPS, INLINE_CAP, READ_CAP, PROMPT_DATE = 8, 8000, 6000, "2026-10-17"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "measurements")
BASE = ("You are PATHFINDER, the user's personal assistant. Today is {date}, timezone Asia/Seoul. "
        "You can use the user's Notion workspace through the tools provided. "
        "An ID you pass to a tool must appear in a tool result you have actually seen in this conversation; "
        "if it does not, search again{rr} first. Before archiving several items, list them by title and ask once.")
SYSTEM = {"inline": BASE.format(rr="", date=PROMPT_DATE), "store": BASE.format(
    rr=" (or open the result with read_result)", date=PROMPT_DATE) + (
    "\nTool results are stored: you receive a handle like r:3 and a short summary, not the full result."
    "\nTo see items, IDs or fields, call read_result(handle, range=[start,end] | query=\"text\" | path=\"results[2].id\")."
    "\nHandles stay valid for the whole conversation; use only handles you have been shown.")}


def render(res, name, arm, store):
    if isinstance(res, dict) and {"error", "skipped", "denied"} & set(res):
        return dumps(res)  # errors, skips and denials are never stored
    if name == "read_result":
        return cut(res, READ_CAP)
    return cut(res, INLINE_CAP) if arm == "inline" else dumps({"handle": store.put(res), "summary": summary(res)})


def run_turn(chat, msgs, tools, world, arm, store, L, turn, gate=lambda n, a: True):
    for _ in range(MAX_STEPS):
        m, (pt, ct), lat = chat(msgs, tools)
        L["steps"] += 1; L["prompt_tokens"] += pt; L["completion_tokens"] += ct; L["latencies"].append(round(lat, 2))
        msgs.append({"role": "assistant", "content": m["content"], **({"tool_calls": m["tool_calls"]} if m["tool_calls"] else {}),
                     **({"blocks": m["blocks"]} if m.get("blocks") else {})})  # blocks: Anthropic replay, verbatim
        if not m["tool_calls"]:
            return m["content"]
        stop, errs, seen = False, 0, len(L["received"])  # one prior set per batch
        for tc in m["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except ValueError:
                args = {"_raw": tc["function"]["arguments"]}
            L["calls"].append({"turn": turn, "name": name, "action": args.get("action"), "args": args, "seen_before": seen})
            if stop:
                res = {"skipped": "an earlier call in this batch was denied/failed"}
            elif not gate(name, args):
                res, stop = {"denied": "the user denied this call"}, True
            else:
                res = store.read(args) if name == "read_result" and store is not None else world.call(name, args)
                errs = errs + 1 if isinstance(res, dict) and "error" in res else 0
                stop = errs >= 2
            content = render(res, name, arm, store)
            L["raw"].append(res); L["received"].append(content)
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
    raise RuntimeError("step cap")


def new_log():
    return {"steps": 0, "calls": [], "raw": [], "received": [], "texts": [], "prompt_tokens": 0,
            "completion_tokens": 0, "latencies": []}


def run_task(tid, chat, arm):
    if tid == "N8":
        return n8_check(sys.modules[__name__]), []
    w, L, store = World(fail_first_archive=tid == "N6"), new_log(), Store() if arm == "store" else None
    tools, msgs = notion_tools() + ([READ_RESULT] if store is not None else []), [{"role": "system", "content": SYSTEM[arm]}]
    err = None
    try:
        for i, u in enumerate(TASKS[tid]):
            msgs.append({"role": "user", "content": u})  # history is append-only in both arms
            L["received"].append(u)
            L["texts"].append(run_turn(chat, msgs, tools, w, arm, store, L, i))
    except Exception as e:  # HTTP error, request cap: recorded, task fails
        err = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
    L["fabricated_ids"] = fabricated_ids(L)
    ok, reason = score(tid, L, w) if not err else (False, err)
    out = {k: v for k, v in L.items() if k not in ("raw", "received")}
    return {"id": tid, "passed": bool(ok), "reason": reason, "error": err, "final_text": L["texts"][-1] if L["texts"] else "", **out}, msgs


def ollama_chat(model):
    def chat(msgs, tools):
        req = urllib.request.Request("http://localhost:11434/v1/chat/completions", dumps(
            {"model": model, "messages": msgs, "tools": tools, "stream": False}).encode(), {"Content-Type": "application/json"})
        t = time.time()
        with urllib.request.urlopen(req, timeout=50) as r:
            d = json.load(r)
        m, u = d["choices"][0]["message"], d.get("usage") or {}
        tcs = [{"id": tc.get("id") or f"call_{i}", "type": "function", "function": {"name": tc["function"]["name"],
                "arguments": tc["function"].get("arguments") or "{}"}} for i, tc in enumerate(m.get("tool_calls") or [])]
        return {"content": m.get("content") or "", "tool_calls": tcs}, (u.get("prompt_tokens", 0), u.get("completion_tokens", 0)), time.time() - t
    return chat


def anthropic_chat(model):
    import anthropic  # only in the Anthropic arm (venv)
    key = os.environ.get("ANTHROPIC_API_KEY") or subprocess.run(["security", "find-generic-password", "-s",
          "pathfinder-anthropic", "-w"], capture_output=True, text=True).stdout.strip()
    if not key:
        sys.exit("no Anthropic key (env ANTHROPIC_API_KEY or Keychain service pathfinder-anthropic)")
    client = anthropic.Anthropic(api_key=key)

    def chat(msgs, tools):
        out = []
        for m in msgs[1:]:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                out.append({"role": "assistant", "content": m.get("blocks") or ([{"type": "text", "text": m["content"]}] if m["content"] else []) + [
                    {"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": json.loads(tc["function"]["arguments"])}
                    for tc in m.get("tool_calls") or []] or [{"type": "text", "text": "(no text)"}]})
            else:
                b = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(b)
                else:
                    out.append({"role": "user", "content": [b]})
        t = time.time()
        r = client.messages.create(model=model, max_tokens=4096, system=msgs[0]["content"], messages=out, tools=[
            {"name": f["function"]["name"], "description": f["function"]["description"], "input_schema": f["function"]["parameters"]}
            for f in tools])
        tcs = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": dumps(b.input)}} for b in r.content if b.type == "tool_use"]
        return ({"content": "".join(b.text for b in r.content if b.type == "text"), "tool_calls": tcs,
                 "blocks": [b.model_dump(exclude_none=True) for b in r.content]},
                (r.usage.input_tokens, r.usage.output_tokens), time.time() - t)
    return chat


def capped(chat, n):
    left = [n]

    def f(*a):
        if left[0] <= 0:
            raise RuntimeError("request cap reached")
        left[0] -= 1
        return chat(*a)
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--provider", choices=["ollama", "anthropic"], default="ollama")
    ap.add_argument("--models", default="")
    ap.add_argument("--arm", choices=["inline", "store", "both"], default="both")
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--out", default=OUT_DIR, help="output directory")
    ap.add_argument("--max-requests", type=int, default=10 ** 6, help="hard cap on model requests per model+arm")
    ap.add_argument("--dump", default="", help="write the messages per task here; {arm} is replaced")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck(sys.modules[__name__])
    if not a.models:
        sys.exit("--models is required")
    today = datetime.date.today().isoformat()
    for model in a.models.split(","):
        for arm in (["inline", "store"] if a.arm == "both" else [a.arm]):
            chat = capped((ollama_chat if a.provider == "ollama" else anthropic_chat)(model), a.max_requests)
            results, dump = [], {}
            for tid in a.tasks.split(","):
                r, dump[tid] = run_task(tid, chat, arm)
                results.append(r)
                print(f"{model} {arm} {tid}: {'PASS' if r['passed'] else 'FAIL'} fab={len(r['fabricated_ids'])} {r['reason']}")
            mr, n8 = [r for r in results if r["id"] != "N8"], [r["passed"] for r in results if r["id"] == "N8"]
            doc = {"provider": a.provider, "model": model, "arm": arm, "date": today, "max_steps": MAX_STEPS, "results": results,
                   "summary": {"passed": sum(r["passed"] for r in results), "of": len(results),
                               "model_passed": sum(r["passed"] for r in mr), "model_of": len(mr), "n8_harness": n8[0] if n8 else None,
                               "fabricated_total": sum(len(r["fabricated_ids"]) for r in results),
                               "by_task": {r["id"]: r["passed"] for r in results}}}
            path = os.path.join(a.out, f"v2-m1-{arm}-{re.sub(r'[^A-Za-z0-9.]+', '-', model)}-{today}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            if a.dump:
                with open(a.dump.replace("{arm}", arm), "w", encoding="utf-8") as f:
                    json.dump(dump, f, ensure_ascii=False, indent=1)
            print(json.dumps(doc["summary"], ensure_ascii=False), "->", path)


if __name__ == "__main__":
    main()
