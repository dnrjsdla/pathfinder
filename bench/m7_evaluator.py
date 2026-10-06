#!/usr/bin/env python3
"""m7: does an evaluator pass earn its cost? (partition 4, D23)

Ten research-shaped goals, each run twice through the job loop against a mock world (8 canned
pages/files, read-only tools so nothing gates): condition A is a plain job run; condition B adds
one evaluator pass - a second, tool-less request that reads the goal and the summary, answers
closed JSON {"adequate": bool, "missing": "..."}; when not adequate the summary and the missing
note are fed back for one more round (never two). Twenty outputs saved blind (a random key hides
the condition) to docs/measurements/m7-<model>.json; --grade prints them shuffled for a
grader to vote thumbs up/down into a votes file; --score unblinds and reports the D23 verdict
(B needs >= 3 more up-votes than A out of 10 to earn its row).

Usage: python3 bench/m7_evaluator.py [--tasks N]
       python3 bench/m7_evaluator.py --grade
       python3 bench/m7_evaluator.py --score --votes docs/measurements/m7-votes.json
       python3 bench/m7_evaluator.py --selfcheck
"""
import argparse
import contextlib
import io
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathfinder import clock, loop  # noqa: E402
from pathfinder.broker import Broker  # noqa: E402
from pathfinder.llm import Endpoint, LLMError  # noqa: E402
from pathfinder.llm import openai_compat  # noqa: E402
from pathfinder.tools import ToolSpec  # noqa: E402

JOB_PROMPT = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "pathfinder", "prompts", "job.txt"), encoding="utf-8").read()

EVALUATOR_PROMPT = (
    "You judge whether a worker's summary actually answers its goal. Given the goal and the "
    "summary, answer with closed JSON only, no other text: "
    '{"adequate": true|false, "missing": "one sentence naming what is missing, or empty if adequate"}.'
)

# -- mock world: 8 canned pages/files, fictional content, example.com only ------------------
PAGES = {
    "https://example.com/pricing/acme": "Acme Cloud pricing: Basic $9/mo (10GB), Pro $29/mo (200GB), "
        "Enterprise: contact sales. Annual billing saves 15%.",
    "https://example.com/pricing/globex": "Globex Storage pricing: Starter $12/mo (15GB), Team $35/mo "
        "(250GB), no annual discount.",
    "https://example.com/status/acme": "Acme Cloud status: all systems operational. Last incident: "
        "2026-08-02, a 40-minute outage in the EU region.",
    "https://example.com/status/globex": "Globex Storage status: degraded performance in the US region "
        "since 2026-09-10, engineering is investigating.",
    "notes/vendors.md": "# Vendor shortlist\n- Acme Cloud: existing contract, EU-based\n- Globex "
        "Storage: cheaper Team tier, US-based\n- Initech Files: dropped, missed SLA twice",
    "notes/meeting-2026-09-15.md": "Standup notes 2026-09-15: storage costs are up 20% this quarter. "
        "Action: compare Acme and Globex pricing and outage history before renewal.",
    "notes/team-roster.md": "Team roster: Ann (lead), Bo (infra), Cy (support). On-call rotates weekly.",
    "notes/travel-policy.md": "Travel policy: book economy for trips under 6 hours; manager approval "
        "needed above $800.",
}
DIRS = {"notes": sorted(p for p in PAGES if p.startswith("notes/")),
        "notes/": sorted(p for p in PAGES if p.startswith("notes/"))}


def _read_file(path, control=None):
    if path not in PAGES:
        raise ValueError(f"no such file: {path}")
    return PAGES[path]


def _list_dir(path, control=None):
    return DIRS.get(path, [])


def _fetch(url, control=None):
    if url not in PAGES:
        raise ValueError(f"no such page: {url}")
    return PAGES[url]


