#!/usr/bin/env python3
"""
m2_steer.py -- measurement m2: does a mid-turn steer (D12/D17) break the conversation?
Uses the real streaming adapter (pathfinder.llm.openai_compat) against an OpenAI-compatible
endpoint (Ollama, local or cloud). Pass line (owner): reflected >= 18/20 AND kept >= 18/20.
Stdlib only.

Usage: python3 bench/m2_steer.py --models a,b [--tasks N] [--out dir] [--max-steps 6] [--base-url url]
       python3 bench/m2_steer.py --selfcheck
"""
import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathfinder.llm import Endpoint, LLMError  # noqa: E402
from pathfinder.llm.openai_compat import stream  # noqa: E402

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
    _tool("get_weather", "Get current weather for a city.", {"city": {"type": "string"}}, ["city"]),
    _tool("list_events", "List calendar events for a date.",
          {"date": {"type": "string", "description": "YYYY-MM-DD"}}, ["date"]),
    _tool("add_event", "Add a calendar event.",
          {"title": {"type": "string"}, "start_iso": {"type": "string", "description": "ISO 8601 datetime"},
           "duration_min": {"type": "integer"}}, ["title", "start_iso", "duration_min"]),
    _tool("search_notes", "Search personal notes by query.", {"query": {"type": "string"}}, ["query"]),
    _tool("read_note", "Read a note's content by its path.", {"path": {"type": "string"}}, ["path"]),
    _tool("run_shell", "Run a shell command.", {"command": {"type": "string"}}, ["command"]),
]

_PLAYWRIGHT_NOTE = "Playwright MCP setup notes.\nconfig path: ~/.config/mcp/playwright.json\n"
_AI_TREND_NOTE = "AI Trend Research notes.\nSources: arxiv, conference talks.\nSummary: transformer scaling continues.\n"

def _h_list_events(a):
    if str(a.get("date", "")).startswith("2026-09-23"):
        return [{"start": "10:00", "end": "11:00", "title": "Team sync"},
                {"start": "14:00", "end": "15:00", "title": "Dentist"}]
    return []

def _h_search_notes(a):
    q = (a.get("query") or "").lower()
    if "playwright" in q or "플레이" in q:
        return [{"path": "notes/playwright-mcp-setup.md", "title": "Playwright MCP Setup"}]
    if "ai" in q or "트렌드" in q or "trend" in q:
        return [{"path": "notes/ai-trend-research.md", "title": "AI Trend Research"}]
    return []

def _h_read_note(a):
    path = a.get("path")
    if path == "notes/playwright-mcp-setup.md":
        return {"path": path, "text": _PLAYWRIGHT_NOTE}
    if path == "notes/ai-trend-research.md":
        return {"path": path, "text": _AI_TREND_NOTE}
    return {"path": path, "text": "Note not found."}

HANDLERS = {
    "get_weather": lambda a: {"city": a.get("city"), "condition": "Sunny", "temp_c": 24},
    "list_events": _h_list_events,
    "add_event": lambda a: {"status": "ok", "title": a.get("title"),
                             "start_iso": a.get("start_iso"), "duration_min": a.get("duration_min")},
    "search_notes": _h_search_notes,
    "read_note": _h_read_note,
    "run_shell": lambda a: {"stdout": f"(mock) ran: {a.get('command')}", "exit_code": 0},
}

# Regional indicators (flags) start at U+1F1E6: a country list will get flags.
EMOJI_RE = re.compile(r"[\U0001F1E6-\U0001FAFF☀-➿]")

def norm(s):
    return s.strip().lower() if isinstance(s, str) else s

def calls_of(calls, name):
    return [a for n, a in calls if n == name]

def to_kst(v):
    # Local (Asia/Seoul) naive datetime, same rule as m1: a model may answer in UTC,
    # so compare the converted clock, not the raw digits.
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

def date_is(args, key, day):
    d = to_kst(args.get(key))
    return d is not None and d.strftime("%Y-%m-%d") == day

def num_eq(args, key, expected):
    try:
        return int(float(args.get(key))) == expected
    except (TypeError, ValueError):
        return False

def city_is(args, aliases):
    city = args.get("city", "")
    return isinstance(city, str) and norm(re.split(r"[,(]", city)[0]) in aliases

def shell_is(args, cmd):
    return norm(args.get("command", "")).strip("'\"`") == cmd

