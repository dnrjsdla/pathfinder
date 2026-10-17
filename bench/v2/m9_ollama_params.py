#!/usr/bin/env python3
"""
m9_ollama_params.py -- measurement M9 (blueprint s13): does Ollama drop tool parameters named
`properties` / `description` (ollama/ollama#18468)? Real Notion specs (pages, databases) vs a control
arm with the suspect params renamed. Ollama version matters: it is recorded in the output.
Stdlib only. One request per probe, non-streaming, default tool_choice.

Pre-registered verdict rule (fixed before the first run):
  suspects = pages.properties, databases.properties, databases.description.
  A suspect is "seen" in the real arm if it appears in the P1 echo for that tool, or (properties for
  pages) as a top-level key in P2's pages call args, or (properties, description for databases) in P3's args.
  The control arm is read the same way with the renamed names.
  - "not dropped": all 3 suspects seen in the real arm.
  - "dropped": 0 seen in the real arm AND >= 1 renamed name seen in the control arm.
  - "partial": 1-2 suspects seen in the real arm AND the control arm saw >= as many renamed names.
  - "inconclusive": anything else (e.g. the model ignores the instruction in both arms).

Usage: python3 bench/v2/m9_ollama_params.py --models <ollama-model> [--out p] [--host http://localhost:11434]
       python3 bench/v2/m9_ollama_params.py --selfcheck
"""
import argparse
import copy
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PARENT = "3f6c1d52-8a47-4e0b-9c15-2d7b6e0a94f1"  # synthetic uuid4
SYSTEM = "You are a tool-use tester. Follow the instruction exactly."
PROMPTS = {
    "P1": ("List every top-level parameter name of the `pages` tool and of the `databases` tool exactly as "
           'given in their schemas, as JSON `{"pages":[...],"databases":[...]}`. Do not call any tool.'),
    "P2": (f"Create a Notion page titled 'Bench Page' under parent page id {PARENT} with the property "
           "Status set to Done. Call the pages tool now."),
    "P3": (f"Create a Notion database titled 'Bench DB' under parent page id {PARENT} with description "
           "'bench' and one property Status (select). Call the databases tool now."),
}
# (tool, real name) -> control name
RENAME = {("pages", "properties"): "page_properties", ("databases", "properties"): "db_properties",
          ("databases", "description"): "db_description"}
# which probe's call args can show the suspect
CALL_PROBE = {"pages": "P2", "databases": "P3"}


def load_tools():
    """-> (real, control): OpenAI function-format lists of the Notion pages and databases tools."""
    mcp = {t["name"]: t for t in json.loads((HERE / "fixtures" / "notion_tools.json").read_text())}
    real = [{"type": "function", "function": {"name": n, "description": mcp[n]["description"],
                                              "parameters": copy.deepcopy(mcp[n]["inputSchema"])}}
            for n in ("pages", "databases")]
    control = copy.deepcopy(real)
    for t in control:
        fn = t["function"]
        props = fn["parameters"]["properties"]
        for (tool, old), new in RENAME.items():
            if tool == fn["name"]:
                props[new] = props.pop(old)
        if fn["name"] == "pages":
            fn["description"] = re.sub(r"\bproperties\b", "page_properties", fn["description"])
        else:
            fn["description"] = fn["description"].replace("title, properties, is_inline", "title, db_properties, is_inline")
    return real, control


