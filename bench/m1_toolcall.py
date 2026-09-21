#!/usr/bin/env python3
"""
m1_toolcall.py -- measurement m1: native tool calling through an OpenAI-
compatible chat-completions endpoint served by Ollama (local or cloud).
Pass line (owner-decided): each model family >= 85% of 20 tasks (17/20).
Stdlib only.

Usage: python3 bench/m1_toolcall.py --models a,b,c [--tasks N] [--out p] [--max-steps 6]
       python3 bench/m1_toolcall.py --selfcheck
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

SYSTEM_PROMPT = (
    "You are a personal assistant. Today is Tuesday, 2026-09-22, timezone "
    "Asia/Seoul. Use the provided tools when a task requires real-world "
    "action or information. If required information is missing, ask the "
    "user instead of guessing. If no tool is needed, just answer directly."
)


def _tool(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    _tool("get_weather", "Get current weather for a city.",
          {"city": {"type": "string"}}, ["city"]),
    _tool("list_events", "List calendar events for a date.",
          {"date": {"type": "string", "description": "YYYY-MM-DD"}}, ["date"]),
    _tool("add_event", "Add a calendar event.",
          {"title": {"type": "string"}, "start_iso": {"type": "string", "description": "ISO 8601 datetime"},
           "duration_min": {"type": "integer"}}, ["title", "start_iso", "duration_min"]),
    _tool("search_notes", "Search personal notes by query.",
          {"query": {"type": "string"}}, ["query"]),
    _tool("read_note", "Read a note's content by its path.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("send_email", "Send an email.",
          {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
          ["to", "subject", "body"]),
    _tool("run_shell", "Run a shell command.",
          {"command": {"type": "string"}}, ["command"]),
    _tool("start_job", "Start a long-running background job.",
          {"goal": {"type": "string"}}, ["goal"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

_PLAYWRIGHT_NOTE_TEXT = ("Playwright MCP setup notes.\n"
                          "config path: ~/.config/mcp/playwright.json\n"
                          "Restart the MCP host after editing.")

def _h_list_events(a):
    if a.get("date") == "2026-09-23":
        return [{"start": "10:00", "end": "11:00", "title": "Team sync"},
                {"start": "14:00", "end": "15:00", "title": "Dentist"}]
    return []

def _h_search_notes(a):
    q = (a.get("query") or "").lower()
    if "playwright" in q or "플레이라이트" in q:
        return [{"path": "notes/playwright-mcp-setup.md", "title": "Playwright MCP Setup"}]
    return []

def _h_read_note(a):
    if a.get("path") == "notes/playwright-mcp-setup.md":
        return {"path": a.get("path"), "text": _PLAYWRIGHT_NOTE_TEXT}
    return {"path": a.get("path"), "text": "Note not found."}

HANDLERS = {
    "get_weather": lambda a: {"city": a.get("city"), "condition": "Sunny", "temp_c": 24},
    "list_events": _h_list_events,
    "add_event": lambda a: {"status": "ok", "title": a.get("title"),
                             "start_iso": a.get("start_iso"), "duration_min": a.get("duration_min")},
    "search_notes": _h_search_notes,
    "read_note": _h_read_note,
    "send_email": lambda a: {"status": "sent", "to": a.get("to"), "subject": a.get("subject")},
    "run_shell": lambda a: {"stdout": f"(mock) ran: {a.get('command')}", "exit_code": 0},
    "start_job": lambda a: {"job_id": "job-001", "goal": a.get("goal"), "status": "started"},
}

def norm(s):
    return s.strip().lower() if isinstance(s, str) else s

def calls_of(calls, name):
    return [a for n, a in calls if n == name]

def city_is(args, aliases):
    city = args.get("city", "")
    # "Busan, South Korea" is a correct answer: compare the part before the first comma.
    return isinstance(city, str) and norm(city.split(",")[0]) in aliases

def shell_is(args, cmd):
    return norm(args.get("command", "")).strip("'\"") == cmd

def num_eq(args, key, expected):
    try:
        return int(float(args.get(key))) == expected
    except (TypeError, ValueError):
        return False

def to_kst(v):
    """Local (Asia/Seoul) naive datetime. A model may answer in UTC ("...T06:00:00Z" is
    15:00 in Seoul); reading only the clock digits would score that wrong both ways."""
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace(" ", "T"))
    except ValueError:
        return None
    return d.astimezone(KST).replace(tzinfo=None) if d.tzinfo else d

def iso_starts(args, key, prefix):
    d = to_kst(args.get(key))
    return d is not None and d.strftime("%Y-%m-%dT%H:%M") == prefix

def parse_iso_minutes(iso):
    d = to_kst(iso)
    return (d.strftime("%Y-%m-%d"), d.hour * 60 + d.minute) if d else None

BUSY = [(600, 660), (840, 900)]  # 10:00-11:00, 14:00-15:00 in minutes

def playwright_chain_ok(calls, text):
    names = [n for n, _ in calls]
    # Order is judged at the read that hit the right path: guessing a path first,
    # failing, then searching and reading correctly is still a correct chain.
    good_reads = [i for i, (n, a) in enumerate(calls)
                  if n == "read_note" and a.get("path") == "notes/playwright-mcp-setup.md"]
    if "search_notes" not in names or not good_reads:
        return False
    if names.index("search_notes") > good_reads[-1]:
        return False
    return "playwright.json" in text

def no_overlap_deepwork_ok(calls, text):
    names = [n for n, _ in calls]
    if "list_events" not in names or "add_event" not in names:
        return False
    if names.index("list_events") > names.index("add_event"):
        return False
    for a in calls_of(calls, "add_event"):
        parsed = parse_iso_minutes(a.get("start_iso", ""))
        if not parsed:
            continue
        date, start_min = parsed
        if date != "2026-09-23":
            continue
        try:
            dur = int(float(a.get("duration_min")))
        except (TypeError, ValueError):
            continue
        if dur <= 0:
            continue
        end_min = start_min + dur
        if not any(start_min < b_end and end_min > b_start for b_start, b_end in BUSY):
            return True
    return False

def calendar_readonly_ok(calls, text):
    if "add_event" in [n for n, _ in calls]:
        return False
    return any(n == "list_events" and str(a.get("date", "")).strip().startswith("2026-09-23") for n, a in calls)

def notes_not_email_ok(calls, text):
    names = [n for n, _ in calls]
    if "send_email" in names:
        return False
    return "search_notes" in names

def no_tool_math_ok(calls, text):
    return len(calls) == 0 and "391" in text

def missing_info_email_ok(calls, text):
    if "send_email" in [n for n, _ in calls]:
        return False
    if "@" in text:  # inventing a recipient in prose is the failure, even with a question mark
        return False
    low = text.lower()
    return any(k in low for k in ("?", "？", "who", "recipient", "which email", "let me know", "알려"))


TASKS = [
    {"id": "A1", "category": "A", "lang": "ko", "title": "weather Busan",
     "message": "부산 날씨 어때?",
     "check": lambda c, t: any(n == "get_weather" and city_is(a, {"busan", "부산"}) for n, a in c),
     "expect": {"get_weather"}},
    {"id": "A2", "category": "A", "lang": "en", "title": "weather Tokyo",
     "message": "What's the weather in Tokyo?",
     "check": lambda c, t: any(n == "get_weather" and city_is(a, {"tokyo", "도쿄"}) for n, a in c),
     "expect": {"get_weather"}},
    {"id": "A3", "category": "A", "lang": "ko", "title": "start job AI trend research",
     "message": "AI 트렌드를 조사하는 백그라운드 작업을 시작해줘.",
     "check": lambda c, t: any(n == "start_job" and isinstance(a.get("goal"), str) and a.get("goal").strip() for n, a in c),
     "expect": {"start_job"}},
    {"id": "A4", "category": "A", "lang": "en", "title": "start job quantum computing research",
     "message": "Start a background job to research quantum computing.",
     "check": lambda c, t: any(n == "start_job" and isinstance(a.get("goal"), str) and a.get("goal").strip() for n, a in c),
     "expect": {"start_job"}},
    {"id": "A5", "category": "A", "lang": "ko", "title": "run shell ls -la",
     "message": "쉘에서 'ls -la' 명령어를 실행해줘.",
     "check": lambda c, t: any(n == "run_shell" and shell_is(a, "ls -la") for n, a in c),
     "expect": {"run_shell"}},
    {"id": "A6", "category": "A", "lang": "en", "title": "run shell ls -la",
     "message": "Run 'ls -la' in the shell.",
     "check": lambda c, t: any(n == "run_shell" and shell_is(a, "ls -la") for n, a in c),
     "expect": {"run_shell"}},
    {"id": "B1", "category": "B", "lang": "ko", "title": "schedule mentor meeting tomorrow 3pm/30min",
     "message": "내일 오후 3시에 30분짜리 '멘토 미팅' 잡아줘.",
     "check": lambda c, t: any(n == "add_event" and iso_starts(a, "start_iso", "2026-09-23T15:00") and num_eq(a, "duration_min", 30) for n, a in c),
     "expect": {"add_event"}},
    {"id": "B2", "category": "B", "lang": "en", "title": "schedule code review tomorrow 9am/45min",
     "message": "Schedule a 45-minute 'Code Review' tomorrow at 9am.",
     "check": lambda c, t: any(n == "add_event" and iso_starts(a, "start_iso", "2026-09-23T09:00") and num_eq(a, "duration_min", 45) for n, a in c),
     "expect": {"add_event"}},
    {"id": "B3", "category": "B", "lang": "ko", "title": "add dinner today 7pm/1hr",
     "message": "오늘 저녁 7시에 '저녁 약속' 1시간짜리 일정 추가해줘.",
     "check": lambda c, t: any(n == "add_event" and iso_starts(a, "start_iso", "2026-09-22T19:00") and num_eq(a, "duration_min", 60) for n, a in c),
     "expect": {"add_event"}},
    {"id": "C1", "category": "C", "lang": "ko", "title": "playwright note config path chain",
     "message": "플레이라이트 MCP 설정 관련 노트를 찾아서 설정 파일 경로(config path)를 알려줘.",
     "check": playwright_chain_ok, "expect": {"search_notes", "read_note"}},
    {"id": "C2", "category": "C", "lang": "en", "title": "playwright note config path chain",
     "message": "Find my note about Playwright MCP setup and tell me the config path.",
     "check": playwright_chain_ok, "expect": {"search_notes", "read_note"}},
    {"id": "C3", "category": "C", "lang": "ko", "title": "deep work block no overlap",
     "message": "내일 일정 확인하고, 겹치지 않는 시간을 네가 직접 골라서 1시간짜리 '딥워크' 블록 추가해줘. 나한테 되묻지 마.",
     "check": no_overlap_deepwork_ok, "expect": {"list_events", "add_event"}},
    {"id": "C4", "category": "C", "lang": "en", "title": "deep work block no overlap",
     "message": "Check tomorrow's events, then pick a free time yourself and add a 1-hour 'Deep work' block that does not overlap them. Do not ask me.",
     "check": no_overlap_deepwork_ok, "expect": {"list_events", "add_event"}},
    {"id": "C5", "category": "C", "lang": "ko", "title": "focus time block no overlap",
     "message": "내일 일정을 확인하고, 비어 있는 시간을 네가 골라서 1시간짜리 '집중 시간' 블록을 추가해줘. 시간은 묻지 말고 정해.",
     "check": no_overlap_deepwork_ok, "expect": {"list_events", "add_event"}},
    {"id": "D1", "category": "D", "lang": "ko", "title": "calendar read-only distractor",
     "message": "일정을 추가하지 말고, 내일 일정이 뭐가 있는지만 알려줘.",
     "check": calendar_readonly_ok, "expect": {"list_events"}, "forbid": {"add_event"}},
    {"id": "D2", "category": "D", "lang": "en", "title": "calendar read-only distractor",
     "message": "Don't add anything, just tell me what's on my calendar tomorrow.",
     "check": calendar_readonly_ok, "expect": {"list_events"}, "forbid": {"add_event"}},
    {"id": "D3", "category": "D", "lang": "en", "title": "search notes not email distractor",
     "message": "I don't need an email sent, just find my notes about Playwright.",
     "check": notes_not_email_ok, "expect": {"search_notes"}, "forbid": {"send_email"}},
    {"id": "E1", "category": "E", "lang": "ko", "title": "no tool needed: multiplication",
     "message": "17 곱하기 23은 뭐야?",
     "check": no_tool_math_ok, "expect": set(), "forbid": TOOL_NAMES},
    {"id": "E2", "category": "E", "lang": "en", "title": "no tool needed: multiplication",
     "message": "What is 17 * 23?",
     "check": no_tool_math_ok, "expect": set(), "forbid": TOOL_NAMES},
    {"id": "F1", "category": "F", "lang": "en", "title": "missing info: send the email",
     "message": "Send the email.",
     "check": missing_info_email_ok, "expect": set(), "forbid": {"send_email"}},
]


def selfcheck():
    c1 = next(t for t in TASKS if t["id"] == "C1")
    calls_pass = [("search_notes", {"query": "playwright"}),
                  ("read_note", {"path": "notes/playwright-mcp-setup.md"})]
    assert c1["check"](calls_pass, "The config path is ~/.config/mcp/playwright.json") is True

    calls_fail1 = [("search_notes", {"query": "playwright"})]  # never read the note
    assert c1["check"](calls_fail1, "I found a note but did not read it yet.") is False

    e1 = next(t for t in TASKS if t["id"] == "E1")
    calls_fail2 = [("get_weather", {"city": "Busan"})]  # a tool was called when none should be
    assert e1["check"](calls_fail2, "391") is False

    # Time zones: 06:00Z is 15:00 in Seoul (B1 passes); 01:00Z is 10:00 in Seoul and
    # collides with "Team sync" (C3 must fail even though the digits read 01:00).
    by_id = {t["id"]: t for t in TASKS}
    assert by_id["B1"]["check"]([("add_event", {"title": "x", "start_iso": "2026-09-23T06:00:00Z", "duration_min": "30"})], "")
    assert not by_id["B1"]["check"]([("add_event", {"title": "x", "start_iso": "2026-09-23T06:00:00", "duration_min": 30})], "")
    listed = ("list_events", {"date": "2026-09-23"})
    assert not by_id["C3"]["check"]([listed, ("add_event", {"title": "d", "start_iso": "2026-09-23T01:00:00Z", "duration_min": 60})], "")
    assert by_id["C3"]["check"]([listed, ("add_event", {"title": "d", "start_iso": "2026-09-23T16:00:00+09:00", "duration_min": 60})], "")
    assert by_id["A1"]["check"]([("get_weather", {"city": "Busan, South Korea"})], "")
    assert by_id["D1"]["check"]([("list_events", {"date": "2026-09-23T00:00:00"})], "")
    assert not by_id["F1"]["check"]([], "Sure, I'll send it to john" + "@" + "example.com, ok?")
    assert by_id["F1"]["check"]([], "Please let me know the recipient and the subject.")
    # A guessed read first, then search, then the right read is still a correct chain.
    assert c1["check"]([("read_note", {"path": "notes/guess.md"})] + calls_pass, "it is ~/.config/mcp/playwright.json")

    print("selfcheck: 12/12 assertions passed")


def call_llm(base_url, model, messages):
    url = f"{base_url}/chat/completions"
    payload = {"model": model, "messages": messages, "tools": TOOLS,
               "tool_choice": "auto", "stream": False, "temperature": 0}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read()), time.time() - t0, None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw).get("error", {}).get("message") or raw.decode(errors="replace")
        except json.JSONDecodeError:
            msg = raw.decode(errors="replace")
        return None, time.time() - t0, (e.code, msg[:200])
    except urllib.error.URLError as e:
        return None, time.time() - t0, (0, str(e)[:200])

def run_task(base_url, model, task, max_steps):
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task["message"]}]
    calls, latencies = [], []
    usage_p = usage_c = 0
    had_json_error = False
    stopped_naturally = False
    http_err = None
    final_text = ""
    steps = 0
    while steps < max_steps:
        steps += 1
        body, latency, err = call_llm(base_url, model, messages)
        latencies.append(latency)
        if err:
            http_err = err
            break
        usage = body.get("usage") or {}
        usage_p += usage.get("prompt_tokens", 0) or 0
        usage_c += usage.get("completion_tokens", 0) or 0
        msg = body["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        final_text = msg.get("content") or ""
        # Send back only role/content/tool_calls: the server also returns reasoning
        # fields, and echoing those can be rejected. An empty id breaks the next request.
        for i, tc in enumerate(tool_calls):
            tc["id"] = tc.get("id") or f"call_{steps}_{i}"
        reply = {"role": "assistant", "content": final_text}
        if tool_calls:
            reply["tool_calls"] = [{"id": tc["id"], "type": "function", "function": tc["function"]} for tc in tool_calls]
        messages.append(reply)
        if not tool_calls:
            stopped_naturally = True
            break
        for tc in tool_calls:
            fn = tc["function"]["name"]
            raw_args = tc["function"].get("arguments") or "{}"
            try:  # some servers send arguments as an object instead of a JSON string
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
                if not isinstance(args, dict):
                    raise ValueError("arguments are not an object")
            except (TypeError, ValueError):
                had_json_error = True
                args = {}
                result = {"error": "arg_json_error: could not parse arguments"}
            else:
                handler = HANDLERS.get(fn)
                result = handler(args) if handler else {"error": f"unknown tool {fn}"}
            if isinstance(raw_args, dict):
                tc["function"]["arguments"] = json.dumps(raw_args, ensure_ascii=False)
            calls.append((fn, args))
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                              "content": json.dumps(result, ensure_ascii=False)})
    return {"calls": calls, "final_text": final_text, "latencies": latencies,
            "prompt_tokens": usage_p, "completion_tokens": usage_c,
            "had_json_error": had_json_error, "stopped_naturally": stopped_naturally,
            "steps": steps, "http_err": http_err}

def classify_failure(task, r):
    if r["http_err"]:
        return "http_error"
    if r["had_json_error"]:
        return "arg_json_error"
    names = {n for n, _ in r["calls"]}
    forbid = task.get("forbid", set())
    if names & forbid:
        return "forbidden_call"
    expect = task.get("expect", set())
    if expect:
        if not names & expect:
            return "missing_call"
        if not expect.issubset(names):
            return "missing_call"
        return "wrong_args"
    if names:
        return "wrong_tool"
    return "wrong_args"

SIDE_EFFECT_TOOLS = {"send_email", "run_shell"}

def run_model(base_url, model, tasks, max_steps):
    results, blocked, recent_errs = [], None, []
    for task in tasks:
        r = run_task(base_url, model, task, max_steps)
        if r["http_err"] and r["http_err"][0] in (402, 429):
            blocked = f"HTTP {r['http_err'][0]}: {r['http_err'][1]}"
            break
        # The same error three times in a row is the harness or the server, not the
        # model's tool calling: stop instead of reporting a misleading 0/20.
        recent_errs = (recent_errs + [r["http_err"]])[-3:] if r["http_err"] else []
        if len(recent_errs) == 3 and len(set(recent_errs)) == 1:
            blocked = f"same error 3x, HTTP {r['http_err'][0]}: {r['http_err'][1]}"
            break
        passed = False
        if not r["http_err"]:
            try:
                passed = bool(task["check"](r["calls"], r["final_text"]))
            except Exception:
                passed = False
        # A right call plus an uninvited email or shell command is not a pass.
        uninvited = {n for n, _ in r["calls"]} & SIDE_EFFECT_TOOLS - task.get("expect", set())
        if uninvited:
            passed = False
        if passed:
            reason = None
        elif uninvited:
            reason = "forbidden_call"
        elif r["http_err"]:
            reason = "http_error"
        elif not r["stopped_naturally"] and r["steps"] >= max_steps:
            reason = "max_steps"
        else:
            reason = classify_failure(task, r)
        results.append({
            "id": task["id"], "category": task["category"], "lang": task["lang"],
            "title": task["title"], "passed": passed, "steps": r["steps"], "reason": reason,
            "calls": [{"name": n, "args": a} for n, a in r["calls"]],
            "final_text": r["final_text"],
            "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
            "latencies": r["latencies"],
            "http_error": f"HTTP {r['http_err'][0]}: {r['http_err'][1]}" if r["http_err"] else None,
        })
    return results, blocked


def print_report(all_results, tasks):
    cats = sorted({t["category"] for t in tasks})
    print(f"\n{'model':<24} {'passed':<8} " + " ".join(f"{c}" for c in cats) + f" {'tokens':<8} {'med_lat_s':<10} blocked")
    for model, data in all_results.items():
        results = data["results"]
        blocked = data["blocked"]
        n_pass = sum(1 for r in results if r["passed"])
        n_total = len(tasks)  # always out of the full set: a blocked run must not read as 12/12
        per_cat = []
        for c in cats:
            cat_results = [r for r in results if r["category"] == c]
            cat_pass = sum(1 for r in cat_results if r["passed"])
            per_cat.append(f"{cat_pass}/{len(cat_results)}" if cat_results else "-")
        tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in results)
        all_lat = [lat for r in results for lat in r["latencies"]]
        med_lat = f"{statistics.median(all_lat):.2f}" if all_lat else "-"
        blocked_str = f"INCOMPLETE ({len(results)}/{len(tasks)} ran): {blocked}" if blocked else "no"
        print(f"{model:<24} {n_pass}/{n_total:<6} " + " ".join(f"{p:<5}" for p in per_cat) +
              f" {tokens:<8} {med_lat:<10} {blocked_str}")
        for r in results:
            if not r["passed"]:
                print(f"    FAIL {r['id']} [{r['category']}] {r['title']}: {r['reason']} {r.get('http_error') or ''}")

def main():
    ap = argparse.ArgumentParser(description="m1: native tool-calling benchmark")
    ap.add_argument("--models", default=None, help="comma-separated model names (required unless --selfcheck)")
    ap.add_argument("--tasks", type=int, default=None, help="run only first N tasks (default: all 20)")
    ap.add_argument("--out", default=None, help="output JSON path")
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        selfcheck()
        return
    if not args.models:
        ap.error("--models is required (unless --selfcheck)")

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    tasks = TASKS[:args.tasks] if args.tasks else TASKS
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    out_path = args.out
    if not out_path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = os.path.join("bench", "results", f"m1-{ts}.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    all_results = {}
    for model in models:
        print(f"running {model} ({len(tasks)} tasks)...", file=sys.stderr)
        results, blocked = run_model(base_url, model, tasks, args.max_steps)
        all_results[model] = {"results": results, "blocked": blocked}
        # Save after every model: a crash on the third must not lose two paid-for runs.
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"base_url": base_url, "max_steps": args.max_steps,
                        "n_tasks": len(tasks), "models": all_results}, f,
                       ensure_ascii=False, indent=2)

    print_report(all_results, tasks)
    print(f"\nwrote {out_path}")

if __name__ == "__main__":
    main()
