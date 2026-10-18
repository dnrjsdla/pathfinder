#!/usr/bin/env python3
"""m8_fold.py -- v2 M8 (docs/plans/v2-partition-1.md, "Measurements"): the fold keeps facts.

Live:    <venv>/bin/python bench/v2/m8_fold.py --ledger <home>/events --session <id> --questions <file>
             --root-model P:M [--summarize-model P:M] [--effort E] [--answers <file>]
Rescore: python3 bench/v2/m8_fold.py --rescore <output json> --verdicts <file>   (the lead's verdicts, by question id)
Dry run: python3 bench/v2/m8_fold.py --dry-run   (a synthetic v1 session and a fake router: no network, no key)

The v1 ledger is only read. The session's turns that open with a `user` event are recorded as v2 root events in a
temp home (`tool_call` -> an assistant step with one tool_use; `tool_result` -> `result` whose body is v1's 2,000-char
clip, flagged `v1_clip`), each turn followed by the real fold (`Clone._fold`, role summarize). Each question then runs
on its own copy of the replayed home as a root user turn with read_result as the only tool. Verdicts by key: an exact
key (an ID or title) in the answer -> correct; "not in the record" -> miss (on a trap: correct); an exact key's answer
naming another ID -> factual error; anything else -> pending, for the lead (--rescore). Pass: folds happened, 0 factual
errors, none pending, every handle a fold covered is in its `summary.handles`, every read_result on a folded handle ok.
The output (docs/measurements/v2-m8-<model>-<date>.json) holds counts, question ids and verdicts only; the questions'
text and the answers go to --answers, outside any repository (default: next to the questions file).
Questions file: JSON lines {"id", "question", "key", "exact", "first_third", "handle", "trap"}.
"""
import argparse, asyncio, datetime, json, os, shutil, sys, tempfile
from pathlib import Path
from unittest import mock

import m6_stub  # next to this file; puts this tree's pathfinder on sys.path
from m6_stub import Home, requests, text
from m8_stub import MISS, check, coverage, dry_run, finish, rescore, verdict
from pathfinder import assemble, clock, ledger, tools
from pathfinder.clone import Item

ASK = ("[bench] Answer from this conversation's record only; open a stored result with read_result when the record "
       f"points to one. If the answer is not there, say exactly: {MISS}.\n\n")


def outside_repos(path: Path) -> Path:
    path = path.resolve()
    if any((p / ".git").exists() for p in path.parents):
        sys.exit(f"{path} is inside a repository: questions and answers stay outside it")
    return path


