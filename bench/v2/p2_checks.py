"""p2_checks.py -- the partition-2 live check's assertions (docs/plans/v2-partition-2.md, "The live check"): the
checks find, gate, fallback and ollama, one per step, each a list of (label, ok) over the whole ledger and the step's
mark; and the dry run on a synthetic ledger. p2_runs.py drives the daemon, calls these and prints the table (split
for the 200-line cap, as p1_runs/p1_checks). Stdlib only.
"""
import json, re, tempfile
from datetime import datetime
from pathlib import Path

from p1_checks import chain, events, find

SERVER, TITLE = "mcp__fake-notion__", "휴가 계획"
ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{16,}", re.I)  # results._ID


def calls(ev):
    """(assistant seq, call id, tool, arguments, block name) of every call; a call_tool is unwrapped (arguments may
    be JSON text), its block name stays "call_tool"."""
    out = []
    for e in ev:
        for b in e.get("blocks") or [] if e["type"] == "assistant" else []:
            if b["type"] != "tool_use":  # thinking, reasoning, text
                continue
            name, args = (b["input"].get("name"), b["input"].get("arguments")) if b["name"] == "call_tool" else (b["name"], b["input"])
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            out.append((e["seq"], b["id"], name, args if isinstance(args, dict) else {}, b["name"]))
    return out


def ran(ev, tool, actions=None):
    """The ok results of calls to `tool` made through call_tool (with an action in `actions`, if given)."""
    ids = {i for _, i, name, args, via in calls(ev)
           if name == tool and via == "call_tool" and (actions is None or args.get("action") in actions)}
    return [e for e in ev if e["type"] == "result" and e["call_id"] in ids and e["ok"]]


def fabricated(ev, after):
    """IDs in the arguments of a call after seq `after` that no earlier result body holds (dashes, case ignored)."""
    norm, bad = lambda s: s.replace("-", "").lower(), []
    for seq, _, _, args, _ in calls(e for e in ev if e["seq"] > after):
        seen = norm(" ".join(json.dumps(e.get("body"), ensure_ascii=False) for e in ev if e["type"] == "result" and e["seq"] < seq))
        bad += [i for i in ID.findall(json.dumps(args)) if norm(i) not in seen]
    return bad


def page(world, title):
    return next((p for p in world["pages"] if p["title"] == title), {})


def check_find(ev, c):
    w, t = [e for e in ev if e["seq"] > c["mark"]], page(c["world"], TITLE).get("last_edited_time", "?")
    d = datetime.fromisoformat(t.replace("Z", "+00:00")) if t != "?" else datetime.min
    action = c["draft"].get("pages", {}).get("effect_by", {}).get("action", {})
    final = [e["text"] for e in w if e["type"] == "assistant" and e.get("final")]
    bad = fabricated(ev, c["mark"])
    c["notes"].append(f"step 1 final text: {final[-1][:200] if final else None!r}")
    return [("the draft: pages get read, archive write", (action.get("get"), action.get("archive")) == ("read", "write")),
            ("one config_set {key: tools}", sum(e["type"] == "config_set" and e.get("key") == "tools" for e in ev) == 1),
            ("a search_tools result naming fake-notion tools",
             any(e["type"] == "result" and e["tool"] == "search_tools" and SERVER in json.dumps(e["body"]) for e in w)),
            ("a call_tool workspace/pages get or search, ok",
             bool(ran(w, SERVER + "workspace", {"get", "search"}) + ran(w, SERVER + "pages", {"get", "search"}))),
            ("no gate", find(w, "gate") is None), (f"fabricated IDs 0 (got {bad})", not bad),
            ("the final text holds the world's time", bool(final) and any(x in final[-1] for x in (
                t[:10], f"{d:%B} {d.day}", f"{d:%b} {d.day}", f"{d.day} {d:%B}", f"{d.month}월 {d.day}일")))]


def check_gate(ev, c):
    w = [e for e in ev if e["seq"] > c["mark"]]
    g = find(w, "gate", tool=SERVER + "pages") or {}
    bad = fabricated(ev, c["mark"])
    return [("a pages gate, effect write", g.get("effect") == "write"),
            (f"its title names {TITLE}", TITLE in (g.get("title") or "")),
            ("approvals prints the title", TITLE in c["out"]["approvals2"]), (f"fabricated IDs 0 (got {bad})", not bad),
            ("result {gate: N} ok, then the report turn ends answer", bool(g) and chain(
                w, ("result", {"gate": g["seq"], "ok": True}), ("user", {"gate": [g["seq"]]}), ("turn_end", {"kind": "answer"}))),
            ("the world marks the page archived", page(c["world"], TITLE).get("id", "?") in c["world"]["archived"])]


def check_fallback(ev, c):
    w = [e for e in ev if e["seq"] > c["mark"]]
    s = next((e for e in w if e["type"] == "result" and e["tool"] == "search_tools"), None)
    body = json.dumps(s["body"], ensure_ascii=False) if s else ""
    return [("search_tools says search is unavailable and lists the tools",
             "search is unavailable" in body and SERVER + "workspace" in body),
            ("then a call_tool to workspace, ok", s is not None and any(e["seq"] > s["seq"] for e in ran(w, SERVER + "workspace"))),
            ("the turn ends answer", find(w, "turn_end", kind="answer") is not None), ("no error event", find(w, "error") is None)]


