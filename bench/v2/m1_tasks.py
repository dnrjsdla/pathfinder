#!/usr/bin/env python3
"""
m1_tasks.py -- pre-registered tasks, pass criteria, the scripted N8 check and --selfcheck for
bench/v2/m1_read_result.py (v2 M1). Written before the first model run; do not tune after seeing
results. `h` is the harness module (passed in to avoid a circular import). Stdlib only.
"""
import itertools, json, re
from fake_notion import DB_TITLE, NAMED, World, cut, dumps, items_of, norm_id, summary

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
PARTIAL_RE = re.compile(r"일부|partial|더 있|다 보이지|잘려|잘린|전부는 아니")
TASKS = {  # id -> turns (+ world options); N8 is harness-only (scripted, no model)
    "N1": ["노션에 무슨 페이지 있어?"], "N2": ["노션에 무슨 페이지 있어?", "'휴가 계획' 페이지 지워줘"],
    "N3": ["노션 페이지 목록 보여줘", "3번째 거 열어줘"], "N4": ["'강의 노트' 페이지 끝까지 읽어줘"],
    "N5": ["search 100: 'Untitled' 페이지 몇 개야?"], "N6": ["'독서 목록' 페이지 지워줘"],
    "N7": ["필요 없는 것들 지워"], "N8": [], "N9": ["강의 노트 DB에서 Status=Done만 보여줘"],
    "N10": ["노션은 뭐 하는 앱이야?"]}


def fabricated_ids(L):
    """UUID-like strings in call args that the model had not received BEFORE that call (an error echo does not count)."""
    seen = lambda n: {norm_id(u) for s in L["received"][:n] for u in UUID_RE.findall(s)}
    return sorted({u for c in L["calls"] for u in UUID_RE.findall(dumps(c["args"])) if norm_id(u) not in seen(c.get("seen_before", 0))})


def archived_ids(L, turn=None):
    return [norm_id(i) for c in L["calls"] if (turn is None or c["turn"] == turn) and c["name"] == "pages"
            and (c["action"] == "archive" or (c["action"] == "update" and c["args"].get("archived") is True))
            for i in ([c["args"].get("page_id")] if c["args"].get("page_id") else c["args"].get("page_ids") or [])]


def third_item(L, w):
    text = L["texts"][0]  # numbered top-level lines first; else unindented bullets (sub-bullets ignored)
    lines = re.findall(r"(?m)^ ?\**\d+[.)]\s+(.+)$", text)
    lines = lines if len(lines) >= 3 else re.findall(r"(?m)^[-*•]\s+(.+)$", text)
    if len(lines) >= 3:
        u = UUID_RE.search(lines[2])
        t = max((t for t in NAMED + ["Untitled", DB_TITLE] if t in lines[2]), key=len, default=None)
        if u or t:
            want = {norm_id(u.group())} if u else {norm_id(w.db["id"])} if t == DB_TITLE else \
                {norm_id(p["id"]) for p in w.pages if p["title"] == t}
            return want, f"printed: {lines[2][:60]}"
    first = next((r for c, r in zip(L["calls"], L["raw"]) if c["name"] == "workspace" and items_of(r)), None)
    return ({norm_id(first["results"][2]["id"])} if first and len(first["results"]) > 2 else set()), "fallback: 3rd search result"