def names_in_echo(text):
    """Parse {"pages":[...],"databases":[...]} out of free text -> {tool: [names]}; {} if unparseable."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
        return {k: [str(x) for x in v] for k, v in d.items() if k in ("pages", "databases") and isinstance(v, list)}
    except (ValueError, AttributeError):
        return {}


def seen(arm, probes):
    """-> set of 'tool.name' (using the arm's own names) that showed up in P1's echo or the call args."""
    out = set()
    for (tool, old), new in RENAME.items():
        name = old if arm == "real" else new
        if name in probes["P1"]["echo"].get(tool, []):
            out.add(f"{tool}.{old}")
        p = probes[CALL_PROBE[tool]]
        if p["call_tool"] == tool and name in p["call_args"]:
            out.add(f"{tool}.{old}")
    return out


def verdict(probes_by_arm):
    r, c = seen("real", probes_by_arm["real"]), seen("control", probes_by_arm["control"])
    if len(r) == 3:
        v = "not dropped"
    elif not r and c:
        v = "dropped"
    elif r and len(c) >= len(r):
        v = "partial"
    else:
        v = "inconclusive"
    return {"verdict": v, "real_seen": sorted(r), "control_seen": sorted(c)}


def chat(host, model, tools, user):
    body = {"model": model, "stream": False, "tools": tools,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    req = urllib.request.Request(host + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=50) as r:
        return json.load(r)["choices"][0]["message"]


def probe(host, model, tools, pid):
    msg = chat(host, model, tools, PROMPTS[pid])
    calls = msg.get("tool_calls") or []
    c = calls[0]["function"] if calls else {}
    try:
        args = json.loads(c.get("arguments") or "{}") if isinstance(c.get("arguments"), str) else (c.get("arguments") or {})
    except ValueError:
        args = {}
    if not isinstance(args, dict):  # double-encoded arguments must not turn `name in args` into a substring test
        args = {}
    return {"prompt": PROMPTS[pid], "raw_text": msg.get("content"), "raw_tool_calls": calls,
            "echo": names_in_echo(msg.get("content")) if pid == "P1" else {},
            "call_tool": c.get("name"), "call_args": args}


def selfcheck():
    real, control = load_tools()
    rp, cp = real[0]["function"]["parameters"]["properties"], control[0]["function"]["parameters"]["properties"]
    assert "properties" in rp and "page_properties" in cp and "properties" not in cp
    dp = control[1]["function"]["parameters"]["properties"]
    assert "db_properties" in dp and "db_description" in dp and "description" not in dp and "page_properties" in dp
    assert names_in_echo('x ```json\n{"pages":["a","properties"],"databases":["description"]}\n```')["pages"][1] == "properties"
    empty = {"P1": {"echo": {}, "call_tool": None, "call_args": {}}, "P2": {"echo": {}, "call_tool": None, "call_args": {}},
             "P3": {"echo": {}, "call_tool": None, "call_args": {}}}
    full = {"P1": {"echo": {"pages": ["properties"], "databases": ["properties", "description"]}, "call_tool": None, "call_args": {}},
            "P2": {"echo": {}, "call_tool": None, "call_args": {}}, "P3": {"echo": {}, "call_tool": None, "call_args": {}}}
    ctl = {"P1": {"echo": {"pages": ["page_properties"], "databases": ["db_properties", "db_description"]}, "call_tool": None, "call_args": {}},
           "P2": {"echo": {}, "call_tool": None, "call_args": {}}, "P3": {"echo": {}, "call_tool": None, "call_args": {}}}
    assert verdict({"real": full, "control": ctl})["verdict"] == "not dropped"
    assert verdict({"real": empty, "control": ctl})["verdict"] == "dropped"
    assert verdict({"real": empty, "control": empty})["verdict"] == "inconclusive"
    print("selfcheck ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--models", default="")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--out", default=str(ROOT / "docs" / "measurements" / f"v2-m9-{date.today()}.json"))
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    if not a.models:
        ap.error("--models is required for a real run")
    with urllib.request.urlopen(a.host + "/api/version", timeout=10) as r:
        version = json.load(r).get("version")
    real, control = load_tools()
    results = []
    for model in filter(None, a.models.split(",")):
        by_arm = {arm: {pid: probe(a.host, model, tools, pid) for pid in PROMPTS}
                  for arm, tools in (("real", real), ("control", control))}
        v = verdict(by_arm)
        results.append({"model": model, **v, "probes": [
            {"arm": arm, "probe": pid, "prompt": p["prompt"], "raw": {"text": p["raw_text"], "tool_calls": p["raw_tool_calls"]},
             "found_names": p["echo"] if pid == "P1" else {p["call_tool"]: sorted(p["call_args"])},
             "ok": bool(p["echo"] or p["call_tool"])}  # ok = the probe produced an echo or a call, not "suspect seen"
            for arm, ps in by_arm.items() for pid, p in ps.items()]})
        print(model, v)
    Path(a.out).write_text(json.dumps({"measurement": "M9", "date": str(date.today()), "ollama_version": version,
        "note": "Ollama version matters: #18468 may be fixed or regressed between releases; re-run after upgrades.",
        "results": results}, ensure_ascii=False, indent=1))
    print("ollama", version, "wrote", a.out)


if __name__ == "__main__":
    sys.exit(main())
