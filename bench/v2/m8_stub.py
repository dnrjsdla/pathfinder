"""m8_stub.py -- m8_fold.py's scorer and dry run (split for the 200-line cap, as m2_stub): handle coverage, the
verdicts and the pass rule, the questions-file check, and the dry run on a synthetic v1 session with the fake model.
No network, no key, no real ledger. Stdlib only.
"""
import asyncio, json, random, re, tempfile, uuid
from pathlib import Path

from m6_stub import FakeRouter, answered, said, text, use
from pathfinder import keychain, ledger

ID = re.compile(r"[0-9a-f]{8}-?(?:[0-9a-f]{4}-?){3}[0-9a-f]{12}", re.I)
MISS = "not in the record"
VERDICTS = ("correct", "miss", "factual error", "pending")


def coverage(events) -> tuple[int, int]:
    """(handles the folds covered, how many of them are missing from their fold's `summary.handles`): a fold
    covers every root result up to its `upto`, earlier folds' included."""
    results = [e["seq"] for e in events if e["type"] == "result" and e.get("clone") == "root"]
    covered = missing = 0
    for s in (e for e in events if e["type"] == "summary"):
        want = {f"r:{seq}" for seq in results if seq <= s["upto"]}
        covered, missing = covered + len(want), missing + len(want - set(s["handles"]))
    return covered, missing


def verdict(q, answer) -> str:
    low, key = answer.casefold(), (q.get("key") or "").casefold()
    others = [m for m in ID.findall(answer) if m.casefold().replace("-", "") != key.replace("-", "")]
    if q.get("exact") and key and key in low:
        return "pending" if others else "correct"  # the key beside another ID: the lead decides
    if MISS in low:
        return "correct" if q.get("trap") else "miss"
    return "factual error" if q.get("exact") and others else "pending"


def finish(out) -> dict:
    """Pass: folds happened, every folded handle kept, at least one read_result on a folded handle and every one
    ok, no factual error, none pending."""
    out["counts"] = {v: sum(q["verdict"] == v for q in out["questions"]) for v in VERDICTS}
    rr = out["read_result_on_folded"]
    out["pass"] = (out["folds"] > 0 and out["missing_handles"] == 0 and rr["calls"] >= 1 and rr["ok"] == rr["calls"]
                   and out["counts"]["factual error"] == 0 and out["counts"]["pending"] == 0)
    return out


def rescore(out, verdicts) -> dict:
    for q in out["questions"]:
        if q["id"] in verdicts:
            assert verdicts[q["id"]] in VERDICTS[:3], verdicts[q["id"]]
            q["verdict"], q["by"] = verdicts[q["id"]], "lead"
    return finish(out)


def check(questions, live) -> None:
    n = lambda key: sum(bool(q.get(key)) for q in questions)  # noqa: E731
    assert all(q.get("id") and q.get("question") and (q.get("key") or q.get("trap")) for q in questions), \
        "each question needs id, question and a key (or trap)"
    assert not live or (len(questions) == 10 and n("first_third") >= 4 and n("handle") >= 3 and n("trap") >= 1), \
        "10 questions: >= 4 first_third, >= 3 handle, >= 1 trap"
    # a user turn's text goes through the ledger's Keychain vault: a token-shaped question would be stored there
    assert not any(keychain.TOKEN_RE.search(q["question"]) for q in questions), "a token-shaped string in a question"


