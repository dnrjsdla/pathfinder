"""
m5_embed.py -- M5 follow-up helpers: Korean-FTS diagnosis (no network) and the local embedding arms.
Imported by m5_tool_search.py. Stdlib only. Embeddings come from the local Ollama /v1/embeddings route.
"""
import json
import math
import re
import sqlite3
import statistics
import time
import urllib.request

EMBED_URL = "http://localhost:11434/v1/embeddings"
HANGUL = re.compile(r"[ㄱ-ㆎ가-힣]")


def embed_one(model, text):
    """-> (vector, seconds). One text per call so per-query latency is measurable."""
    req = urllib.request.Request(EMBED_URL, data=json.dumps({"model": model, "input": text}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=40) as r:
        vec = json.loads(r.read())["data"][0]["embedding"]
    return vec, time.perf_counter() - t0


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def rrf(rankings, k=60, top=3):
    """Reciprocal-rank fusion of several ranked name lists -> top names."""
    score = {}
    for names in rankings:
        for i, n in enumerate(names, 1):
            score[n] = score.get(n, 0) + 1 / (k + i)
    return sorted(score, key=lambda n: -score[n])[:top]


def add_arms(rows, texts, model, cell):
    """Adds arms 'embed' and 'hybrid' to every row. texts: {tool name: search_text}. cell(top, expected) -> arm dict.
    Returns the median embedding latency per query in ms."""
    names = list(texts)
    tvec = {n: embed_one(model, texts[n])[0] for n in names}  # 33 calls, cached for the run
    for row in rows:
        qv, secs = embed_one(model, row["query"])
        top = sorted(names, key=lambda n: -cosine(qv, tvec[n]))[:3]
        exp = set(row["expected"])
        row["arms"]["embed"] = dict(cell(top, exp), embed_ms=round(secs * 1000, 1))
        row["arms"]["hybrid"] = cell(rrf([row["arms"]["unicode61"]["top3"], top]), exp)
    return statistics.median(r["arms"]["embed"]["embed_ms"] for r in rows)


def verdict_embed(summary, model, embed_ms):
    b = max(("embed", "hybrid"), key=lambda a: (summary[a]["all"]["top3"], -summary[a]["block_chars"]["max"]))
    return {"embed_model": model, "best_arm": b, "top3": f"{summary[b]['all']['top3']}/20",
            "top3_ko": f"{summary[b]['ko']['top3']}/10", "top3_en": f"{summary[b]['en']['top3']}/10",
            "max_block_chars": summary[b]["block_chars"]["max"], "pass": summary[b]["all"]["top3"] >= 18,
            "embed_ms_median_per_query": embed_ms,
            "rule": "best of {embed, hybrid} top-3 >= 18/20; block size reported, not judged (set by Notion spec size)"}


def _tokens(tokenizer, text):
    """Tokens the FTS5 tokenizer produces for text, read back through fts5vocab."""
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE VIRTUAL TABLE p USING fts5(b, tokenize='{tokenizer}')")
    db.execute("INSERT INTO p VALUES (?)", (text,))
    db.execute("CREATE VIRTUAL TABLE v USING fts5vocab(p, 'row')")
    return [r[0] for r in db.execute("SELECT term FROM v ORDER BY term")]


def diagnosis(queries, texts):
    """Evidence for 'why Korean scores 0 in FTS'. No network. texts: {tool name: indexed search_text}."""
    ko = [q for lang, q, _ in queries if lang == "ko"]
    probe = ko[2]  # the "mail search" query
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE t USING fts5(name UNINDEXED, body, tokenize='unicode61')")
    db.executemany("INSERT INTO t VALUES (?, ?)", list(texts.items()))
    match = " OR ".join(f'"{w}"' for w in probe.split())
    hits = lambda: [r[0] for r in db.execute("SELECT name FROM t WHERE t MATCH ?", (match,))]
    before = hits()
    db.execute("UPDATE t SET body = body || ' ' || ? WHERE name = 'search_mail'", (probe.split()[0],))
    after = hits()
    per_tool = {n: len(HANGUL.findall(x)) for n, x in texts.items()}
    return {
        "hangul_chars_in_indexed_search_text": {"tools": len(texts), "total": sum(per_tool.values()),
                                                "tools_with_any": [n for n, c in per_tool.items() if c]},
        "hangul_chars_per_korean_query": {q: len(HANGUL.findall(q)) for q in ko},
        "tokenizer_split_of_query": {"query": probe, "unicode61": _tokens("unicode61", probe),
                                     "trigram": _tokens("trigram", probe)},
        "one_word_probe": {"query": probe, "added_word_to_search_mail": probe.split()[0],
                           "hits_before": before, "hits_after": after},
        "conclusion": "Lexical search cannot cross languages: the index holds no Korean text, so a Korean query "
                      "has nothing to match; the tokenizers split Hangul into sane words/trigrams, so they are not the cause.",
    }


def selfcheck():
    assert abs(cosine([1, 0], [1, 0]) - 1) < 1e-9 and abs(cosine([1, 0], [0, 1])) < 1e-9
    assert abs(cosine([1, 1], [2, 2]) - 1) < 1e-9
    assert rrf([["a", "b", "c"], ["c", "a", "d"]]) == ["a", "c", "b"]  # a: 1/61+1/62, c: 1/63+1/61
    assert rrf([[], ["x", "y"]]) == ["x", "y"] and rrf([["a"], ["b"]], top=1) == ["a"]
    d = diagnosis([("ko", "a", set()), ("ko", "b", set()), ("ko", "메일 검색", set())],
                  {"search_mail": "search mail", "pages": "page"})
    assert d["hangul_chars_in_indexed_search_text"]["total"] == 0 and d["one_word_probe"]["hits_before"] == []
    assert d["one_word_probe"]["hits_after"] == ["search_mail"]
    assert d["tokenizer_split_of_query"]["unicode61"] == ["검색", "메일"]