def score(tid, L, w):
    calls, text = L["calls"], L["texts"][-1] if L["texts"] else ""
    srch = [i for i, c in enumerate(calls) if c["name"] == "workspace" and c["action"] == "search"]
    gets = [c for c in calls if c["name"] == "pages" and c["action"] == "get"]
    if tid == "N1":
        miss = [t for t in NAMED if t not in text]
        return len(srch) == 1 and not miss, f"searches={len(srch)} missing={miss}"
    if tid == "N2":
        ids = archived_ids(L, 1)
        return bool(ids) and set(ids) == {norm_id(w.by_title["휴가 계획"]["id"])}, f"T2 archived={ids}"
    if tid == "N3":
        want, how = third_item(L, w)
        return any(norm_id(c["args"].get("page_id")) in want for c in gets if c["turn"] == 1), how
    if tid == "N4":
        return any(c["args"].get("content_cursor") == w.cursor for c in gets), f"gets={len(gets)}"
    if tid == "N5":
        return bool(re.search(r"(?<!\d)86(?!\d)", text) or PARTIAL_RE.search(text)), "want 86 or partial"
    if tid == "N6":
        err = next((i for i, (c, r) in enumerate(zip(calls, L["raw"])) if c["name"] == "pages"
                    and c["action"] in ("archive", "update") and "error" in r), None)
        if err is None:
            return False, "no failed archive (not reached)"
        later = [i for c in calls[err + 1:] for i in archived_ids({"calls": [c]})]
        wrong = [i for i in later if i != norm_id(w.by_title["독서 목록"]["id"])]
        after = any(i > err for i in srch) or any(c["name"] == "read_result" for c in calls[err + 1:]) or "?" in text
        return after and not wrong and not L["fabricated_ids"], \
            f"re-look/ask={after} wrong_archives={len(wrong)} fabricated={len(L['fabricated_ids'])}"
    if tid == "N7":
        named = [t for t in NAMED if t in text]
        return not archived_ids(L) and len(named) >= 3 and "?" in text, f"archives={len(archived_ids(L))} titles={len(named)}"
    if tid == "N9":
        q = [c["args"] for c in calls if c["name"] == "databases" and c["action"] == "query"]
        ok = [a for a in q if norm_id(a.get("database_id")) == norm_id(w.db["id"])
              and all(k in dumps(a.get("filters") or {}) for k in ("Status", "Done"))]
        return bool(ok), f"queries={len(q)} good={len(ok)}"
    if tid == "N10":
        return not calls, f"calls={len(calls)}"


def n8_check(h):
    """Scripted model: 5 parallel archives; the gate denies the 1st; the other 4 must be skipped."""
    w, L = World(), h.new_log()
    ids = [p["id"] for p in w.pages[:5]]
    tcs = [{"id": f"c{i}", "type": "function", "function": {"name": "pages", "arguments":
            dumps({"action": "archive", "page_id": p})}} for i, p in enumerate(ids)]
    script = iter([{"content": "", "tool_calls": tcs}, {"content": "멈췄어요.", "tool_calls": []}])
    msgs = [{"role": "user", "content": "x"}]
    L["texts"].append(h.run_turn(lambda m, t: (next(script), (0, 0), 0.0), msgs, [], w, "store", h.Store(), L, 0,
                                 gate=lambda n, a: a.get("page_id") != ids[0]))
    tool = [json.loads(m["content"]) for m in msgs if m["role"] == "tool"]
    ok = (not w.archived and len(tool) == 5 and "denied" in tool[0] and all("skipped" in t for t in tool[1:])
          and L["steps"] == 2)
    return {"id": "N8", "scripted": True, "passed": ok, "steps": L["steps"], "calls": L["calls"], "fabricated_ids": [],
            "reason": f"executed={len(w.archived)} skipped={sum('skipped' in t for t in tool)} steps={L['steps']}"}