def v1_turns(events_dir, session) -> tuple[list, int]:
    """The session's v1 turns (read-only, seq order) that hold a `user` and a `turn_end`, and how many others."""
    by = {}
    for path in sorted(Path(events_dir).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("session") == session and e.get("turn") and "clone" not in e:
                by.setdefault(e["turn"], []).append(e)
    turns = sorted((sorted(t, key=lambda e: e["seq"]) for t in by.values()), key=lambda t: t[0]["seq"])
    keep = [t for t in turns if {"user", "turn_end"} <= {e["type"] for e in t}]
    return keep, len(turns) - len(keep)


async def replay(home, root, turns, session) -> None:
    for turn in turns:
        ids, step = {}, 0
        for e in turn:
            ev = {"session": session, "turn": e["turn"], "clone": "root",
                  "origin": e.get("origin") if e.get("origin") in ledger.ORIGINS else "system"}
            if e["type"] == "user":  # third_party: masked like any recorded text, never vaulted again
                home.record("user", **ev, trust="third_party", text=e.get("text", ""))
            elif e["type"] == "turn_start":
                home.record("turn_start", **ev, item="user", resident=clock.now_line(e["ts"]))
            elif e["type"] == "tool_call":
                step += 1
                ids[e.get("id")] = call = f"v1_{e['seq']}"
                args = e["args"] if isinstance(e.get("args"), dict) else {"clipped": e.get("args")}
                block = {"type": "tool_use", "id": call, "name": e.get("name", ""), "input": args}
                home.record("assistant", **ev, step=step, model="v1", stop_reason="tool_use", usage={}, text="",
                            final=False, blocks=[block])
            elif e["type"] == "tool_result" and e.get("id") in ids:
                home.record("result", **ev, trust="third_party", step=step, call_id=ids.pop(e["id"]),
                            tool=e.get("name", ""), ok=bool(e.get("ok")), body=e.get("result"), v1_clip=True)
            elif e["type"] == "assistant" and e.get("final") and not e.get("withdrawn") and (e.get("text") or "").strip():
                step += 1  # a withdrawn draft (D51) and a blank text never reached history
                home.record("assistant", **ev, step=step, model="v1", stop_reason="end_turn", usage={},
                            text=e["text"], final=True, blocks=[text(e["text"])])
            elif e["type"] == "turn_end":
                home.record("turn_end", **ev, kind=e.get("kind") or "answer", steps=step, usage={})
        await root._fold(session)  # the real fold, as Clone.run calls it after each item


async def ask(base: Path, q, session, router, folded) -> tuple[str, list, list]:
    """One question on its own copy of the replayed home: (answer, [ok of each read_result on a folded handle],
    the requests' usage)."""
    home = Home(shutil.copytree(base, base.with_name(f"q-{q['id']}")), router)

    async def resident():
        return [clock.now_line()]

    root, mark = home.clone("root", "root", resident), home.ledger.last_seq
    with mock.patch.dict(tools.TOOLS, {}, clear=True):  # read_result is the only tool (the assembler adds it)
        await root.run(Item("user", ASK + q["question"], session))
    ev = home.events(mark)
    home.state.close()
    calls = {b["id"]: b["input"].get("handle") for e in ev if e["type"] == "assistant" for b in e["blocks"]
             if b["type"] == "tool_use" and b["name"] == "read_result"}
    oks = [e["ok"] for e in ev if e["type"] == "result" and calls.get(e["call_id"]) in folded]
    answer = next((e["text"] for e in reversed(ev) if e["type"] == "assistant" and e.get("final")), "")
    return answer, oks, requests(ev)


async def measure(a, refs, events_dir, questions, tmp: Path, router=None) -> tuple[dict, dict]:
    base = tmp / "replay"
    base.mkdir()
    m6_stub.configure(base, refs, a.effort)
    home = Home(base, router)
    turns, skipped = v1_turns(events_dir, a.session)
    await replay(home, home.clone("root", "root", None), turns, a.session)
    ev = home.events()
    home.state.close()
    covered, missing = coverage(ev)
    folded = {h for e in ev if e["type"] == "summary" for h in e["handles"]}
    use, oks, answers, rows = requests(ev), [], {}, []
    for q in questions:
        answer, ok, spent = await ask(base, q, a.session, router, folded)
        oks, use = oks + ok, use + spent
        answers[q["id"]] = {"question": q["question"], "answer": answer}
        rows.append({"id": q["id"], "verdict": verdict(q, answer), "read_result_on_folded": len(ok)})
        print(f"--- {q['id']} [{rows[-1]['verdict']}]\n{answer}\n")
    totals = dict(zip(m6_stub.USAGE, map(sum, zip(*use)))) if use else {}
    return finish({"measurement": "v2-m8", "models": refs, "effort": a.effort or "api default", "date": a.date,
                   "turns": len(turns), "skipped_turns": skipped, "results": sum(e["type"] == "result" for e in ev),
                   "folds": sum(e["type"] == "summary" for e in ev), "covered_handles": covered,
                   "missing_handles": missing, "read_result_on_folded": {"calls": len(oks), "ok": sum(oks)},
                   "requests": len(use), "usage": totals, "questions": rows}), answers


def main():
    ap = argparse.ArgumentParser(description="v2 M8: the fold keeps facts")
    ap.add_argument("--dry-run", action="store_true", help="a synthetic session, fake router; checks the scoring")
    ap.add_argument("--ledger", help="the v1 ledger's events directory (<home>/events); only read")
    ap.add_argument("--session", help="the v1 session id to replay")
    ap.add_argument("--questions", help="JSON lines, outside any repository")
    ap.add_argument("--answers", help="questions' text and answers, outside any repository (default: next to them)")
    ap.add_argument("--root-model", default="", help="provider:model (the provider is configured as api anthropic)")
    ap.add_argument("--summarize-model", default="", help="provider:model; default: the root's")
    ap.add_argument("--effort", default="", choices=["", "low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--out", default=str(m6_stub.ROOT / "docs" / "measurements"))
    ap.add_argument("--history-budget", type=int, default=0, help="chars of window before a fold; 0 = the product's "
                    "(a v2 window of handles is small: a v1 session may need a lower one to fold at all)")
    ap.add_argument("--rescore", help="an output JSON to rescore in place with --verdicts")
    ap.add_argument("--verdicts", help='JSON {"<question id>": "correct" | "miss" | "factual error"}')
    a = ap.parse_args()
    if a.rescore:
        path = Path(a.rescore)
        out = rescore(json.loads(path.read_text()), json.loads(Path(a.verdicts).read_text()))
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
        print(out["counts"], "pass", out["pass"])
        return 0 if out["pass"] else 1
    if a.dry_run:
        return dry_run(a)
    if not (a.ledger and a.session and a.questions and ":" in a.root_model):
        ap.error("--ledger, --session, --questions and --root-model provider:model are required")
    qpath = outside_repos(Path(a.questions))
    questions = [json.loads(line) for line in qpath.read_text(encoding="utf-8").splitlines() if line.strip()]
    check(questions, live=True)
    answers_path = outside_repos(Path(a.answers) if a.answers else qpath.with_suffix(".answers.json"))
    m6_stub.key()
    refs = {"root": a.root_model, "summarize": a.summarize_model or a.root_model}
    budget = a.history_budget or assemble.HISTORY_BUDGET
    with tempfile.TemporaryDirectory(prefix="pf-m8-") as tmp, mock.patch.object(assemble, "HISTORY_BUDGET", budget):
        out, answers = asyncio.run(measure(a, refs, Path(a.ledger), questions, Path(tmp)))
    out["history_budget"] = budget
    with os.fdopen(os.open(answers_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(json.dumps(answers, ensure_ascii=False, indent=1) + "\n")  # private from its first byte
    model = refs["root"].partition(":")[2].replace("/", "-").replace(":", "-")
    path = Path(a.out) / f"v2-m8-{model}-{a.date}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{out['counts']}  folds {out['folds']}  missing handles {out['missing_handles']}  read_result on folded "
          f"{out['read_result_on_folded']}  pass {out['pass']}\n-> {path}\nanswers -> {answers_path}")
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
