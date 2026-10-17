#!/usr/bin/env python3
"""
m2_resume.py -- v2 measurement M2: a clone stopped at an approval gate resumes after a restart
from a context rebuilt only from the ledger (V12), on the Anthropic Messages API.
Pass line (blueprint section 13): an arm passes at N/N resumed and 0 HTTP 400.
Arms: strip-all (V12 as written), replay, strip-all-drop (beta drop_block), strip-last-only.
Usage: <venv>/python bench/v2/m2_resume.py --models M[,M2] [--trials 10] [--arm a,b] [--out DIR]
       <venv>/python bench/v2/m2_resume.py --selfcheck      (stub client, no network)
"""
import argparse, json, os, random, re, subprocess, sys, tempfile, uuid
from datetime import date

import anthropic

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- pre-registered: prompts, tools, titles, pass criteria (do not tune after a run) ----
SYSTEM = ("You are a personal assistant working in the user's notes workspace. Use the tools to act. "
          "Find a page with search_pages before acting on it. When a task is done, say so briefly.")
_obj = lambda key: {"type": "object", "properties": {key: {"type": "string"}}, "required": [key]}
TOOLS = [{"name": "search_pages", "description": "Search workspace pages by title. Returns id and title.",
          "input_schema": _obj("query")},
         {"name": "archive_page", "description": "Archive a page by id (destructive; needs user approval).",
          "input_schema": _obj("page_id")}]
TITLES = ["Quarterly garden plan", "Bicycle repair log", "Reading list draft", "Kitchen renovation ideas",
          "Old travel checklist", "Piano practice notes", "Board game rules summary", "Spring cleaning tasks",
          "Recipe trial results", "Language study plan"]
DECOYS = ["Weekly review template", "Ideas inbox"]
PROMPT = "Archive the page titled '{title}' and confirm when done."
DONE_RE = re.compile(r"\barchived\b", re.I)   # resumed = final text reports the archive, no tool call
THINKING = {"type": "adaptive", "display": "summarized"}
BETA = "thinking-binding-controls-2026-08-01"
ARMS = ["strip-all", "replay", "strip-all-drop", "strip-last-only"]
MAX_PRE = 4                  # requests allowed before the gate
EST_IN, EST_OUT = 1500, 300  # rough tokens per request, for the printed budget only


def canon(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))  # no sort_keys: key order is part of the prefix


def strip(messages, arm):
    """The arm's deliberate thinking removal; everything else is untouched."""
    if arm == "replay":
        return messages
    last = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=-1)
    return [{**m, "content": [b for b in m["content"] if b["type"] not in ("thinking", "redacted_thinking")]}
            if m["role"] == "assistant" and (arm != "strip-last-only" or i == last) else m
            for i, m in enumerate(messages)]


def ledger_append(path, ev):
    with open(path, "a", encoding="utf-8") as f:
        f.write(canon(ev) + "\n")