MOCK_TOOLS = {
    "read_file": ToolSpec("read_file", "Read a text file.", {"path": {"type": "string", "description": "file path"}},
                          ["path"], _read_file),
    "list_dir": ToolSpec("list_dir", "List files in a directory.", {"path": {"type": "string", "description": "directory"}},
                         ["path"], _list_dir),
    "fetch": ToolSpec("fetch", "Fetch a URL's text content.", {"url": {"type": "string", "description": "https URL"}},
                      ["url"], _fetch),
}
SCHEMAS = [spec.schema() for spec in MOCK_TOOLS.values()]

GOALS = [
    ("g1", "Compare Acme Cloud and Globex Storage pricing (https://example.com/pricing/acme and "
           "https://example.com/pricing/globex) and recommend one for a small team."),
    ("g2", "Check both vendors' status pages (https://example.com/status/acme, https://example.com/status/globex) "
           "and report whether either has ongoing issues."),
    ("g3", "Read notes/vendors.md and notes/meeting-2026-09-15.md, then summarize the renewal decision needed."),
    ("g4", "Find Acme's Pro tier price and Globex's Team tier price (pricing pages under https://example.com/pricing/) "
           "and say which is cheaper per GB."),
    ("g5", "List the files in notes/ and report which ones are relevant to the vendor renewal."),
    ("g6", "Check Acme's status page (https://example.com/status/acme) and report the most recent incident."),
    ("g7", "Gather Acme and Globex pricing plus their status pages (https://example.com/pricing/... and "
           "https://example.com/status/...) and write a one-paragraph vendor comparison."),
    ("g8", "Read notes/team-roster.md and notes/travel-policy.md, then summarize who to contact and the "
           "approval threshold for a trip."),
    ("g9", "Check whether Globex Storage (https://example.com/status/globex) currently has a known performance "
           "problem, and if so since when."),
    ("g10", "Read the 2026-09-15 standup notes and the vendor shortlist in notes/, then report what action item "
            "is open and its two candidates."),
]


def build_messages(goal_text):
    return [{"role": "system", "content": JOB_PROMPT},
            {"role": "system", "content": clock.now_line()},
            {"role": "user", "content": f"Goal: {goal_text}"}]


def make_step(endpoint):
    def step(messages):
        yield from openai_compat.stream(endpoint, messages, tools=SCHEMAS)
    return step


def run_round(endpoint, messages):
    broker = Broker(MOCK_TOOLS, "job", {}, control=None, emit=lambda *a: None)
    return loop.run(make_step(endpoint), messages, call_tool=broker.call, emit=lambda *a: None, budget=20)


def call_evaluator(endpoint, goal_text, summary):
    messages = [{"role": "system", "content": EVALUATOR_PROMPT},
                {"role": "user", "content": f"Goal: {goal_text}\n\nSummary: {summary}"}]
    text, usage = "", None
    for kind, payload in openai_compat.stream(endpoint, messages):
        if kind == "text":
            text += payload
        elif kind == "usage":
            usage = payload
    try:
        parsed = json.loads(text.strip().strip("`").removeprefix("json"))
        verdict = {"adequate": bool(parsed.get("adequate")), "missing": str(parsed.get("missing") or "")}
    except (json.JSONDecodeError, TypeError, AttributeError):
        verdict = {"adequate": True, "missing": "", "unparseable": text[:200]}  # no free round on a bad verdict
    return verdict, usage