def ascii_letter_ratio(text):
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if c.isascii()) / len(letters) if letters else 0.0

def lines_of(text):
    return [l.strip() for l in text.strip().splitlines() if l.strip()]

def item_nums(text):
    # Numbers that ARE a list item ("7", "1. 7", "7."), not digits inside prose: a model
    # that acknowledges "20에서 멈춰줘" before obeying must not fail on the 20 in its own words.
    out = []
    for l in lines_of(text):
        l = re.sub(r"^\s*\d+[.)]\s+(?=\S)", "", l)  # "1. 7" -> "7", but "7." stays a number
        l = re.sub(r"^[-*•]\s*|\*+", "", l).strip()
        if re.fullmatch(r"-?\d+[.,)]?", l):
            out.append(int(re.findall(r"-?\d+", l)[0]))
    return out

def _cut_ready(text):
    return text.count("\n") >= 3 or len(text) >= 160

# The envelope a mid-stream steer travels in. Run 1 (no envelope) showed both families
# rewriting the list from 1 or asking "backwards from where?": the model cannot know its
# answer was cut unless the harness says so. Partition 2 sends the same envelope.
STEER_ENVELOPE = ("[The user interrupted mid-answer with the instruction below. Apply it and continue "
                  "from exactly where the answer above stopped; do not repeat what was already written.]\n")
ENVELOPE = True

def has(t, *words):
    return any(w in t.lower() for w in words)

def weather_said(t):
    return "24" in t or "sunny" in t.lower() or "맑" in t

# Category A (tool boundary, 10 tasks). The steer is appended after ALL tool results of the
# first tool-bearing step (a user message between two results of one assistant message
# would leave a tool_call id unanswered). `pre` = calls made before the steer, `post` =
# after. `too_late(pre)`: the steered action already happened before the steer could be
# sent (parallel calls) - that task is excluded, not scored against the model.
def _a(id_, title, message, steer, reflected, kept, too_late, pass_case, fail_case):
    return {"id": id_, "category": "A", "title": title, "message": message, "steer": steer,
            "reflected": reflected, "kept": kept, "too_late": too_late,
            "pass_case": pass_case, "fail_case": fail_case}

def _pre_has(name, n=1):
    return lambda pre: len(calls_of(pre, name)) >= n

_T = "2026-09-23"
_LE = ("list_events", {"date": _T})

