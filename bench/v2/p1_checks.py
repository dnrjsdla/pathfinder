"""p1_checks.py -- the partition-1 live check's assertions (docs/plans/v2-partition-1.md, "The live check"):
the ledger reader, one check per step, and the dry run on a synthetic ledger. A check is a list of (label, ok),
ok True, False, or None (untested). p1_runs.py drives the daemon, calls these and prints the table (split for the
200-line cap, as m2_resume/m2_stub). Stdlib only.
"""
import json, sqlite3, tempfile
from pathlib import Path


def events(home, after=0):
    """The ledger's events after seq `after`, in seq order (a line a kill cut short is skipped)."""
    out = []
    for path in sorted((home / "events").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return sorted((e for e in out if e["seq"] > after), key=lambda e: e["seq"])


def find(ev, type, after=0, **match):
    return next((e for e in ev if e["seq"] > after and e["type"] == type
                 and all(e.get(k) == v for k, v in match.items())), None)


def chain(ev, *steps):
    """True when every (type, match) of `steps` is found in seq order, each after the one before."""
    at = 0
    for type, match in steps:
        e = find(ev, type, at, **match)
        if e is None:
            return False
        at = e["seq"]
    return True


def uses(ev, name):
    """(assistant event, tool_use block) for every call of tool `name`."""
    return [(e, b) for e in ev if e["type"] == "assistant" for b in e.get("blocks") or []
            if b["type"] == "tool_use" and b["name"] == name]


def no_400(ev):
    return not any(e["type"] == "error" and e.get("status") == 400 for e in ev)


def listed_first(output, seq):
    return output.startswith(f"{seq}  write_file  ")  # `pathfinder approvals` puts open gates first


def root_state(home):
    db = sqlite3.connect((home / "state.db").as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT state FROM clones WHERE id = 'root'").fetchone()
    finally:
        db.close()
    return row and row[0]


def check_turn(ev, c):
    read = find(ev, "result", tool="read_file", ok=True)
    n = read["seq"] if read else -1
    reopened = [find(ev, "result", e["seq"], call_id=b["id"], tool="read_result", ok=True) for e, b in uses(ev, "read_result")
                if e["seq"] > n and b["input"].get("handle") == f"r:{n}"]
    steps = [e for e in ev if e["type"] == "assistant"]
    final = [e["text"] for e in steps if e.get("final")]
    use = [(u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0)) for u in (e["usage"] for e in steps)]
    errors = [f"#{e['seq']} clone={e.get('clone') or '-'} origin={e.get('origin')} model={e.get('model')} "
              f"status={e.get('status')}: {e.get('message', '')[:160]!r}" for e in ev if e["type"] == "error"]
    c["notes"].append(f"step 1 cache (read, write) per request: {use}")
    return [("a read_file result r:N", read is not None), ("read_result on r:N, and its result", any(reopened)),
            ("the final text holds the id", bool(final) and c["want"] in final[-1]),
            ("say printed the id", c["want"] in c["out"]["say1"]),
            ("every assistant names the chain head", bool(steps) and all(e["model"] == c["root"] for e in steps)),
            ("no error event" + (f" (got {' | '.join(errors)})" if errors else ""), not errors),
            ("cache read > 0 from request 2", len(use) >= 2 and all(r > 0 for r, _ in use[1:])),
            ("request >= 3 reads more than request 1 wrote (history breakpoint)",
             len(use) >= 3 and all(r > use[0][1] for r, _ in use[2:]))]


def check_gate(ev, c):
    g = find(ev, "gate", tool="write_file")
    c["gate"] = g["seq"] if g else None
    return [("a write_file gate", g is not None),
            ("no result for the gated call", g is not None and find(ev, "result", call_id=g["call_id"]) is None),
            ("then turn_end answer in that turn", g is not None and chain(ev, ("gate", {"seq": g["seq"]}),
                                                                         ("turn_end", {"turn": g["turn"], "kind": "answer"}))),
            ("the root's row is parked", root_state(c["home"]) == "parked"),
            ("say printed the gate line", g is not None and f"pathfinder approve {g['seq']}" in c["out"]["say2"]),
            ("approvals lists it first by its seq", listed_first(c["out"]["approvals2"], c["gate"]))]


def check_restart(ev, c):
    n, answer = c["gate"], c["home"] / "workspace" / "answer.txt"
    k = sum(b["type"] == "thinking" for e in c["before"] if e["type"] == "assistant" for b in e.get("blocks") or [])
    final = [e["text"].strip() for e in ev if e["type"] == "assistant" and e.get("final")]
    c["notes"].append(f"thinking blocks before the restart: {k}")
    return [("approvals after the rebuild lists the same gate", listed_first(c["out"]["approvals3"], n)),
            ("approve printed the report turn", bool(final) and final[-1] in c["out"]["approve3"]),
            ("resume, result ok, report user, turn_start, assistant, turn_end answer",
             chain(ev, ("resume", {"gate": n, "decision": "approved"}), ("result", {"gate": n, "ok": True}),
                   ("user", {"origin": "system", "gate": [n]}), ("turn_start", {}), ("assistant", {}),
                   ("turn_end", {"kind": "answer"}))),
            ("answer.txt holds the id", answer.exists() and answer.read_text().strip() == c["want"]),
            ("no interrupted", find(ev, "interrupted") is None), ("no HTTP 400", no_400(ev)),
            ("thinking before the restart (k = 0: the strip is untested)", True if k else None)]  # None: UNTESTED


def check_interrupted(ev, c):
    killed = uses(ev, "run_command")
    t = killed[0][0]["turn"] if killed else None
    final = [e["text"] for e in ev if e["type"] == "assistant" and e.get("final")]
    c["notes"].append(f"step 4 reply (should say the outcome is unknown): {final[-1][:200] if final else None!r}")
    return [("a run_command call recorded before the kill", t is not None),
            ("restore: interrupted {restart}, turn_end interrupted; then the next say is answered",
             chain(ev, ("interrupted", {"why": "restart", "turn": t}), ("turn_end", {"kind": "interrupted", "turn": t}),
                   ("user", {}), ("turn_end", {"kind": "answer"}))),
            ("no HTTP 400", no_400(ev))]


def check_fallback(ev, c):
    return [("error (root ref, status 0), then an assistant from the fallback ref, turn_end answer",
             chain(ev, ("error", {"model": c["root"], "status": 0}), ("assistant", {"model": c["fallback"]}),
                   ("turn_end", {"kind": "answer"})))]


CHECKS = {1: check_turn, 2: check_gate, 3: check_restart, 4: check_interrupted, 5: check_fallback}


def dry_run():
    """The table rows of a passing run's ledger written by hand, read back through `events`; then of each step
    with its key events dropped (BREAK), which must FAIL."""
    root, fallback, want, ev, marks = "anthropic:fake-root", "ollama:fake-fallback", "page-0001", [], []

    def add(type, turn, **f):
        ev.append({"seq": len(ev) + 1, "type": type, "turn": turn, "session": "s1", "origin": "user", "clone": "root", **f})
        return ev[-1]["seq"]

    def turn(t, *steps, end="answer", **user):
        add("user", t, **user), add("turn_start", t)
        for blocks, usage, extra in steps:
            add("assistant", t, model=root, blocks=blocks, usage=usage, final=not extra, text=want)
            for e in extra:
                add(e.pop("type"), t, **e)
        if end:
            add("turn_end", t, kind=end)

    use = lambda i, name, **inp: {"type": "tool_use", "id": i, "name": name, "input": inp}
    marks.append(len(ev))
    n = len(ev) + 4
    turn("t1", ([{"type": "thinking"}, use("c1", "read_file")], {"cache_creation_input_tokens": 3000},
                [{"type": "result", "tool": "read_file", "call_id": "c1", "ok": True}]),
         ([use("c2", "read_result", handle=f"r:{n}")], {"cache_read_input_tokens": 3000},
          [{"type": "result", "tool": "read_result", "call_id": "c2", "ok": True}]),
         ([{"type": "text", "text": want}], {"cache_read_input_tokens": 3600}, []))
    marks.append(len(ev))
    g = len(ev) + 4
    turn("t2", ([use("c3", "write_file")], {}, [{"type": "gate", "tool": "write_file", "call_id": "c3"}]), ([], {}, []))
    marks.append(len(ev))
    add("resume", "t2", gate=g, decision="approved"), add("result", "t2", gate=g, ok=True, call_id="c3")
    turn("t3", ([], {}, []), origin="system", gate=[g])
    marks.append(len(ev))
    turn("t4", ([use("c4", "run_command")], {}, []), end="")
    add("interrupted", "t4", why="restart"), add("turn_end", "t4", kind="interrupted")
    turn("t5", ([], {}, []))
    marks.append(len(ev))
    add("user", "t6"), add("error", "t6", model=root, status=0)
    add("assistant", "t6", model=fallback, blocks=[], usage={}, final=True, text="hello"), add("turn_end", "t6", kind="answer")
    BREAK = {1: "result", 2: "gate", 3: "resume", 4: "interrupted", 5: "error"}
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "events").mkdir()
        (home / "events" / "2026-10-17.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in ev))
        (home / "workspace").mkdir()
        (home / "workspace" / "answer.txt").write_text(want + "\n")
        with sqlite3.connect(home / "state.db") as db:
            db.execute("CREATE TABLE clones (id TEXT PRIMARY KEY, state TEXT)")
            db.execute("INSERT INTO clones VALUES ('root', 'parked')")
        db.close()
        line = f"{g}  write_file  {{}}\n"
        out = {"say1": want, "say2": f"(run `pathfinder approve {g}` or `pathfinder deny {g}`)",
               "approvals2": line, "approvals3": line, "approve3": want + "\n"}
        rows, broken = [], []
        for step, check in CHECKS.items():
            window = events(home, marks[step - 1])[:marks[step] - marks[step - 1] if step < 5 else None]
            c = {"home": home, "want": want, "root": root, "fallback": fallback, "out": out, "notes": [], "gate": g,
                 "before": events(home)[:marks[2]]}
            rows.append((step, check(window, c)))
            dropped = [e for e in window if e["type"] != BREAK[step]]
            broken.append((f"{step} broken", [("fails without its " + BREAK[step], not all(ok for _, ok in check(dropped, c)))]))
    return rows + broken