def run_item(endpoint, goal_id, goal_text, condition):
    t0 = time.time()
    error = evaluator = None
    steps, usage, summary = 0, None, ""
    try:
        messages = build_messages(goal_text)
        out = run_round(endpoint, messages)
        summary, steps, usage = out.text, out.steps, out.usage
        if condition == "B":
            evaluator, eu = call_evaluator(endpoint, goal_text, summary)
            usage = loop._sum_usage(usage, eu)
            if not evaluator["adequate"]:
                # one more round, never two: the summary so far is already in `messages` as the
                # final assistant message (loop.run appended it), so only the steer goes in
                messages.append({"role": "user", "content": f"Continue: the summary is missing: {evaluator['missing']}"})
                out = run_round(endpoint, messages)
                summary, steps, usage = out.text, steps + out.steps, loop._sum_usage(usage, out.usage)
    except LLMError as e:
        error = f"http {e.status}: {e.message[:160]}"
    tokens = sum(v for k, v in (usage or {}).items() if k in ("prompt_tokens", "completion_tokens"))
    return {"key": os.urandom(4).hex(), "goal_id": goal_id, "condition": condition, "summary": summary,
            "tokens": tokens, "seconds": round(time.time() - t0, 2), "steps": steps, "evaluator": evaluator,
            "error": error}


def run_all(endpoint, goals):
    items = []
    for goal_id, goal_text in goals:
        conditions = ["A", "B"]
        random.shuffle(conditions)
        for c in conditions:
            item = run_item(endpoint, goal_id, goal_text, c)
            items.append(item)
            print(f"  {goal_id} [{c}] steps={item['steps']} tokens={item['tokens']} seconds={item['seconds']:.1f}"
                  + (f" ERROR={item['error']}" if item["error"] else ""), flush=True)
    return items


def grade(items):
    """Print items shuffled, condition hidden, for a blind grader."""
    goal_text = dict(GOALS)
    shown = [it for it in items if not it.get("error")]
    random.shuffle(shown)
    for item in shown:
        print(f"key={item['key']}\ngoal: {goal_text[item['goal_id']]}\nsummary: {item['summary']}\n")


def score(items, votes):
    by_cond = {"A": [], "B": []}
    excluded = 0
    for item in items:
        if item.get("error"):
            excluded += 1
            continue
        by_cond[item["condition"]].append(item)
    print(f"excluded (http error): {excluded}")
    up = {}
    for cond, its in by_cond.items():
        up[cond] = sum(1 for it in its if votes.get(it["key"]) == "up")
        print(f"condition {cond}: {up[cond]}/{len(its)} up, tokens={sum(it['tokens'] for it in its)}, "
              f"seconds={sum(it['seconds'] for it in its):.1f}")
    rounds = sum(1 for it in by_cond["B"] if it["evaluator"] and not it["evaluator"]["adequate"])
    print(f"evaluator asked for a second round on {rounds}/{len(by_cond['B'])} goals")
    by_goal = {}
    for item in items:
        by_goal.setdefault(item["goal_id"], {})[item["condition"]] = item
    print("\nper-goal (A vs B):")
    for goal_id, pair in sorted(by_goal.items(), key=lambda kv: int(kv[0][1:])):
        va = votes.get(pair["A"]["key"], "?") if "A" in pair else "?"
        vb = votes.get(pair["B"]["key"], "?") if "B" in pair else "?"
        print(f"  {goal_id}: A={va} B={vb}")
    diff = up.get("B", 0) - up.get("A", 0)
    passed = diff >= 3
    print(f"\nD23: B - A = {diff} ({'>= 3, evaluator earns its row' if passed else '< 3, evaluator stays out'})")
    return passed


def _fake_stream(endpoint, messages, tools=None, cancel=None):
    """No network: a job's first request makes one read_file call, then answers; the evaluator
    marks a fresh summary inadequate exactly once, and adequate once revised."""
    if any(m["role"] == "system" and "closed JSON" in m["content"] for m in messages):
        summary = messages[-1]["content"]
        bad = "gap" in summary and "revised" not in summary
        yield ("text", '{"adequate": false, "missing": "the vendor comparison"}' if bad else '{"adequate": true, "missing": ""}')
        yield ("usage", {"prompt_tokens": 20, "completion_tokens": 10})
        return
    if not any(m["role"] == "tool" for m in messages):
        yield ("tool_calls", [{"id": "call_0", "name": "read_file", "arguments": {"path": "notes/vendors.md"}}])
        yield ("usage", {"prompt_tokens": 50, "completion_tokens": 15})
        return
    continuing = any(m["role"] == "user" and "Continue" in (m["content"] or "") for m in messages)
    yield ("text", "revised summary, no gap here" if continuing else "summary with a gap in it")
    yield ("usage", {"prompt_tokens": 60, "completion_tokens": 20})