TASKS_A = [
    _a("A1", "weather city swap",
       "부산 날씨 확인하고, 부산이 맑으면 도쿄 날씨도 알려줘.", "잠깐, 도쿄 말고 오사카 날씨로 확인해줘.",
       lambda pre, post, t: any(city_is(a, {"osaka", "오사카"}) for a in calls_of(post, "get_weather")),
       lambda pre, post, t: any(city_is(a, {"busan", "부산"}) for a in calls_of(pre, "get_weather")) and has(t, "부산", "busan") and weather_said(t),
       _pre_has("get_weather", 2),
       ([("get_weather", {"city": "Busan"})], [("get_weather", {"city": "Osaka"})], "부산: 맑음 24도. 오사카도 맑음."),
       ([("get_weather", {"city": "Busan"})], [("get_weather", {"city": "Tokyo"})], "Osaka is sunny.")),
    _a("A2", "event duration change",
       "내일 일정 확인하고, 비어있는 시간에 오후 4시부터 30분짜리 '독서' 일정 추가해줘.", "시간은 그대로 두고, 30분 말고 60분으로 바꿔줘.",
       lambda pre, post, t: any(iso_starts(a, "start_iso", f"{_T}T16:00") and num_eq(a, "duration_min", 60) for a in calls_of(post, "add_event")),
       lambda pre, post, t: any(date_is(a, "date", _T) for a in calls_of(pre, "list_events")) and any("독서" in str(a.get("title", "")) for a in calls_of(post, "add_event")),
       _pre_has("add_event"),
       ([_LE], [("add_event", {"title": "독서", "start_iso": f"{_T}T16:00:00", "duration_min": 60})], ""),
       ([_LE], [("add_event", {"title": "청소", "start_iso": f"{_T}T16:00:00", "duration_min": 30})], "")),
    _a("A3", "event time change",
       "내일 일정 확인하고, 오전 11시부터 1시간짜리 '점심 약속' 일정 추가해줘.", "시간을 오전 11시 말고 오후 5시로 바꿔줘.",
       lambda pre, post, t: any(iso_starts(a, "start_iso", f"{_T}T17:00") for a in calls_of(post, "add_event")),
       lambda pre, post, t: any(date_is(a, "date", _T) for a in calls_of(pre, "list_events")) and any("점심" in str(a.get("title", "")) and num_eq(a, "duration_min", 60) for a in calls_of(post, "add_event")),
       _pre_has("add_event"),
       ([_LE], [("add_event", {"title": "점심 약속", "start_iso": f"{_T}T17:00:00", "duration_min": 60})], ""),
       ([_LE], [("add_event", {"title": "청소", "start_iso": f"{_T}T11:00:00", "duration_min": 60})], "")),
    _a("A4", "one more event",
       "내일 일정 확인하고, 오후 6시부터 1시간짜리 '저녁 운동' 일정 추가해줘.", "그리고 오후 8시부터 30분짜리 '독서' 일정도 하나 더 추가해줘.",
       lambda pre, post, t: any(iso_starts(a, "start_iso", f"{_T}T20:00") and num_eq(a, "duration_min", 30) for a in calls_of(post, "add_event")),
       lambda pre, post, t: any(iso_starts(a, "start_iso", f"{_T}T18:00") and num_eq(a, "duration_min", 60) and "저녁" in str(a.get("title", "")) for a in calls_of(pre + post, "add_event")),
       lambda pre: False,  # an early first event does not make "one more" impossible
       ([_LE], [("add_event", {"title": "저녁 운동", "start_iso": f"{_T}T18:00:00", "duration_min": 60}),
                ("add_event", {"title": "독서", "start_iso": f"{_T}T20:00:00", "duration_min": 30})], ""),
       ([_LE], [("add_event", {"title": "청소", "start_iso": f"{_T}T09:00:00", "duration_min": 15})], "")),
    _a("A5", "cancel the event after all",
       "내일 일정 확인하고, 오후 3시부터 1시간짜리 '고객 미팅' 일정 추가해줘.", "잠깐, 일정 추가하지 마. 그냥 취소할래.",
       lambda pre, post, t: not calls_of(post, "add_event"),
       lambda pre, post, t: any(date_is(a, "date", _T) for a in calls_of(pre, "list_events")),
       _pre_has("add_event"),
       ([_LE], [], "알겠습니다. 추가하지 않았습니다."),
       ([("get_weather", {"city": "Seoul"})], [("add_event", {"title": "고객 미팅", "start_iso": f"{_T}T15:00:00", "duration_min": 60})], "")),
    _a("A6", "different note",
       "플레이라이트 MCP 설정 노트를 찾아서 설정 경로를 알려주고, 그다음 서울 날씨도 알려줘.", "잠깐, 플레이라이트 말고 'AI 트렌드' 관련 노트를 찾아서 읽어줘.",
       lambda pre, post, t: any(a.get("path") == "notes/ai-trend-research.md" for a in calls_of(post, "read_note")),
       lambda pre, post, t: any(city_is(a, {"seoul", "서울"}) for a in calls_of(pre + post, "get_weather")) and weather_said(t),
       _pre_has("read_note"),
       ([("search_notes", {"query": "playwright"})], [("search_notes", {"query": "AI 트렌드"}), ("read_note", {"path": "notes/ai-trend-research.md"}), ("get_weather", {"city": "Seoul"})], "서울은 맑음 24도."),
       ([("search_notes", {"query": "playwright"})], [("read_note", {"path": "notes/playwright-mcp-setup.md"})], "")),
    _a("A7", "different shell command",
       "먼저 서울 날씨 확인하고, 맑으면 쉘에서 'ls -la' 명령어를 실행해줘.", "쉘 명령은 'ls -la' 말고 'pwd'로 실행해줘.",
       lambda pre, post, t: any(shell_is(a, "pwd") for a in calls_of(post, "run_shell")),
       lambda pre, post, t: any(city_is(a, {"seoul", "서울"}) for a in calls_of(pre, "get_weather")) and has(t, "서울", "seoul") and weather_said(t),
       _pre_has("run_shell"),
       ([("get_weather", {"city": "Seoul"})], [("run_shell", {"command": "pwd"})], "Seoul: sunny, 24C."),
       ([("get_weather", {"city": "Seoul"})], [("run_shell", {"command": "ls -la"})], "sunny")),
    _a("A8", "answer in English",
       "부산 날씨 확인하고, 맑으면 내일 일정도 확인해줘.", "From now on, answer in English.",
       lambda pre, post, t: ascii_letter_ratio(t) >= 0.9,
       lambda pre, post, t: any(city_is(a, {"busan", "부산"}) for a in calls_of(pre, "get_weather")) and any(date_is(a, "date", _T) for a in calls_of(post, "list_events")),
       lambda pre: False,
       ([("get_weather", {"city": "Busan"})], [("list_events", {"date": _T})], "Busan is sunny and 24C. Tomorrow: Team sync at 10, Dentist at 14."),
       ([("get_weather", {"city": "Busan"})], [], "부산은 맑고 24도입니다.")),
    _a("A9", "event duration shrink",
       "먼저 런던 날씨 확인하고, 맑으면 내일 오후 1시부터 45분짜리 '전화 회의' 일정 추가해줘.", "시간은 그대로 두고 45분 대신 20분으로 줄여줘.",
       lambda pre, post, t: any(iso_starts(a, "start_iso", f"{_T}T13:00") and num_eq(a, "duration_min", 20) for a in calls_of(post, "add_event")),
       lambda pre, post, t: any(city_is(a, {"london", "런던"}) for a in calls_of(pre, "get_weather")) and has(t, "런던", "london") and weather_said(t),
       _pre_has("add_event"),
       ([("get_weather", {"city": "London"})], [("add_event", {"title": "전화 회의", "start_iso": f"{_T}T13:00:00", "duration_min": 20})], "London: sunny, 24C."),
       ([("get_weather", {"city": "London"})], [("add_event", {"title": "전화 회의", "start_iso": f"{_T}T13:00:00", "duration_min": 45})], "24")),
    _a("A10", "different date",
       "제주 날씨 확인하고, 맑으면 내일 일정도 확인해줘.", "내일 말고 오늘 일정으로 확인해줘.",
       lambda pre, post, t: any(date_is(a, "date", "2026-09-22") for a in calls_of(post, "list_events")),
       lambda pre, post, t: any(city_is(a, {"jeju", "제주"}) for a in calls_of(pre, "get_weather")) and has(t, "제주", "jeju") and weather_said(t),
       _pre_has("list_events"),
       ([("get_weather", {"city": "Jeju"})], [("list_events", {"date": "2026-09-21T15:00:00Z"})], "제주: 맑음, 24도."),
       ([("get_weather", {"city": "Jeju"})], [("list_events", {"date": _T})], "sunny")),
]

