#!/usr/bin/env python3
"""
m5_tool_search.py -- measurement M5 (blueprint s13): does harness-side FTS5 tool search find the
right tool among 33 (17 native + 5 memory + 11 Notion), Korean and English queries?
No model calls. Stdlib only. Queries, aliases and pass line are pre-registered below (fixed before the first run).
Pass line: top-3 >= 18/20 AND max tool block <= 8000 chars, in the best FTS-only arm (unicode61 / trigram).
The "alias" arm (unicode61 + hand Korean aliases) is shown for the gap only and never decides the verdict.

Embedding arms (follow-up; run only with --embed-model, local Ollama, no credits; code in m5_embed.py):
  embed  = cosine top-3 over embeddings of each tool's search_text; hybrid = RRF(k=60) of unicode61 top-3 + embed top-3.
  verdict_embed (pre-registered before the first embed run): pass iff the best of {embed, hybrid} has top-3 >= 18/20.
  The block-size criterion is reported for those arms too, but it is set by the Notion spec size, not the search method.
  The primary verdict stays FTS-only and unchanged.

Usage: python3 bench/v2/m5_tool_search.py [--out p] [--embed-model name]
       python3 bench/v2/m5_tool_search.py --selfcheck
"""
import argparse
import json
import re
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path

import m5_embed

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

# ---- pre-registered queries: (lang, text a model would pass to search_tools, expected tool names) ----
QUERIES = [
    ("en", "archive a notion page", {"pages"}),
    ("en", "query a notion database with a filter", {"databases"}),
    ("en", "search my email", {"search_mail"}),
    ("en", "run a shell command", {"run_command"}),
    ("en", "read a note in the vault", {"read_memory"}),
    ("en", "install an mcp server", {"propose_install"}),
    ("en", "add a daily schedule", {"add_schedule"}),
    ("en", "upload a file to notion", {"file_uploads"}),
    ("en", "mark a reminder as done", {"complete_reminder"}),
    ("en", "add a comment to a notion page", {"comments"}),
    ("ko", "노션 페이지 보관", {"pages"}),
    ("ko", "내일 일정 추가", {"add_event"}),
    ("ko", "메일 검색", {"search_mail"}),
    ("ko", "셸 명령 실행", {"run_command"}),
    ("ko", "메모리에서 기억 찾기", {"search_memory"}),
    ("ko", "미리 알림 목록 보기", {"list_reminders"}),
    ("ko", "노션 데이터베이스 조회", {"databases"}),
    ("ko", "파일 쓰기", {"write_file"}),
    ("ko", "매일 아침 알림 예약", {"add_schedule", "add_reminder"}),
    ("ko", "노션에 댓글 달기", {"comments"}),
]

# hand aliases for the "alias" arm only (<= 3 Korean words per tool). Written with the queries in
# view, so several aliases contain the query text verbatim: an optimistic upper bound, never the verdict.
ALIASES = {
    "run_command": "셸 명령 실행", "read_file": "파일 읽기", "write_file": "파일 쓰기",
    "list_events": "일정 조회 캘린더", "add_event": "일정 추가 캘린더", "search_tools": "도구 검색 서버",
    "propose_install": "설치 서버 도구", "list_mail": "메일 목록", "search_mail": "메일 검색",
    "read_mail": "메일 읽기", "list_reminders": "미리 알림 목록", "add_reminder": "미리 알림 추가",
    "complete_reminder": "미리 알림 완료", "delete_reminder": "미리 알림 삭제", "add_schedule": "예약 알림 반복",
    "list_schedules": "예약 목록", "remove_schedule": "예약 삭제", "search_memory": "기억 메모리 검색",
    "read_memory": "기억 메모리 읽기", "edit_memory": "기억 메모리 수정", "read_ledger": "기록 원장 읽기",
    "read_skill": "스킬 절차 읽기", "pages": "노션 페이지 보관", "databases": "노션 데이터베이스 조회",
    "blocks": "노션 블록", "users": "노션 사용자", "workspace": "노션 워크스페이스 검색",
    "comments": "노션 댓글", "content_convert": "마크다운 변환", "file_uploads": "노션 파일 업로드",
    "help": "노션 도움말", "config": "노션 설정", "config__open_relay": "노션 릴레이 열기",
}

# 5 core tools always in the block (~400 chars each, mock)
_PAD = (" Use it whenever the user's request needs it; arguments are validated by the harness and the"
        " result comes back as text. Prefer the narrowest call that answers the question, and do not repeat"
        " a call whose result you already have in this conversation.")
def _stub(name, desc, prop):
    return {"type": "function", "function": {"name": name, "description": desc + _PAD, "parameters": {
        "type": "object", "properties": {prop: {"type": "string", "description": "the " + prop}}, "required": [prop]}}}
CORE = [_stub("read_result", "Read a stored tool result by its r:N handle.", "handle"),
        _stub("search_tools", "Search the available tools by what they do; returns their full specs.", "query"),
        _stub("search_memory", "Search the memory vault for pages matching a query.", "query"),
        _stub("read_memory", "Read one file of the memory vault.", "path"),
        _stub("note_self", "Leave a note for yourself that survives the context window.", "text")]