def fake_world(tmp: Path) -> tuple[Path, list]:
    """A synthetic v1 session: 30 lookup turns (bodies past v1's clip, long answers) and one signal turn (no `user`:
    skipped); and five questions, one per verdict path."""
    rng, rows = random.Random(0), []
    pages = [{"id": str(uuid.UUID(int=rng.getrandbits(128), version=4)), "title": f"Page {i}"} for i in range(30)]

    def add(type_, turn, **fields):
        rows.append({"seq": len(rows) + 1, "ts": 1.76e9 + len(rows), "type": type_, "session": "s-fake",
                     "turn": turn, "origin": "signal" if turn == "t-signal" else "user", **fields})

    for i, page in enumerate(pages):
        body = json.dumps([page] + [{"id": "x" * 32, "title": "filler"}] * 40)
        add("user", f"t{i}", text=f"Find page {i} please.")
        add("turn_start", f"t{i}")
        add("tool_call", f"t{i}", id=f"c{i}", name="search", args={"query": f"Page {i}"})
        add("tool_result", f"t{i}", id=f"c{i}", name="search", ok=True, result=body[:2000] + "...(truncated)")
        add("assistant", f"t{i}", final=True, text=f"Page {i} found: see the result. " + "More detail. " * 70)
        add("turn_end", f"t{i}", kind="answer")
    for type_ in ("turn_start", "assistant", "turn_end"):
        add(type_, "t-signal", final=True, text="A signal turn.", kind="answer")
    (tmp / "events").mkdir()
    (tmp / "events" / "2026-10-14.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    other = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    qs = [{"id": "q1", "question": f"[fake: It is {pages[1]['id']}.] Page 1's id?", "key": pages[1]["id"],
           "exact": True},
          {"id": "q2", "question": f"[fake: {MISS}] Page 2's id?", "key": pages[2]["id"], "exact": True},
          {"id": "q3", "question": f"[fake: It is {other}.] Page 3's id?", "key": pages[3]["id"], "exact": True},
          {"id": "q4", "question": f"[fake: {MISS}] The trap?", "key": "", "trap": True},
          {"id": "q5", "question": "[fake: Something vague.] How did it go?", "key": "fine"},
          {"id": "q6", "question": f"[fake: {pages[4]['id']} or {other}.] Page 4's id?", "key": pages[4]["id"],
           "exact": True}]
    return tmp / "events", qs


def ask_rule(req):
    """A question's first step opens the first handle the record shows; then it answers what `[fake: ...]` says."""
    if not answered(req) and (handles := re.findall(r"r:\d+", said(req))):
        return [use("read_result", handle=handles[0])]
    return [text(re.findall(r"\[fake: ([^\]]*)\]", said(req))[-1])]


def dry_run(a) -> int:
    from m8_fold import measure  # here, not at the top: m8_fold imports this module
    refs, a.session = {"root": "fake:fake-model", "summarize": "fake:fake-model"}, "s-fake"
    with tempfile.TemporaryDirectory(prefix="pf-m8-") as tmp:
        events_dir, qs = fake_world(Path(tmp))
        check(qs, live=False)
        out, answers = asyncio.run(measure(a, refs, events_dir, qs, Path(tmp), FakeRouter(refs, ask_rule)))
        ev = list(ledger.Ledger(Path(tmp) / "replay").read())
    doctored = [dict(e, handles=e["handles"][1:]) if e["type"] == "summary" else e for e in ev]
    blob, rr = json.dumps(out, ensure_ascii=False), out["read_result_on_folded"]
    assert out["counts"] == {"correct": 2, "miss": 1, "factual error": 1, "pending": 2}, out["counts"]
    assert finish({**out, "read_result_on_folded": {"calls": 0, "ok": 0}, "questions": []})["pass"] is False
    assert out["folds"] >= 1 and out["covered_handles"] > 0 and out["missing_handles"] == 0, out
    assert out["turns"] == 30 and out["skipped_turns"] == 1 and out["results"] == 30, out
    assert coverage(doctored)[1] > 0 and rr["calls"] >= 1 and rr["ok"] == rr["calls"] and not out["pass"]
    assert not any(t in blob for q in qs for t in (q["question"], answers[q["id"]]["answer"])), "content in the output"
    assert rescore(out, {"q5": "correct", "q6": "correct"})["counts"]["pending"] == 0 and not out["pass"]
    assert rescore(out, {"q3": "correct"})["pass"]
    print(json.dumps({k: out[k] for k in ("turns", "skipped_turns", "results", "folds", "covered_handles",
                                          "missing_handles", "read_result_on_folded", "requests")}))
    print("dry run: PASS (conversion, folds, handle coverage, read_result, the three verdicts, rescore, no content)")
    return 0