# Category B (mid-stream, 10 tasks): the stream is cut once >=3 complete lines or >=160
# chars have arrived. reflected/kept score the CONTINUATION (kept also sees the partial).
# A continuation that restarts the list, or one that ignores the steer, must fail.

def _b(id_, title, message, steer, reflected, kept, pass_case, fail_cases, n_total):
    return {"id": id_, "category": "B", "title": title, "message": message, "steer": steer, "n_total": n_total,
            "reflected": reflected, "kept": kept, "pass_case": pass_case, "fail_cases": fail_cases}

def _no_restart(p, cont, n_total):
    lp, lc = lines_of(p), lines_of(cont)
    if p and not p.endswith("\n"):
        lp = lp[:-1]  # a cut mid-word leaves a fragment ("미" of "미국"): not an item, and re-typing it is right
    items_p = [re.sub(r"^\s*\d+[.)]\s*", "", l) for l in lp]
    return bool(lc) and not any(l.startswith(pi) for l in lc for pi in items_p if pi) and len(lp) + len(lc) <= n_total + 2

def _b_every_fifth(id_, n_total):
    partial = "1\n2\n3\n"
    return _b(id_, f"every fifth to {n_total}", f"1부터 {n_total}까지 숫자를 한 줄에 하나씩 세어줘.", "지금부터는 5의 배수만 세어줘.",
              lambda cont: bool(item_nums(cont)) and all(n % 5 == 0 for n in item_nums(cont)),
              lambda p, cont: bool(item_nums(cont)) and item_nums(cont)[0] != 1 and n_total in item_nums(cont),
              (partial, "네, 5의 배수만 세겠습니다.\n" + "\n".join(f"{k}." if k % 10 else str(k) for k in range(5, n_total + 1, 5)) + "\n"),
              [(partial, partial), (partial, "4\n5\n10\n")], n_total)