def load_corpus():
    """-> list of (openai_spec, normalized) for the 33 tools."""
    fx = HERE / "fixtures"
    specs = json.loads((fx / "native_tools.json").read_text()) + json.loads((fx / "memory_tools.json").read_text())
    for t in json.loads((fx / "notion_tools.json").read_text()):  # MCP shape -> OpenAI function shape
        specs.append({"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                      "parameters": t["inputSchema"]}})
    return [(s, normalize(s["function"])) for s in specs]


def normalize(fn):
    params = []
    for pn, pv in fn["parameters"].get("properties", {}).items():
        params.append((pn, pv.get("description", ""), pv.get("enum", [])))
        for sn, sv in pv.get("properties", {}).items():  # nested properties, one level
            params.append((f"{pn}.{sn}", sv.get("description", ""), sv.get("enum", [])))
    return {"name": fn["name"], "description": fn["description"], "params": params}


def search_text(n, alias=""):
    parts = [n["name"], n["name"].replace("_", " "), n["description"]]
    parts += [f"{p} {d} {' '.join(map(str, e))}" for p, d, e in n["params"]]
    return "\n".join(parts + ([alias] if alias else []))


def build(corpus, tokenizer, aliases=False):
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE VIRTUAL TABLE t USING fts5(name UNINDEXED, body, tokenize='{tokenizer}')")
    db.executemany("INSERT INTO t VALUES (?, ?)",
                   [(n["name"], search_text(n, ALIASES[n["name"]] if aliases else "")) for _, n in corpus])
    return db


def search(db, query, trigram, k=3):
    toks = re.findall(r"\w+", query)
    toks = [w for w in toks if len(w) >= 3] if trigram else toks  # trigram cannot match < 3 chars
    if not toks:
        return []
    q = " OR ".join('"' + w.replace('"', '') + '"' for w in toks)
    return [r[0] for r in db.execute("SELECT name FROM t WHERE t MATCH ? ORDER BY bm25(t) LIMIT ?", (q, k))]


def run(embed_model=None):
    corpus = load_corpus()
    assert len(corpus) == 33, len(corpus)
    spec = {n["name"]: s for s, n in corpus}
    arms = {"unicode61": (build(corpus, "unicode61"), False),
            "trigram": (build(corpus, "trigram"), True),
            "alias": (build(corpus, "unicode61", aliases=True), False)}
    core_names = {c["function"]["name"] for c in CORE}

    def cell(top, exp):
        block = json.dumps(CORE + [spec[x] for x in top if x not in core_names], ensure_ascii=False)
        return {"top3": top, "top1_hit": bool(top) and top[0] in exp, "top3_hit": bool(set(top) & exp),
                "block_chars": len(block)}
    rows = [{"query": q, "lang": lang, "expected": sorted(exp),
             "arms": {arm: cell(search(db, q, tri), exp) for arm, (db, tri) in arms.items()}}
            for lang, q, exp in QUERIES]
    texts = {n["name"]: search_text(n) for _, n in corpus}
    embed_ms = m5_embed.add_arms(rows, texts, embed_model, cell) if embed_model else None
    summary = {}
    for arm in rows[0]["arms"]:
        summary[arm] = {}
        for lang in ("en", "ko", "all"):
            rs = [r["arms"][arm] for r in rows if lang in ("all", r["lang"])]
            summary[arm][lang] = {"n": len(rs), "top1": sum(x["top1_hit"] for x in rs), "top3": sum(x["top3_hit"] for x in rs)}
        bl = [r["arms"][arm]["block_chars"] for r in rows]
        summary[arm]["block_chars"] = {"max": max(bl), "median": statistics.median(bl)}
    fts = {a: summary[a] for a in ("unicode61", "trigram")}
    best = max(fts, key=lambda a: (fts[a]["all"]["top3"], -fts[a]["block_chars"]["max"]))
    ok = summary[best]["all"]["top3"] >= 18 and summary[best]["block_chars"]["max"] <= 8000
    verdict = {"best_fts_arm": best, "top3": f"{summary[best]['all']['top3']}/20",
               "top3_ko": f"{summary[best]['ko']['top3']}/10", "top3_en": f"{summary[best]['en']['top3']}/10",
               "max_block_chars": summary[best]["block_chars"]["max"], "pass": ok,
               "rule": "top-3 >= 18/20 and max block <= 8000 chars in the best FTS-only arm; alias arm excluded",
               "on_fail": "per-server allowlist only (blueprint s13)"}
    out = {"verdict": verdict}
    if embed_model:
        out["verdict_embed"] = m5_embed.verdict_embed(summary, embed_model, embed_ms)
    baseline = sum(len(json.dumps(s, ensure_ascii=False)) for s, _ in corpus)
    core = len(json.dumps(CORE, ensure_ascii=False))
    return {"measurement": "M5", "date": str(date.today()), "tools": 33, "all_specs_chars": baseline,
            "core_stub_chars": core, "summary": summary, **out,
            "diagnosis": m5_embed.diagnosis(QUERIES, texts), "rows": rows}


def selfcheck():
    corpus = load_corpus()
    assert len(corpus) == 33 and len(QUERIES) == 20
    assert sum(q[0] == "ko" for q in QUERIES) == 10 and set(ALIASES) == {n["name"] for _, n in corpus}
    assert all(400 <= len(json.dumps(c)) <= 560 for c in CORE), [len(json.dumps(c)) for c in CORE]
    db = build(corpus, "unicode61")
    assert "run_command" in search(db, "run a shell command", False)
    assert "pages" in search(build(corpus, "trigram"), "archive a notion page", True)
    m5_embed.selfcheck()
    print("selfcheck ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "measurements" / f"v2-m5-{date.today()}.json"))
    ap.add_argument("--embed-model", default=None, help="Ollama embedding model; omit for FTS-only")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    res = run(a.embed_model)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps({k: res[k] for k in ("all_specs_chars", "summary", "verdict", "verdict_embed", "diagnosis") if k in res},
                     ensure_ascii=False, indent=1))
    print("wrote", a.out)


if __name__ == "__main__":
    sys.exit(main())
