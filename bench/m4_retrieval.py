#!/usr/bin/env python3
"""m4: does the hybrid index earn its vectors over FTS alone?

Builds a temp `state.db`, indexes the transplanted Mark.5 retrieval corpus
(Korean wiki pages, plus dated notes as distractor pages) through the real
page index, then runs FTS-only, vector-only and hybrid `state.search` for
each of the 16 `search-*` questions. Reports recall@3 per branch over the 12
positives, and the negatives' top vector-branch cosine plus whether FTS
returned anything. With no `--embed` the vector and hybrid branches are
"n/a" (D10's FTS-only fallback). Zero cloud credits with a local `--embed`
model (Ollama). Stdlib only.
"""
import argparse
import json
import math
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pathfinder import llm  # noqa: E402
from pathfinder.llm import openai_compat  # noqa: E402
from pathfinder.state import State  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "retrieval" / "corpus" / "wiki"
QUESTIONS = Path(__file__).resolve().parent / "retrieval" / "questions.json"


def load_corpus():
    pages = []
    for p in sorted(CORPUS.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), p.stem)
        pages.append({"path": f"wiki/{p.name}", "title": title, "text": text, "mtime": p.stat().st_mtime})
    return pages


def index_corpus(state, pages, endpoint, model):
    vectors = openai_compat.embed(endpoint, [p["text"] for p in pages]) if endpoint else None
    for i, p in enumerate(pages):
        vec = vectors[i] if vectors else None
        state.index_page(p["path"], p["title"], p["text"], p["mtime"], vector=vec, model=model)


def top_cosine(state, query_vector, model):
    """Best raw cosine among `model`'s stored vectors, for reporting a negative's top hit -
    the fused RRF score `search()` returns isn't the cosine itself."""
    qnorm = math.sqrt(sum(x * x for x in query_vector))
    if qnorm == 0:
        return None
    best = None
    for _path, blob in state.db.execute("SELECT path, vec FROM vectors WHERE model = ?", (model,)):
        vec = struct.unpack(f"<{len(blob) // 4}f", blob)
        vnorm = math.sqrt(sum(x * x for x in vec))
        if vnorm == 0:
            continue
        cos = sum(a * b for a, b in zip(query_vector, vec)) / (qnorm * vnorm)
        if best is None or cos > best:
            best = cos
    return best


def run_questions(state, questions, endpoint, model, top_k):
    qvecs = {}
    if endpoint:
        embedded = openai_compat.embed(endpoint, [q["query"] for q in questions])
        qvecs = {q["id"]: v for q, v in zip(questions, embedded)}
    results = []
    for q in questions:
        qvec = qvecs.get(q["id"])
        fts_hits = [h["path"] for h in state.search(q["query"], top_k=top_k)]
        vector_hits = [h["path"] for h in state.search(q["query"], query_vector=qvec, model=model,
                                                        top_k=top_k, fts=False)] if qvec else []
        hybrid_hits = [h["path"] for h in state.search(q["query"], query_vector=qvec, model=model,
                                                        top_k=top_k)] if qvec else fts_hits
        entry = {"id": q["id"], "query": q["query"], "expect": q["expect"],
                 "fts": fts_hits, "vector": vector_hits, "hybrid": hybrid_hits}
        if not q["expect"]:
            entry["top_cosine"] = top_cosine(state, qvec, model) if qvec else None
        results.append(entry)
    return results


def recall_at_3(results, branch):
    positives = [r for r in results if r["expect"]]
    if not positives:
        return None
    hits = sum(1 for r in positives if any(p in r[branch] for p in r["expect"]))
    return hits / len(positives)


def print_report(results, embed, verbose):
    positives = [r for r in results if r["expect"]]
    negatives = [r for r in results if not r["expect"]]
    print(f"recall@3 over {len(positives)} positives:")
    print(f"  fts:    {recall_at_3(results, 'fts'):.2f}")
    if embed:
        print(f"  vector: {recall_at_3(results, 'vector'):.2f}")
        print(f"  hybrid: {recall_at_3(results, 'hybrid'):.2f}")
    else:
        print("  vector: n/a (no --embed)")
        print("  hybrid: n/a (no --embed)")
    if verbose:
        for r in positives:
            print(f"  {r['id']}: expect={r['expect']} fts={r['fts']} vector={r['vector']} hybrid={r['hybrid']}")
    print("negatives (top hit under the floor, not gated):")
    for r in negatives:
        cos = r.get("top_cosine")
        print(f"  {r['id']}: fts_hits={bool(r['fts'])} top_cosine={'n/a' if cos is None else f'{cos:.3f}'}")


def selfcheck():
    """Bench logic on two tiny in-memory pages, no network: recall and RRF merge."""
    with tempfile.TemporaryDirectory() as d:
        state = State(Path(d) / "s.db")
        # FTS matches page A only; the vector branch ranks page B above A.
        state.index_page("wiki/a.md", "A", "사과 바나나", 1.0, vector=[0.0, 1.0, 0.0], model="m")
        state.index_page("wiki/b.md", "B", "포도 딸기", 2.0, vector=[1.0, 0.0, 0.0], model="m")
        qvec = [1.0, 0.0, 0.0]
        fts_hits = [h["path"] for h in state.search("사과", top_k=3)]
        vector_hits = [h["path"] for h in state.search("사과", query_vector=qvec, model="m", top_k=3, fts=False)]
        hybrid_hits = [h["path"] for h in state.search("사과", query_vector=qvec, model="m", top_k=3)]
        assert fts_hits == ["wiki/a.md"], fts_hits
        assert vector_hits[0] == "wiki/b.md", vector_hits
        # A appears in both branches, B in vector alone - the dual-branch page wins the merge.
        assert hybrid_hits[0] == "wiki/a.md" and set(hybrid_hits) == {"wiki/a.md", "wiki/b.md"}, hybrid_hits
        results = [{"id": "q1", "expect": ["wiki/a.md"], "fts": fts_hits, "hybrid": hybrid_hits}]
        assert recall_at_3(results, "fts") == 1.0
        assert recall_at_3(results, "hybrid") == 1.0
        state.close()
    print("selfcheck ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed", default=None, help="provider:model, e.g. ollama:bge-m3 (omit for FTS-only)")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--out", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()

    endpoint = None
    if args.embed:
        cfg = {"providers": {"ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": ""}}}
        endpoint = llm.resolve(cfg, args.embed)

    pages = load_corpus()
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]

    with tempfile.TemporaryDirectory() as d:
        state = State(Path(d) / "state.db")
        index_corpus(state, pages, endpoint, args.embed or "")
        results = run_questions(state, questions, endpoint, args.embed or "", args.top_k)
        state.close()

    print_report(results, args.embed, args.verbose)

    if args.out:
        out = args.out
    elif args.embed:
        out = f"docs/measurements/m4-{args.embed.replace(':', '-').replace('/', '-')}.json"
    else:
        out = "docs/measurements/m4-fts-only.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"embed": args.embed, "top_k": args.top_k, "ts": time.time(),
                                     "results": results}, indent=1, ensure_ascii=False))
    print("saved", out)


if __name__ == "__main__":
    main()