def _b_stop_at(id_, n_total, stop_at):
    partial = "1\n2\n3\n"
    return _b(id_, f"stop at {stop_at} of {n_total}", f"1부터 {n_total}까지 숫자를 한 줄에 하나씩 세어줘.", f"{n_total}까지 말고 {stop_at}에서 멈춰줘.",
              lambda cont: bool(item_nums(cont)) and max(item_nums(cont)) == stop_at,
              lambda p, cont: bool(item_nums(cont)) and (not item_nums(p) or item_nums(cont)[0] == item_nums(p)[-1] + 1) and stop_at in item_nums(cont),
              (partial, f"알겠습니다, {n_total}까지 말고 {stop_at}에서 멈추겠습니다.\n" + "\n".join(f"{k}. {k}" for k in range(4, stop_at + 1)) + "\n"),
              [(partial, partial), (partial, "\n".join(str(k) for k in range(1, stop_at + 1)) + "\n")], n_total)

def _b_backwards(id_, n_total):
    partial = "1\n2\n3\n"
    return _b(id_, f"backwards from step to {n_total}", f"1부터 {n_total}까지 숫자를 한 줄에 하나씩 세어줘.", "여기서부터는 거꾸로 세어줘.",
              lambda cont: len(item_nums(cont)) >= 2 and all(a > b for a, b in zip(item_nums(cont), item_nums(cont)[1:])),
              lambda p, cont: bool(item_nums(cont)) and (not item_nums(p) or abs(item_nums(cont)[0] - item_nums(p)[-1]) <= 1),
              (partial, "3부터 거꾸로 세겠습니다:\n3\n2\n1\n"),
              [(partial, partial), (partial, "30\n29\n28\n")], n_total)

def _b_list_english(id_, kind, n_total):
    partial = "1. 사과\n2. 바나나\n3. 포도\n"
    return _b(id_, f"{kind} list, continue in English ({n_total})", f"{kind} 이름 {n_total}개를 한 줄에 하나씩 한국어로 나열해줘.", "Continue in English from here.",
              lambda cont: ascii_letter_ratio(cont) >= 0.9,
              lambda p, cont: _no_restart(p, cont, n_total),
              (partial, "\n".join(f"{k}. Fruit{k}" for k in range(4, n_total + 1)) + "\n"),
              [(partial, partial), (partial, "\n".join(f"{k}. Fruit{k}" for k in range(1, n_total + 1)) + "\n")], n_total)

def _b_list_emoji(id_, kind, n_total):
    partial = "한국\n일본\n중국\n"
    return _b(id_, f"{kind} list, add emoji ({n_total})", f"{kind} 이름 {n_total}개를 한 줄에 하나씩 나열해줘.", "여기서부터는 각 줄 끝에 이모지를 하나씩 붙여줘.",
              # an intro and an outro line without an emoji are allowed; the items must have one
              lambda cont: sum(1 for l in lines_of(cont) if EMOJI_RE.search(l)) >= max(2, len(lines_of(cont)) - 2),
              lambda p, cont: _no_restart(p, cont, n_total),
              (partial, "알겠습니다!\n미국 \U0001F1FA\U0001F1F8\n영국 \U0001F1EC\U0001F1E7\n프랑스 \U0001F31F\n"),
              [(partial, partial), (partial, "한국 \U0001F1F0\U0001F1F7\n일본 \U0001F1EF\U0001F1F5\n")], n_total)

TASKS_B = [
    _b_every_fifth("B1", 30), _b_stop_at("B2", 40, 20), _b_backwards("B3", 30),
    _b_list_english("B4", "과일", 20), _b_list_emoji("B5", "나라", 15),
    _b_stop_at("B6", 50, 35), _b_list_english("B7", "색깔", 15),
    _b_every_fifth("B8", 45), _b_backwards("B9", 40), _b_list_emoji("B10", "나라", 12),
]

TASKS = TASKS_A + TASKS_B

