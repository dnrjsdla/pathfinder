#!/usr/bin/env python3
"""m7a: what the free plan does with two overlapping streaming requests.

Each round: one solo request (baseline first-token latency), then two requests started
`--gap` seconds apart on the same model. For the overlapped pair we record whether the
second one waited for the first (queued), failed (429/402/other), or streamed alongside
(parallel). Decides how strict `llm/slot.py` must be (D25). Stdlib only, ~12 requests.
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pathfinder.llm import Endpoint, LLMError  # noqa: E402
from pathfinder.llm.openai_compat import stream  # noqa: E402

PROMPT = "Write about 150 words describing the ocean at dawn. Plain prose, no headings."


def one(endpoint, label, t0):
    rec = {"label": label, "start": time.monotonic() - t0, "first_token": None, "end": None,
           "status": "ok", "chars": 0, "usage": None}
    try:
        for kind, payload in stream(endpoint, [{"role": "user", "content": PROMPT}]):
            if kind in ("text", "reasoning"):
                if rec["first_token"] is None:
                    rec["first_token"] = time.monotonic() - t0
                if kind == "text":
                    rec["chars"] += len(payload)
            elif kind == "usage":
                rec["usage"] = payload
    except LLMError as e:
        rec["status"] = f"http {e.status}: {e.message[:120]}"
    except Exception as e:  # noqa: BLE001 - a bench records, it does not crash
        rec["status"] = f"{type(e).__name__}: {e}"[:160]
    rec["end"] = time.monotonic() - t0
    return rec


def classify(a, b):
    """How the second request (b) fared against the first (a)."""
    if b["status"] != "ok":
        return "failed"
    if a["status"] != "ok" or b["first_token"] is None:
        return "unclear"
    # b streamed while a was still streaming -> both ran; b's first token after a ended -> queued
    return "parallel" if b["first_token"] < a["end"] - 0.2 else "queued"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-oss:120b-cloud")
    ap.add_argument("--base-url", default="http://localhost:11434/v1")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--gap", type=float, default=0.5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    endpoint = Endpoint(base_url=args.base_url, api_key="", model=args.model)
    rounds = []
    for r in range(args.rounds):
        t0 = time.monotonic()
        solo = one(endpoint, "solo", t0)
        time.sleep(1.0)
        t0 = time.monotonic()
        pair = [None, None]
        th = [threading.Thread(target=lambda i=i: pair.__setitem__(i, one(endpoint, f"pair{i}", t0)))
              for i in range(2)]
        th[0].start()
        time.sleep(args.gap)
        th[1].start()
        for t in th:
            t.join()
        verdict = classify(pair[0], pair[1])
        rounds.append({"solo": solo, "first": pair[0], "second": pair[1], "verdict": verdict})
        ft = lambda x: "-" if x["first_token"] is None else f"{x['first_token']:.2f}s"  # noqa: E731
        print(f"round {r + 1}: solo ft {ft(solo)} | first ft {ft(pair[0])} end {pair[0]['end']:.2f}s "
              f"| second ft {ft(pair[1])} end {pair[1]['end']:.2f}s [{pair[1]['status']}] -> {verdict}",
              flush=True)
    verdicts = [x["verdict"] for x in rounds]
    summary = {v: verdicts.count(v) for v in sorted(set(verdicts))}
    print("verdicts:", summary)
    out = args.out or f"docs/measurements/m7a-{args.model.replace(':', '-')}.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"model": args.model, "gap_s": args.gap, "prompt": PROMPT,
                                     "rounds": rounds, "summary": summary}, indent=1))
    print("saved", out)


if __name__ == "__main__":
    main()