def selfcheck():
    n = 0
    real_stream = openai_compat.stream
    openai_compat.stream = _fake_stream
    try:
        endpoint = Endpoint(base_url="http://fake", api_key="", model="fake")
        with contextlib.redirect_stdout(io.StringIO()):
            items = run_all(endpoint, GOALS)
        assert len(items) == 20; n += 1  # noqa: E702
        by_goal = {}
        for it in items:
            by_goal.setdefault(it["goal_id"], set()).add(it["condition"])
        assert len(by_goal) == 10 and all(c == {"A", "B"} for c in by_goal.values()); n += 1  # noqa: E702
        keys = [it["key"] for it in items]
        assert len(set(keys)) == len(keys); n += 1  # noqa: E702
        a_items = [it for it in items if it["condition"] == "A"]
        b_items = [it for it in items if it["condition"] == "B"]
        assert all(it["evaluator"] is None and it["steps"] == 2 for it in a_items); n += 1  # noqa: E702
        # every fresh summary has the gap -> exactly one extra round each, and the revised text is final
        assert all(it["evaluator"]["adequate"] is False for it in b_items); n += 1  # noqa: E702
        assert all(it["summary"] == "revised summary, no gap here" and it["steps"] == 3 for it in b_items); n += 1  # noqa: E702
        assert all(it["tokens"] > a_items[0]["tokens"] for it in b_items); n += 1  # noqa: E702
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            grade(items)
        out = buf.getvalue()
        assert "condition" not in out.lower() and '"A"' not in out and "[B]" not in out; n += 1  # noqa: E702
        assert all(it["key"] in out for it in items); n += 1  # noqa: E702
        votes = {it["key"]: ("up" if it["condition"] == "B" else "down") for it in items}
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            passed = score(items, votes)
        assert passed is True and "condition A: 0/10 up" in buf2.getvalue() and "condition B: 10/10 up" in buf2.getvalue(); n += 1  # noqa: E702
        with contextlib.redirect_stdout(io.StringIO()):
            assert score(items, {it["key"]: "up" for it in items}) is False; n += 1  # noqa: E702
            # an http error is excluded, not scored
            items[0]["error"] = "http 402: out of credit"
            assert score(items, votes) in (True, False); n += 1  # noqa: E702
    finally:
        openai_compat.stream = real_stream
    print(f"selfcheck: {n} assertions passed")


def main():
    ap = argparse.ArgumentParser(description="m7: does an evaluator pass earn its cost?")
    ap.add_argument("--model", default="gpt-oss:120b-cloud")
    ap.add_argument("--base-url", default="http://localhost:11434/v1")
    ap.add_argument("--tasks", type=int, default=None, help="run only the first N goals (smoke)")
    ap.add_argument("--out", default=None, help="trace file (default docs/measurements/m7-<model>.json)")
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--votes", default=None)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        return selfcheck()
    path = args.out or f"docs/measurements/m7-{args.model.replace(':', '-')}.json"
    if args.grade or args.score:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)["items"]
        if args.grade:
            return grade(items)
        if not args.votes:
            ap.error("--score needs --votes FILE")
        with open(args.votes, encoding="utf-8") as f:
            score(items, json.load(f))
        return
    goals = GOALS[:args.tasks] if args.tasks else GOALS
    endpoint = Endpoint(base_url=args.base_url, api_key="", model=args.model)
    items = run_all(endpoint, goals)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "evaluator_prompt": EVALUATOR_PROMPT, "items": items}, f, ensure_ascii=False, indent=1)
    print(f"saved {path} ({len(items)} items) -- next: --grade, then --score --votes FILE")


if __name__ == "__main__":
    main()