def selfcheck():
    n = 0
    for task in TASKS_A:
        pre, post, t = task["pass_case"]
        assert task["reflected"](pre, post, t) is True, f"{task['id']} reflected pass_case"
        assert task["kept"](pre, post, t) is True, f"{task['id']} kept pass_case"
        assert task["too_late"](pre) is False, f"{task['id']} too_late pass_case"
        pre, post, t = task["fail_case"]
        assert task["reflected"](pre, post, t) is False, f"{task['id']} reflected fail_case"
        assert task["kept"](pre, post, t) is False, f"{task['id']} kept fail_case"
        n += 5
    for task in TASKS_B:
        p, cont = task["pass_case"]
        assert task["reflected"](cont) is True, f"{task['id']} reflected pass_case"
        assert task["kept"](p, cont) is True, f"{task['id']} kept pass_case"
        for p, cont in task["fail_cases"]:  # a verbatim restart, then a near miss
            assert not (task["reflected"](cont) and task["kept"](p, cont)), f"{task['id']} fail_case passed"
        n += 4
    assert item_nums("알겠습니다, 40까지 말고 20에서 멈추겠습니다.\n4\n1. 5\n**6**\n7.") == [4, 5, 6, 7]
    assert EMOJI_RE.search("미국 \U0001F1FA\U0001F1F8") and not EMOJI_RE.search("미국")
    assert date_is({"date": "2026-09-21T15:00:00Z"}, "date", "2026-09-22")
    n += 3
    print(f"selfcheck: {n}/{n} assertions passed")

def call_model(endpoint, messages, tools=None, cancel=None):
    # Consume stream() to completion. Only "text" and "tool_calls" payloads are kept
    # -- reasoning is never echoed back to the model.
    t0 = time.time()
    text_parts, tool_calls, usage, cancelled, err = [], [], {}, False, None
    try:
        for kind, payload in stream(endpoint, messages, tools=tools, cancel=cancel):
            if kind == "text":
                text_parts.append(payload)
            elif kind == "tool_calls":
                tool_calls = payload
            elif kind == "usage":
                usage = payload or {}
            elif kind == "cancelled":
                cancelled = True
    except LLMError as e:
        err = (e.status, e.message)
    return {"text": "".join(text_parts), "tool_calls": tool_calls, "usage": usage,
            "cancelled": cancelled, "latency": time.time() - t0, "err": err}

def run_b_first_stream(endpoint, messages):
    # Stream the first response, setting `cancel` as soon as our own accumulated text
    # meets the cut condition. `stream` checks `cancel` between chunks and yields
    # ("cancelled", None) once it takes effect. The adapter yields no usage when cut, so
    # this request's tokens are not counted (the B token column is a lower bound).
    cancel = threading.Event()
    t0 = time.time()
    parts, cancelled, err = [], False, None
    try:
        for kind, payload in stream(endpoint, messages, tools=None, cancel=cancel):
            if kind == "text":
                parts.append(payload)
                if _cut_ready("".join(parts)):
                    cancel.set()
            elif kind == "cancelled":
                cancelled = True
    except LLMError as e:
        err = (e.status, e.message)
    return {"partial": "".join(parts), "cancelled": cancelled, "latency": time.time() - t0, "err": err}

def run_task_a(endpoint, task, max_steps):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task["message"]}]
    calls, latencies, seen_ids = [], [], set()
    up = uc = 0
    final_text, err, steer_at, steps = "", None, None, 0
    while steps < max_steps:
        steps += 1
        r = call_model(endpoint, messages, tools=TOOLS)
        latencies.append(r["latency"])
        if r["err"]:
            err = r["err"]
            break
        up += r["usage"].get("prompt_tokens", 0) or 0
        uc += r["usage"].get("completion_tokens", 0) or 0
        final_text = r["text"]
        tcs = r["tool_calls"]
        for i, tc in enumerate(tcs):
            # The adapter's own fallback restarts at call_0 every response: re-key collisions.
            tc["id"] = tc.get("id") or f"call_{steps}_{i}"
            if tc["id"] in seen_ids:
                tc["id"] = f"{tc['id']}_{steps}"
            seen_ids.add(tc["id"])
        # Rebuild the assistant message as role/content/tool_calls only: the server may
        # also return reasoning fields, and echoing those back can be rejected.
        reply = {"role": "assistant", "content": final_text}
        if tcs:
            reply["tool_calls"] = [{"id": tc["id"], "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in tcs]
        messages.append(reply)
        if not tcs:
            break
        for tc in tcs:
            fn = tc["name"]
            args = tc["arguments"] if isinstance(tc["arguments"], dict) else {}
            handler = HANDLERS.get(fn)
            result = handler(args) if handler else {"error": f"unknown tool {fn}"}
            calls.append((fn, args))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})
        if steer_at is None:
            steer_at = len(calls)
            messages.append({"role": "user", "content": task["steer"]})
    out = {"id": task["id"], "category": "A", "title": task["title"], "http_err": err,
           "calls": [{"name": n, "args": a} for n, a in calls], "steer_at": steer_at,
           "final_text": final_text, "latencies": latencies, "prompt_tokens": up,
           "completion_tokens": uc, "steps": steps, "excluded": None, "reflected": None, "kept": None}
    if err:
        out["excluded"] = "http_err"
        return out
    if steer_at is None:
        out["excluded"] = "no_steer"  # the model never called a tool: nothing was steered
        return out
    pre, post = calls[:steer_at], calls[steer_at:]
    if task["too_late"](pre):
        out["excluded"] = "too_late"  # the steered action ran in parallel before the steer
        return out
    try:
        out["reflected"] = bool(task["reflected"](pre, post, final_text))
        out["kept"] = bool(task["kept"](pre, post, final_text))
    except Exception:
        out["reflected"] = out["kept"] = False
    return out