def restart(path, arm):
    """Fresh view: rebuild system/tools/messages from the ledger only, then approve the gate."""
    with open(path, encoding="utf-8") as f:
        ev = [json.loads(line) for line in f]
    system = next(e["text"] for e in ev if e["kind"] == "system")
    tools = next(e["tools"] for e in ev if e["kind"] == "tools")
    msgs = {}
    for e in (e for e in ev if e["kind"] == "block"):
        msgs.setdefault(e["msg"], {"role": e["role"], "content": []})["content"].append(e["block"])
    msgs = strip([msgs[i] for i in sorted(msgs)], arm)
    # ponytail: every pending tool_use gets {"ok":true}; only archive_page is ever pending here
    results = [{"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps({"ok": True})}
               for b in msgs[-1]["content"] if b["type"] == "tool_use"]
    return system, tools, msgs + [{"role": "user", "content": results}]


def prefix_identical(sent, arm, system, tools, msgs, k):
    o = json.loads(sent)
    want = canon({"system": o["system"], "tools": o["tools"], "messages": strip(o["messages"], arm)})
    return len(msgs) == k + 1 and canon({"system": system, "tools": tools, "messages": msgs[:k]}) == want


def send(client, arm, model, system, tools, messages):
    kw = dict(model=model, max_tokens=8000, system=system, tools=tools, messages=messages, thinking=THINKING)
    try:
        if arm == "strip-all-drop":
            kw["thinking"] = {**THINKING, "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
            return 200, client.beta.messages.create(betas=[BETA], **kw), ""
        return 200, client.messages.create(**kw), ""
    except anthropic.APIError as e:  # status errors carry status_code; connection errors -> "other"
        return getattr(e, "status_code", "other"), None, str(e)[:300]


def jsonable(o):
    return json.loads(json.dumps(o, default=lambda x: x.model_dump(mode="json") if hasattr(x, "model_dump") else str(x)))


def run_trial(client, model, arm, i):
    title, rng = TITLES[i % len(TITLES)], random.Random(i)
    rec = {"arm": arm, "trial": i, "title": title, "status": None, "error": "", "resumed": False,
           "extra_search": False, "input_transformations": [], "tokens": {"in": 0, "out": 0},
           "requests": 0, "prefix_identical": None, "final_text": ""}

    def account(status, resp, err):
        rec["requests"] += 1
        rec["status"], rec["error"] = status, err
        if resp is not None:
            rec["tokens"]["in"] += resp.usage.input_tokens
            rec["tokens"]["out"] += resp.usage.output_tokens
            if getattr(resp, "input_transformations", None):
                rec["input_transformations"].append(jsonable(resp.input_transformations))
        return resp
    with tempfile.TemporaryDirectory() as d:
        led, messages = os.path.join(d, "ledger.jsonl"), []
        ledger_append(led, {"kind": "system", "text": SYSTEM})
        ledger_append(led, {"kind": "tools", "tools": TOOLS})

        def add(role, content):
            for b in content:
                ledger_append(led, {"kind": "block", "msg": len(messages), "role": role, "block": b})
            messages.append({"role": role, "content": content})
        add("user", [{"type": "text", "text": PROMPT.format(title=title)}])
        for _ in range(MAX_PRE):
            resp = account(*send(client, arm, model, SYSTEM, TOOLS, messages))
            if resp is None:
                return rec
            add("assistant", [b.model_dump(mode="json", exclude_none=True) for b in resp.content])
            uses = [b for b in messages[-1]["content"] if b["type"] == "tool_use"]
            if not uses or any(u["name"] == "archive_page" for u in uses):
                break
            pages = [{"id": str(uuid.uuid4()), "title": t} for t in [title] + DECOYS]
            rng.shuffle(pages)
            add("user", [{"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(pages)} for u in uses])
        if not any(b.get("name") == "archive_page" for b in messages[-1]["content"]):
            rec["status"] = "no_gate"
            return rec
        k, sent = len(messages), canon({"system": SYSTEM, "tools": TOOLS, "messages": messages})
        del messages  # drop all in-memory state; only the ledger survives the "restart"
        system, tools, msgs = restart(led, arm)
    rec["prefix_identical"] = prefix_identical(sent, arm, system, tools, msgs, k)
    resp = account(*send(client, arm, model, system, tools, msgs))
    if resp is not None:
        out = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        names = [b["name"] for b in out if b["type"] == "tool_use"]
        rec["final_text"] = " ".join(b.get("text", "") for b in out if b["type"] == "text")[:300]
        rec["extra_search"] = "search_pages" in names
        rec["resumed"] = not names and bool(DONE_RE.search(rec["final_text"]))
    return rec


def summarize(records, arms, trials):
    out = {}
    for a in arms:
        rs = [r for r in records if r["arm"] == a]
        s = {"trials": len(rs), "resumed": sum(r["resumed"] for r in rs), "http_400": sum(r["status"] == 400 for r in rs),
             "prefix_identical": sum(bool(r["prefix_identical"]) for r in rs)}
        out[a] = {**s, "pass": s["resumed"] == trials and s["http_400"] == 0}
    return {"arms": out, "passing_arms": [a for a, s in out.items() if s["pass"]]}


def api_key():
    try:
        return os.environ.get("ANTHROPIC_API_KEY") or subprocess.run(
            ["security", "find-generic-password", "-s", "pathfinder-anthropic", "-w"],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def main():
    ap = argparse.ArgumentParser(description="v2 M2: gate-stop -> ledger -> restart -> resume")
    ap.add_argument("--models", help="comma-separated Anthropic model ids (required for a real run)")
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--arm", default=",".join(ARMS))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "measurements"), help="output directory")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        return __import__("m2_stub").selfcheck(sys.modules[__name__])  # stub lives next to this file
    arms, models = a.arm.split(","), (a.models or "").split(",")
    if not a.models or set(arms) - set(ARMS):
        ap.error("--models is required; --arm must be from " + ",".join(ARMS))
    n = a.trials * len(arms) * 3 * len(models)
    print(f"budget: ~{n} requests (trials x arms x 3 x models), ~${n * (EST_IN * 0.10 + EST_OUT * 0.50) / 1e6:.3f}"
          " at $0.10/$0.50 per MTok")
    client = anthropic.Anthropic(api_key=api_key() or sys.exit(
        "no Anthropic key: set ANTHROPIC_API_KEY or Keychain service pathfinder-anthropic"))
    for m in models:
        recs = []
        for arm in arms:
            for i in range(a.trials):
                recs.append(r := run_trial(client, m, arm, i))
                print(f"{m} {arm} #{i}: status={r['status']} resumed={r['resumed']} prefix={r['prefix_identical']}")
        res = {"measurement": "v2-M2", "model": m, "date": date.today().isoformat(), "trials": a.trials,
               **summarize(recs, arms, a.trials), "records": recs}
        path = os.path.join(a.out, f"v2-m2-{re.sub(r'[^A-Za-z0-9._-]', '_', m)}-{res['date']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(json.dumps(res["arms"], indent=2), "\npassing arms:", res["passing_arms"], "\nwrote", path)


if __name__ == "__main__":
    main()