def check_ollama(ev, c):
    w = [e for e in ev if e["seq"] > c["mark"]]
    g = find(w, "gate", tool=SERVER + "pages") or {}
    props, new = g.get("args", {}).get("properties"), c["world"]["pages"][c["pages0"]:]
    return [("a pages call_tool whose recorded arguments hold properties",
             any(name == SERVER + "pages" and via == "call_tool" and "properties" in args for _, _, name, args, via in calls(w))),
            ("gated, and the approved call ran ok", bool(g) and find(w, "result", gate=g["seq"], ok=True) is not None),
            ("the fake server got the properties unchanged", props is not None and [p.get("properties") for p in new] == [props])]


CHECKS = {1: check_find, 2: check_gate, 3: check_fallback, 4: check_ollama}

def dry_run():
    """A passing run's ledger and world written by hand, read back through `events`; then each step on a broken
    copy (`BREAK`: a fabricated id or direct mcp__ calls; no gate or an empty title; no search note; no gate or
    the properties dropped), which must FAIL."""
    pid, t, props, ev, marks = "e3e70682-c209-4cac-a29f-6fbed82c07cd", "2026-09-06T02:15:00.000Z", {"Status": "Done"}, [], []

    def add(type, **f):
        ev.append({"seq": len(ev) + 1, "type": type, "session": "s1", "turn": "t", "origin": "user", "clone": "root", **f})
        return ev[-1]["seq"]

    use = lambda i, n, /, **inp: {"type": "tool_use", "id": i, "name": n, "input": inp}
    proxy = lambda i, tool, **args: [use(i, "call_tool", name=SERVER + tool, arguments=args)]
    add("config_set", key="tools"), marks.append(len(ev))
    add("assistant", blocks=[{"type": "reasoning", "text": "..."}, use("c1", "search_tools", query="Notion page")])
    add("result", call_id="c1", tool="search_tools", ok=True, body=f"{SERVER}workspace ... {SERVER}pages ...")
    add("assistant", blocks=proxy("c2", "workspace", action="search", query=TITLE))
    add("result", call_id="c2", tool=SERVER + "workspace", ok=True, body={"results": [{"id": pid, "title": TITLE}]})
    add("assistant", blocks=proxy("c3", "pages", action="get", page_id=pid.replace("-", "")))
    add("result", call_id="c3", tool=SERVER + "pages", ok=True, body={"id": pid, "last_edited_time": t})
    add("assistant", blocks=[], final=True, text="It was last edited on 2026-09-06."), add("turn_end", kind="answer")
    marks.append(len(ev))
    add("assistant", blocks=proxy("c4", "pages", action="archive", page_id=pid))
    g = add("gate", call_id="c4", tool=SERVER + "pages", effect="write", title=f"page_id: {TITLE}", args={"page_id": pid})
    add("turn_end", kind="answer"), add("resume", gate=g, decision="approved")
    add("result", call_id="c4", tool=SERVER + "pages", ok=True, gate=g, body={})
    add("user", origin="system", gate=[g]), add("turn_end", kind="answer"), marks.append(len(ev))
    add("assistant", blocks=[use("c5", "search_tools", query="휴가")])
    add("result", call_id="c5", tool="search_tools", ok=True, body=f"Tool search is unavailable (x). {SERVER}workspace")
    add("assistant", blocks=proxy("c6", "workspace", action="search", query="휴가"))
    add("result", call_id="c6", tool=SERVER + "workspace", ok=True, body={}), add("turn_end", kind="answer")
    marks.append(len(ev))
    add("assistant", blocks=proxy("c7", "pages", action="create", title="P2 check", properties=json.dumps(props)))
    g4 = add("gate", call_id="c7", tool=SERVER + "pages", effect="write", title="", args={"properties": props})
    add("result", call_id="c7", tool=SERVER + "pages", ok=True, gate=g4, body={}), marks.append(len(ev))
    world = {"pages": [{"id": pid, "title": TITLE, "last_edited_time": t}, {"id": "n", "title": "P2 check", "properties": props}],
             "archived": [pid]}
    drop = lambda type: lambda ev: [e for e in ev if e["type"] != type]
    gates = lambda **f: lambda ev: [{**e, **f} if e["type"] == "gate" else e for e in ev]
    direct = lambda b: {**b, "name": b["input"]["name"], "input": b["input"]["arguments"]} if b["name"] == "call_tool" else b
    BREAK = {1: [("a fabricated id", lambda ev: ev + [{"seq": 999, "type": "assistant",
                                                        "blocks": proxy("c9", "pages", action="get", page_id="0123456789abcdef01")}]),
                 ("direct mcp__ calls", lambda ev: [{**e, "blocks": [direct(b) for b in e["blocks"] if b["type"] == "tool_use"]}
                                                    if e["type"] == "assistant" else e for e in ev])],
             2: [("its gate", drop("gate")), ("an empty gate title", gates(title=""))],
             3: [("search's note", lambda ev: [e for e in ev if e.get("tool") != "search_tools"])],
             4: [("its gate", drop("gate")), ("properties dropped", gates(args={"action": "create"}))]}
    rows, broken = [], []
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "events").mkdir()
        (Path(tmp) / "events" / "2026-10-19.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in ev))
        for n, check in CHECKS.items():
            c = {"mark": marks[n - 1], "world": world, "pages0": 1, "notes": [], "out": {"approvals2": f"{g}  pages  write  page_id: {TITLE}"},
                 "draft": {"pages": {"effect_by": {"action": {"get": "read", "archive": "write"}}}}}
            upto = events(Path(tmp))[:marks[n]]
            rows.append((n, check(upto, c)))
            broken.append((f"{n} broken", [(f"fails with {label}", not all(ok for _, ok in check(cut(upto), c)))
                                           for label, cut in BREAK[n]]))
    return rows + broken