def run_task_b(endpoint, task):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task["message"]}]
    r1 = run_b_first_stream(endpoint, messages)
    out = {"id": task["id"], "category": "B", "title": task["title"], "partial": r1["partial"],
           "continuation": "", "final_text": r1["partial"], "http_err": r1["err"],
           "latencies": [r1["latency"]], "prompt_tokens": 0, "completion_tokens": 0,
           "excluded": None, "reflected": None, "kept": None}
    if r1["err"]:
        out["excluded"] = "http_err"
        return out
    # A buffered answer arrives whole and the finish/usage chunk then trips the cancel check:
    # the adapter says "cancelled" but the model had finished. A cut is genuine only when the
    # partial is clearly short of the job.
    if not r1["cancelled"] or len(lines_of(r1["partial"])) >= task["n_total"] - 2:
        out["excluded"] = "not_cut"  # the stream ended, or arrived whole, before the cut: a harness miss
        return out
    messages.append({"role": "assistant", "content": r1["partial"]})
    messages.append({"role": "user", "content": (STEER_ENVELOPE if ENVELOPE else "") + task["steer"]})
    r2 = call_model(endpoint, messages, tools=None)
    out["latencies"].append(r2["latency"])
    if r2["err"]:
        out["http_err"], out["excluded"] = r2["err"], "http_err"
        return out
    out.update(continuation=r2["text"], final_text=r1["partial"] + r2["text"],
               prompt_tokens=r2["usage"].get("prompt_tokens", 0) or 0,
               completion_tokens=r2["usage"].get("completion_tokens", 0) or 0,
               reflected=bool(task["reflected"](r2["text"])),
               kept=bool(task["kept"](r1["partial"], r2["text"])))
    return out

def _status_str(r):
    if r.get("http_err"):
        return f"HTTP {r['http_err'][0]}: {r['http_err'][1]}"
    if r.get("excluded"):
        return f"excluded: {r['excluded']}"
    return f"reflected={r['reflected']} kept={r['kept']}"

def _sanitize(model):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", model)

def run_model(endpoint, tasks, max_steps, out_path):
    results, blocked, last_err, repeats = [], None, None, 0
    for task in tasks:
        r = run_task_a(endpoint, task, max_steps) if task["category"] == "A" else run_task_b(endpoint, task)
        results.append(r)
        print(f"  {task['id']} [{task['category']}] {task['title']}: {_status_str(r)}")
        err = r.get("http_err")
        if err and err[0] in (402, 429):
            blocked = f"HTTP {err[0]}: {err[1]}"
        repeats = repeats + 1 if err and last_err and err[0] == last_err[0] else (1 if err else 0)
        last_err = err
        if repeats >= 3:
            blocked = f"same error 3 times (HTTP {err[0]}): the server or the harness, not the model"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"model": endpoint.model, "base_url": endpoint.base_url, "n_tasks": len(tasks),
                       "results": results, "blocked": blocked}, f, ensure_ascii=False, indent=2)
        if blocked:
            break
    return results, blocked