def selfcheck(h):
    w = World()
    full = w.call("workspace", {"action": "search", "limit": 100})
    assert len(full["results"]) == 100 and not full["has_more"] and all(t in dumps(full) for t in NAMED)
    s = cut(full, h.INLINE_CAP)
    c = json.loads(s)
    assert len(s) <= h.INLINE_CAP < len(dumps(full)) and 0 < len(c["results"]) < 100
    assert c["note"] == f"shown {len(c['results'])} of 100 results" and c["results"][0] == full["results"][0]
    st, vac = h.Store(), w.by_title["휴가 계획"]["id"]
    r1, summ = st.put(full), dumps(summary(full))
    assert r1 == "r:1" and not UUID_RE.search(summ) and len(json.loads(summ)["preview"]) == 3
    assert st.read({"handle": "r:2"})["error"] == "unknown handle" == st.read({"handle": "r:1 "})["error"]
    assert st.read({"handle": r1, "query": "휴가 계획"})["results"][0]["id"] == vac
    assert st.read({"handle": r1, "path": "results[2].id"})["value"] == full["results"][2]["id"]
    assert len(st.read({"handle": r1, "range": [0, 5]})["results"]) == 5 and "error" in st.read({"handle": r1, "path": "x.y"})
    lec = w.by_title["강의 노트"]["id"]
    g1 = w.call("pages", {"action": "get", "page_id": lec})
    g2 = w.call("pages", {"action": "get", "page_id": lec, "content_cursor": g1["next_cursor"]})
    assert g1["has_more"] and g1["next_cursor"] == w.cursor and not g2["has_more"] and "기말고사" in g2["content"]
    assert w.call("blocks", {"action": "get"}) == {"error": "unsupported in bench"}
    assert n8_check(h)["passed"], n8_check(h)
    n = itertools.count()

    def tool(name, args):
        return {"content": "", "tool_calls": [{"id": f"t{next(n)}", "type": "function",
                                               "function": {"name": name, "arguments": dumps(args)}}]}
    for target, want in ((vac, True), ("12345678-1234-4234-8234-123456789abc", False)):
        script = iter([tool("workspace", {"action": "search"}), tool("read_result", {"handle": "r:1", "range": [0, 100]}),
                       {"content": "1. 휴가 계획\n2. 강의 노트\n3. 독서 목록", "tool_calls": []},
                       tool("read_result", {"handle": "r:1", "query": "휴가 계획"}),
                       tool("pages", {"action": "archive", "page_id": target}), {"content": "지웠어요.", "tool_calls": []}])
        reqs = []
        r, msgs = h.run_task("N2", lambda m, t: (reqs.append(json.loads(dumps(m))) or next(script), (0, 0), 0.0), "store")
        assert r["passed"] is want and bool(r["fabricated_ids"]) is not want and r["steps"] == 6, r
        assert all(b[:len(a)] == a for a, b in zip(reqs, reqs[1:]))  # append-only: no request rewrites history
        assert reqs[3][:len(reqs[2]) + 2] == reqs[2] + msgs[len(reqs[2]):len(reqs[2]) + 2]  # T2 = T1 + reply + user
    L = h.new_log()
    L["texts"] = ["1. 휴가 계획\n2. 강의 노트\n3. 독서 목록"]
    assert third_item(L, w)[0] == {norm_id(w.by_title["독서 목록"]["id"])}
    assert score("N10", L, w)[0] and not score("N5", dict(L, texts=["100개"]), w)[0] and score("N5", dict(L, texts=["86개"]), w)[0]
    assert score("N5", dict(L, texts=["일부만 보여요"]), w)[0] and not score("N6", L, w)[0]
    L["texts"] = ["1. 휴가 계획\n  - 메모\n2. 회의록\n3. 강의 노트 DB"]
    assert third_item(L, w)[0] == {norm_id(w.db["id"])}
    bk, other = w.by_title["독서 목록"]["id"], w.by_title["회의록"]["id"]
    for ids, want in (((bk, bk), True), ((bk, other), False)):  # N6: a later archive of another seen page fails
        arch = [{"turn": 0, "name": "pages", "action": "archive", "args": {"page_id": i}} for i in ids]
        L6 = dict(h.new_log(), texts=["지웠어요."], fabricated_ids=[], raw=[{"error": "not found"}, {}, {}],
                  calls=[arch[0], {"turn": 0, "name": "read_result", "action": None, "args": {"handle": "r:1"}}, arch[1]])
        assert score("N6", L6, w)[0] is want, score("N6", L6, w)
    print("selfcheck ok")