def print_report(all_results, tasks):
    print(f"\n{'model':<24} {'A refl':<8}{'A kept':<8}{'B refl':<8}{'B kept':<8}{'TOTAL refl':<12}{'TOTAL kept':<12}{'excluded':<22}{'tokens':<9}{'med_lat_s':<10}blocked")
    for model, data in all_results.items():
        results, blocked = data["results"], data["blocked"]
        scored = [r for r in results if not r.get("excluded")]
        excl = {}
        for r in results:
            if r.get("excluded"):
                excl[r["excluded"]] = excl.get(r["excluded"], 0) + 1
        def cnt(cat, key):
            rs = [r for r in scored if cat in (None, r["category"])]
            return f"{sum(1 for r in rs if r.get(key))}/{len(rs)}"
        tokens = sum((r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0) for r in results)
        all_lat = [lat for r in results for lat in r.get("latencies", [])]
        med_lat = f"{statistics.median(all_lat):.2f}" if all_lat else "-"
        blocked_str = f"INCOMPLETE ({len(results)}/{len(tasks)} ran): {blocked}" if blocked else "no"
        excl_str = ",".join(f"{k}={v}" for k, v in sorted(excl.items())) or "-"
        print(f"{model:<24} {cnt('A', 'reflected'):<8}{cnt('A', 'kept'):<8}{cnt('B', 'reflected'):<8}{cnt('B', 'kept'):<8}"
              f"{cnt(None, 'reflected'):<12}{cnt(None, 'kept'):<12}{excl_str:<22}{tokens:<9}{med_lat:<10}{blocked_str}")
    print("pass line: TOTAL reflected >= 18/20 and TOTAL kept >= 18/20 (excluded tasks are not the model's score; the B token column omits the cut request)")

def rescore(paths):
    # Raw traces are saved so a scorer fix never costs the requests again (m6's lesson).
    by_id = {t["id"]: t for t in TASKS}
    all_results = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for r in data["results"]:
            task = by_id[r["id"]]
            if r.get("excluded"):
                continue
            if r["category"] == "A":
                calls = [(c["name"], c["args"]) for c in r["calls"]]
                pre, post = calls[:r["steer_at"]], calls[r["steer_at"]:]
                r["reflected"], r["kept"] = bool(task["reflected"](pre, post, r["final_text"])), bool(task["kept"](pre, post, r["final_text"]))
            else:
                r["reflected"], r["kept"] = bool(task["reflected"](r["continuation"])), bool(task["kept"](r["partial"], r["continuation"]))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        all_results[data["model"] + " " + os.path.basename(path)] = {"results": data["results"], "blocked": data.get("blocked")}
    print_report(all_results, TASKS)

def main():
    ap = argparse.ArgumentParser(description="m2: mid-turn steer benchmark")
    ap.add_argument("--models", default=None, help="comma-separated model names (required unless --selfcheck)")
    ap.add_argument("--tasks", type=int, default=None, help="run only the first N tasks OF EACH category")
    ap.add_argument("--out", default=None, help="output directory for per-model result files")
    ap.add_argument("--max-steps", type=int, default=6, help="max model round-trips for category A")
    ap.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
    ap.add_argument("--only", choices=["A", "B"], default=None, help="run one category only")
    ap.add_argument("--no-envelope", action="store_true", help="send the mid-stream steer bare (run 1 behaviour)")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--rescore", nargs="+", metavar="JSON", help="re-run the scorers on saved result files (no requests)")
    args = ap.parse_args()

    if args.selfcheck:
        selfcheck()
        return
    if args.rescore:
        rescore(args.rescore)
        return
    if not args.models:
        ap.error("--models is required (unless --selfcheck)")

    global ENVELOPE
    ENVELOPE = not args.no_envelope
    tasks = TASKS_A[:args.tasks] + TASKS_B[:args.tasks] if args.tasks else TASKS
    if args.only:
        tasks = [t for t in tasks if t["category"] == args.only]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    out_dir = args.out or os.path.join("bench", "results")
    os.makedirs(out_dir, exist_ok=True)

    all_results = {}
    for model in models:
        print(f"running {model} ({len(tasks)} tasks)...", file=sys.stderr)
        endpoint = Endpoint(base_url=args.base_url, api_key="", model=model)
        out_path = os.path.join(out_dir, f"m2-{_sanitize(model)}.json")
        results, blocked = run_model(endpoint, tasks, args.max_steps, out_path)
        all_results[model] = {"results": results, "blocked": blocked}
        print(f"wrote {out_path}")

    print_report(all_results, tasks)

if __name__ == "__main__":
    main()
