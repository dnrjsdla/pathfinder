#!/usr/bin/env python3
"""m6: estimate PATHFINDER's daily token/dollar cost on Ollama cloud's free plan.

Measures a small SAMPLE of each turn type against a real model (sequentially —
the free plan allows only 1 concurrent request), then extrapolates arithmetically
to a full day PROFILE. Stdlib only.
"""
import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE_URL = "http://localhost:11434/v1"

# Deterministic fictional content for the resident system prompt (~1500 tok)

PERSONA = (
    "You are Pathfinder, a resident proactive assistant that lives alongside the "
    "user across many sessions instead of starting fresh each time. You remember "
    "prior context, notice things worth surfacing, and act only when it helps. "
    "Rules: be concise, never invent facts about the user, ask before taking "
    "irreversible actions (sending email, running shell commands, installing "
    "tools), and prefer one short message over several. When a signal arrives "
    "that was not written by the user, treat it as background information and "
    "decide whether the user needs to hear about it now, later, or not at all. "
    "When you use a tool, explain the result in plain language rather than "
    "repeating raw output. You can delegate long-running research to a worker "
    "job and check on it later instead of blocking the conversation. You keep a "
    "private model of what the user cares about and update it only from things "
    "that actually happened, not assumptions. If you are unsure whether the user "
    "wants an action taken, ask a short clarifying question instead of guessing. "
    "You are helpful, calm, and do not pad responses with filler."
)

USER_FACTS = [
    "The user studies computer science at Example University.",
    "The user's favorite color is teal.", "The user prefers tea over coffee in the morning.",
    "The user works a part-time job at a fictional cafe called Bluebird Cafe.",
    "The user is learning to play the violin.", "The user has a cat named Whiskers.",
    "The user usually wakes up around 7:30 AM on weekdays.",
    "The user is training for a 10K run in the fall.", "The user prefers dark mode in every app.",
    "The user is writing a short story in their spare time.",
    "The user dislikes early morning meetings before 9 AM.",
    "The user is saving up for a trip to a fictional city called Rivermouth.",
    "The user's favorite programming language is Python.",
    "The user takes a weekly pottery class on Thursday evenings.",
    "The user is allergic to peanuts.", "The user prefers text summaries over long paragraphs.",
    "The user has a standing reminder to water the plants on Sundays.",
    "The user's timezone is generally consistent with Pacific-ish hours.",
    "The user likes to review their calendar first thing in the morning.",
    "The user is part of a fictional book club called The Wandering Pages.",
    "The user prefers metric units.",
    "The user is mildly nostalgic about a fictional childhood town called Ashfield.",
    "The user keeps a running list of gift ideas for friends.",
    "The user likes concise weather reports without extra chit-chat.",
    "The user is currently learning basic conversational Spanish.",
]

WIKI_ADJ = ["Northern", "Hidden", "Quiet", "Amber", "Iron", "Velvet", "Coastal", "Winter", "Golden", "Silent"]
WIKI_NOUN = ["Garden", "Archive", "Bridge", "Market", "Lantern", "Harbor", "Forge", "Meadow", "Signal", "Vault"]

def build_wiki_pages():
    pages = []
    for i in range(40):
        adj = WIKI_ADJ[i % len(WIKI_ADJ)]
        noun = WIKI_NOUN[(i // len(WIKI_ADJ)) % len(WIKI_NOUN)]
        date = f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
        pages.append(f"{adj} {noun} (updated {date})")
    return pages

WIKI_PAGES = build_wiki_pages()

def build_system_prompt():
    lines = [PERSONA, "", "What I know about the user:"]
    lines += [f"- {f}" for f in USER_FACTS]
    lines += ["", "Wiki map (known pages):"]
    lines += [f"- {p}" for p in WIKI_PAGES]
    return "\n".join(lines)

SYSTEM_PROMPT = build_system_prompt()

JOB_SYSTEM_PROMPT = (
    "You are a background worker spawned by a resident assistant to complete one "
    "research goal, then stop. You have search and read tools that return short "
    "snippets. Work efficiently: search first, read the most promising results, "
    "then write a final summary directly to the user — no preamble, no "
    "apologies, no meta-commentary about being a worker. Cite candidate names "
    "plainly. If you cannot find enough information within a few tool calls, "
    "summarize what you found and say so honestly rather than inventing "
    "details. Keep the final summary under 150 words. Do not ask the user "
    "questions — you cannot see their replies until this job completes."
)

CONSOLIDATE_SYSTEM_PROMPT = (
    "You consolidate a resident assistant's memory of the user. You are given "
    "an excerpt of a day's event log. Output a JSON list of deltas, each "
    "shaped like {\"target\": <fact key>, \"op\": \"add\"|\"update\"|\"remove\", "
    "\"new\": <new value or null>, \"evidence\": <short quote from the log>}. "
    "Only propose a delta when the log gives clear evidence; do not "
    "speculate. Output JSON only, no prose before or after it."
)

ACTIONS = [
    "reviewed budget notes", "messaged a colleague", "updated the project board",
    "read a wiki page", "attended a meeting", "drafted an email",
    "closed a support ticket", "logged a workout", "planned tomorrow's tasks",
    "checked the weather",
]

def build_day_log(n_lines=170):
    lines = []
    for i in range(n_lines):
        hh = (8 + i // 10) % 24
        mm = (i * 7) % 60
        action = ACTIONS[i % len(ACTIONS)]
        topic = WIKI_PAGES[i % len(WIKI_PAGES)]
        lines.append(f"{hh:02d}:{mm:02d} - user {action} related to '{topic}'. (log entry {i + 1})")
    return "\n".join(lines)

# Tool schemas + deterministic mock handlers

TOOL_SPECS = [
    ("get_weather", "Get current weather for a location.", {"location": {"type": "string"}}, ["location"]),
    ("list_events", "List calendar events for a date.", {"date": {"type": "string"}}, ["date"]),
    ("add_event", "Add a calendar event.", {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}}, ["title", "start"]),
    ("add_reminder", "Add a reminder.", {"text": {"type": "string"}, "when": {"type": "string"}}, ["text", "when"]),
    ("search", "Search the web for a query.", {"query": {"type": "string"}}, ["query"]),
    ("read", "Read the contents of a URL.", {"url": {"type": "string"}}, ["url"]),
    ("run_shell", "Run a shell command in a sandbox.", {"command": {"type": "string"}}, ["command"]),
    ("start_job", "Start a background worker job.", {"goal": {"type": "string"}}, ["goal"]),
    ("job_status", "Check status of a background job.", {"job_id": {"type": "string"}}, ["job_id"]),
    ("send_email", "Send an email.", {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, ["to", "subject", "body"]),
    ("mcp_search_tools", "Search available MCP tool servers.", {"query": {"type": "string"}}, ["query"]),
    ("propose_tool_install", "Propose installing a new tool.", {"name": {"type": "string"}, "reason": {"type": "string"}}, ["name", "reason"]),
]

def build_tools():
    tools = []
    for name, desc, props, required in TOOL_SPECS:
        tools.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        }})
    return tools

TOOLS = build_tools()

def _snippet(seed):
    # ~300-token (~220 word) deterministic filler snippet for search/read mocks
    words = " ".join(f"topic{seed}-detail{i}" for i in range(220))
    return words

TOOL_HANDLERS = {
    "get_weather": lambda a: {"location": a.get("location", "Unknown"), "forecast": "Sunny, 22C", "humidity": 40},
    "list_events": lambda a: {"date": a.get("date", "today"), "events": [{"title": "Team sync", "time": "10:00"}]},
    "add_event": lambda a: {"status": "created", "id": "evt_001"},
    "add_reminder": lambda a: {"status": "created", "id": "rem_001"},
    "search": lambda a: {"query": a.get("query", ""), "results": [
        {"title": f"Result {i} for {a.get('query', '')}", "snippet": _snippet(i)} for i in range(3)
    ]},
    "read": lambda a: {"url": a.get("url", ""), "content": _snippet(hash(a.get("url", "")) % 7)},
    "run_shell": lambda a: {"stdout": "mock output", "exit_code": 0},
    "start_job": lambda a: {"job_id": "job_001", "status": "queued"},
    "job_status": lambda a: {"job_id": a.get("job_id", "job_001"), "status": "done"},
    "send_email": lambda a: {"status": "sent"},
    "mcp_search_tools": lambda a: {"tools": [{"name": "browser-mcp", "desc": "Browser automation MCP server"}]},
    "propose_tool_install": lambda a: {"status": "proposed"},
}

JOB_TOOLS = [t for t in TOOLS if t["function"]["name"] in ("search", "read")]

# Networking: streaming chat completion + SSE parsing

class BlockedError(Exception):
    pass

def parse_stream(lines, messages, tools, start=None):
    """Parse an iterable of raw SSE lines (bytes or str). Returns metrics dict.
    Split out from the network call so --selfcheck can exercise it offline.
    `start` is the moment the request was sent: the server holds the response
    headers until it has a first token, so a clock started here reads 0."""
    start = time.monotonic() if start is None else start
    ttft = None
    ttft_kind = None
    content_parts = []
    tool_calls_map = {}
    usage = None
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta", {}) or {}
            c = delta.get("content")
            if c:
                content_parts.append(c)
                if ttft is None:
                    ttft = time.monotonic() - start
                    ttft_kind = "content"
            reasoning = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
            if reasoning and ttft is None:
                ttft = time.monotonic() - start
                ttft_kind = "reasoning"
            tcs = delta.get("tool_calls")
            if tcs:
                if ttft is None:
                    ttft = time.monotonic() - start
                    ttft_kind = "tool_calls"
                for tc in tcs:
                    idx = tc.get("index", 0)
                    entry = tool_calls_map.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        entry["name"] += fn["name"]
                    if fn.get("arguments"):
                        entry["arguments"] += fn["arguments"]
        if chunk.get("usage"):
            usage = chunk["usage"]
    total_time = time.monotonic() - start
    content = "".join(content_parts)
    tool_calls = [dict(index=i, **v) for i, v in sorted(tool_calls_map.items())]

    if usage:
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        usage_estimated = False
    else:
        usage_estimated = True
        # ponytail: rough len/4 estimate, ignores tokenizer specifics; upgrade
        # path is a real tokenizer if this ever needs to be precise.
        prompt_len = len(json.dumps(messages)) + (len(json.dumps(tools)) if tools else 0)
        prompt_tokens = max(1, prompt_len // 4)
        completion_tokens = max(0, len(content) // 4)
        cached_tokens = 0

    return {
        "content": content, "tool_calls": tool_calls,
        "ttft": ttft if ttft is not None else total_time, "ttft_kind": ttft_kind or "none",
        "total_time": total_time, "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens, "cached_tokens": cached_tokens,
        "usage_estimated": usage_estimated,
    }

def post_stream(base_url, model, messages, tools=None, timeout=120):
    body = {"model": model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    start = time.monotonic()
    try:
        # `with` releases the socket: the free plan allows one concurrent request, and a
        # lingering connection can get the next request queued or refused.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse_stream(resp, messages, tools, start=start)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")[:200]
        raise BlockedError(f"HTTP {e.code}: {msg}")

def run_tool_calls(tool_calls):
    msgs = []
    for tc in tool_calls:
        name = tc["name"]
        try:
            args = json.loads(tc["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        handler = TOOL_HANDLERS.get(name)
        result = handler(args) if handler else {"error": f"unknown tool {name}"}
        msgs.append({"role": "tool", "tool_call_id": tc.get("id") or f"call_{name}",
                     "name": name, "content": json.dumps(result)})
    return msgs

def run_turn(base_url, model, messages, tools, max_steps):
    """One agent-loop turn. Mutates `messages` in place (so callers can keep a
    growing shared history across turns). Returns aggregate metrics."""
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
              "requests": 0, "total_time": 0.0}
    ttft = None
    ttft_kind = None
    usage_estimated = False
    for step in range(max_steps):
        resp = post_stream(base_url, model, messages, tools=tools)
        totals["prompt_tokens"] += resp["prompt_tokens"]
        totals["completion_tokens"] += resp["completion_tokens"]
        totals["cached_tokens"] += resp["cached_tokens"]
        totals["requests"] += 1
        totals.setdefault("first_prompt_tokens", resp["prompt_tokens"])
        totals["total_time"] += resp["total_time"]
        usage_estimated = usage_estimated or resp["usage_estimated"]
        if ttft is None:
            ttft, ttft_kind = resp["ttft"], resp["ttft_kind"]
        assistant_msg = {"role": "assistant", "content": resp["content"]}
        if resp["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {"id": tc.get("id") or f"call_{step}_{i}", "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for i, tc in enumerate(resp["tool_calls"])
            ]
        messages.append(assistant_msg)
        if not resp["tool_calls"]:
            break
        tool_msgs = run_tool_calls(resp["tool_calls"])
        for m, tc in zip(tool_msgs, assistant_msg["tool_calls"]):
            m["tool_call_id"] = tc["id"]
        messages.extend(tool_msgs)
    totals["ttft"] = ttft
    totals["ttft_kind"] = ttft_kind
    totals["usage_estimated"] = usage_estimated
    return totals

# Per-turn-type sampling

PLAIN_MSGS = ["How's my day looking so far?", "Anything interesting happen recently?", "Just checking in, how are things?"]
ONE_TOOL_MSGS = ["What's the weather like right now?", "What's on my calendar today?"]
TWO_TOOL_MSGS = [
    "Check the weather and then add a reminder to bring an umbrella if it's raining.",
    "Look at today's events and add a reminder for the next one.",
]

SIGNAL_KINDS = {
    "job_finished": {"job_id": "job_001", "result": "3 candidates found"},
    "reminder_due": {"text": "bring umbrella", "when": "18:00"},
    "calendar_soon": {"title": "Team sync", "starts_in_min": 10},
    "email_received": {"from": "noreply@example.com", "subject": "Weekly digest"},
    "weather_alert": {"condition": "rain", "severity": "moderate"},
}

JOB_GOALS = [
    "research which MCP servers exist for browser automation and summarize 3 candidates",
    "research which MCP servers exist for note-taking and summarize 3 candidates",
]

def sample_user_turns(base_url, model, n):
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    out = []
    for i in range(n):
        bucket = i % 3
        pool = (PLAIN_MSGS, ONE_TOOL_MSGS, TWO_TOOL_MSGS)[bucket]
        msg = pool[(i // 3) % len(pool)]
        history.append({"role": "user", "content": msg})
        m = run_turn(base_url, model, history, TOOLS, max_steps=4)
        m["turn_index"] = i + 1
        out.append(m)
    return out

def sample_signal_turns(base_url, model, n):
    kinds = list(SIGNAL_KINDS)
    out = []
    for i in range(n):
        kind = kinds[i % len(kinds)]
        envelope = f"[signal — not written by the user] kind={kind} fields={json.dumps(SIGNAL_KINDS[kind])}"
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": envelope}]
        out.append(run_turn(base_url, model, messages, TOOLS, max_steps=4))
    return out

def sample_job_turns(base_url, model, n):
    out = []
    for i in range(n):
        goal = JOB_GOALS[i % len(JOB_GOALS)]
        messages = [{"role": "system", "content": JOB_SYSTEM_PROMPT}, {"role": "user", "content": f"Goal: {goal}"}]
        out.append(run_turn(base_url, model, messages, JOB_TOOLS, max_steps=5))
    return out

def sample_consolidate_turns(base_url, model, n):
    day_log = build_day_log()
    out = []
    for i in range(n):
        user_msg = f"Day log excerpt:\n{day_log}\n\nProduce the JSON list of deltas as instructed."
        messages = [{"role": "system", "content": CONSOLIDATE_SYSTEM_PROMPT}, {"role": "user", "content": user_msg}]
        out.append(run_turn(base_url, model, messages, None, max_steps=1))
    return out

SAMPLERS = {"user": sample_user_turns, "signal": sample_signal_turns,
            "job": sample_job_turns, "consolidate": sample_consolidate_turns}

# Stats + extrapolation

def linear_fit(xs, ys):
    """Least squares by hand: y = a + b*x."""
    n = len(xs)
    if n < 2:
        return (ys[0] if ys else 0.0), 0.0
    xbar, ybar = sum(xs) / n, sum(ys) / n
    num = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    den = sum((x - xbar) ** 2 for x in xs)
    b = num / den if den else 0.0
    a = ybar - b * xbar
    return a, b

def extrapolate_user_prompt_tokens(sample_metrics, profile_n, reset_every=30):
    """User-turn prompt tokens grow with a resident's history, so a flat mean
    would undercount a long day. Fit prompt_tokens as a linear function of
    turn index from the sample, then sum it over the profile, resetting the
    index every `reset_every` turns (as if the session restarts there)."""
    # Fit on the first request of each turn: a turn with tool calls sends several
    # requests, and mixing those totals into the series made the slope negative.
    xs = [m["turn_index"] for m in sample_metrics]
    ys = [m.get("first_prompt_tokens", m["prompt_tokens"]) for m in sample_metrics]
    a, b = linear_fit(xs, ys)
    if b < 0:  # history cannot shrink; a negative slope is sampling noise
        a, b = sum(ys) / len(ys), 0.0
    # Later requests inside a turn carry the tool results too, so they are LARGER than the
    # first one. Add their measured mean instead of multiplying the first request's size,
    # which undercounts (the dangerous direction).
    extra = sum(m["prompt_tokens"] - y for m, y in zip(sample_metrics, ys)) / len(ys)
    total = sum(a + b * ((i - 1) % reset_every + 1) for i in range(1, profile_n + 1)) + extra * profile_n
    return total, a, b

def summarize_type(type_name, metrics_list, profile_n):
    if not metrics_list:
        return None
    mean_prompt = statistics.mean(m["prompt_tokens"] for m in metrics_list)
    mean_cached = statistics.mean(m["cached_tokens"] for m in metrics_list)
    mean_completion = statistics.mean(m["completion_tokens"] for m in metrics_list)
    mean_requests = statistics.mean(m["requests"] for m in metrics_list)
    ttft_vals = [m["ttft"] for m in metrics_list if m["ttft"] is not None]
    median_ttft = statistics.median(ttft_vals) if ttft_vals else 0.0
    median_time = statistics.median(m["total_time"] for m in metrics_list)
    fit_a = fit_b = None
    if type_name == "user":
        day_prompt_tokens, fit_a, fit_b = extrapolate_user_prompt_tokens(metrics_list, profile_n)
    else:
        day_prompt_tokens = mean_prompt * profile_n
    # Cached share of the prompt, not a flat mean: it must scale with the fitted prompt.
    total_prompt = sum(m["prompt_tokens"] for m in metrics_list)
    cached_share = sum(m["cached_tokens"] for m in metrics_list) / total_prompt if total_prompt else 0.0
    day_cached_tokens = day_prompt_tokens * cached_share
    day_completion_tokens = mean_completion * profile_n
    return {
        "sample_n": len(metrics_list), "profile_n": profile_n,
        "mean_prompt_tokens": mean_prompt, "mean_cached_tokens": mean_cached,
        "mean_completion_tokens": mean_completion, "mean_requests": mean_requests,
        "median_ttft_s": median_ttft, "median_total_latency_s": median_time,
        "day_prompt_tokens": day_prompt_tokens, "day_cached_tokens": day_cached_tokens,
        "day_completion_tokens": day_completion_tokens,
        "fit_a": fit_a, "fit_b": fit_b,
        "usage_estimated_any": any(m["usage_estimated"] for m in metrics_list),
    }

def cost_for_type(summary, price):
    if not summary or not price:
        return None
    in_rate, cached_rate, out_rate = price
    billable_prompt = summary["day_prompt_tokens"] - summary["day_cached_tokens"]
    cost = (billable_prompt * in_rate + summary["day_cached_tokens"] * cached_rate +
            summary["day_completion_tokens"] * out_rate) / 1_000_000
    return cost

# Self-check (no network)

def selfcheck():
    canned = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":" world"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,'
        '"prompt_tokens_details":{"cached_tokens":4}}}',
        'data: [DONE]',
    ]
    result = parse_stream(canned, messages=[{"role": "user", "content": "hi"}], tools=None)
    assert result["content"] == "Hello world"
    assert result["prompt_tokens"] == 12
    assert result["completion_tokens"] == 3
    assert result["cached_tokens"] == 4
    assert result["usage_estimated"] is False
    assert result["ttft_kind"] == "content"
    assert result["ttft"] is not None

    # toy series: prompt tokens grow exactly 10 tokens/turn from a=0
    xs, ys = [1, 2, 3, 4], [10, 20, 30, 40]
    a, b = linear_fit(xs, ys)
    assert abs(a) < 1e-9 and abs(b - 10) < 1e-9
    toy_metrics = [{"turn_index": x, "prompt_tokens": y} for x, y in zip(xs, ys)]
    total, fa, fb = extrapolate_user_prompt_tokens(toy_metrics, profile_n=5, reset_every=30)
    assert abs(total - 150) < 1e-6  # 10+20+30+40+50
    # turns with a second, larger request: first request 10,20,30,40 and 15 more tokens each turn
    multi = [{"turn_index": x, "first_prompt_tokens": y, "prompt_tokens": y + 15} for x, y in zip(xs, ys)]
    total, _, _ = extrapolate_user_prompt_tokens(multi, profile_n=5, reset_every=30)
    assert abs(total - (150 + 15 * 5)) < 1e-6
    # noisy sample with a negative slope must not extrapolate below the sample mean
    noisy = [{"turn_index": 1, "prompt_tokens": 40}, {"turn_index": 2, "prompt_tokens": 20}]
    total, _, fb = extrapolate_user_prompt_tokens(noisy, profile_n=30, reset_every=30)
    assert fb == 0.0 and abs(total - 30 * 30) < 1e-6

    print("selfcheck: OK")

# CLI / orchestration

def parse_counts(s):
    d = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        k, v = part.split("=", 1)
        d[k.strip()] = int(v)
    return d

def parse_prices(s):
    prices = {}
    if not s:
        return prices
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        model, rates = part.split("=", 1)
        vals = tuple(float(x) for x in rates.split(","))
        prices[model.strip()] = vals
    return prices

def default_out_path():
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"bench/results/m6-{ts}.json"

def run_model(base_url, model, sample_counts, profile_counts):
    """Returns (per_type summaries, raw per-turn metrics, error message or None).
    Raw metrics are kept so the extrapolation formula can be corrected later without
    paying for the requests again; an error keeps whatever types already finished."""
    per_type, raw, error = {}, {}, None
    for t, sampler in SAMPLERS.items():
        n = sample_counts.get(t, 0)
        if n <= 0:
            continue
        try:
            raw[t] = sampler(base_url, model, n)
        except (BlockedError, urllib.error.URLError, TimeoutError) as e:
            error = f"{t}: {e}"
            break
        per_type[t] = summarize_type(t, raw[t], profile_counts.get(t, 0))
    return per_type, raw, error

def print_report(model, per_type, price):
    print(f"\n=== {model} ===")
    day_tokens_total = 0.0
    day_cost_total = 0.0
    user_ttft = None
    for t, s in per_type.items():
        if not s:
            continue
        day_tokens = s["day_prompt_tokens"] + s["day_completion_tokens"]
        day_tokens_total += day_tokens
        cost = cost_for_type(s, price)
        cost_str = f"${cost:.4f}/day" if cost is not None else "(no price)"
        print(f"  {t:12s} sample={s['sample_n']:>2} mean_prompt={s['mean_prompt_tokens']:.0f} "
              f"mean_cached={s['mean_cached_tokens']:.0f} mean_completion={s['mean_completion_tokens']:.0f} "
              f"mean_req={s['mean_requests']:.1f} ttft_med={s['median_ttft_s']:.2f}s "
              f"lat_med={s['median_total_latency_s']:.2f}s day_tokens={day_tokens:.0f} {cost_str}")
        if cost is not None:
            day_cost_total += cost
        if t == "user":
            user_ttft = s["median_ttft_s"]
    cost_30d = f"${day_cost_total * 30:.2f}" if price else "n/a"
    cost_day = f"${day_cost_total:.4f}" if price else "n/a"
    ttft_str = f"{user_ttft:.2f}s" if user_ttft is not None else "n/a"
    print(f"SUMMARY {model}: tokens/day={day_tokens_total:.0f} $/day={cost_day} "
          f"$/30d={cost_30d} median_ttft_user={ttft_str}")

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models")
    p.add_argument("--sample", default="user=6,signal=5,job=1,consolidate=1")
    p.add_argument("--profile", default="user=30,signal=20,job=5,consolidate=2")
    p.add_argument("--prices", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--selfcheck", action="store_true")
    args = p.parse_args()

    if args.selfcheck:
        selfcheck()
        return

    if not args.models:
        p.error("--models is required (unless --selfcheck)")

    base_url = os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    sample_counts = parse_counts(args.sample)
    profile_counts = parse_counts(args.profile)
    prices = parse_prices(args.prices)
    out_path = args.out or default_out_path()

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    all_results = {}
    for model in models:
        print(f"Running {model} ...")
        per_type, raw, error = run_model(base_url, model, sample_counts, profile_counts)
        all_results[model] = {"blocked": bool(error), "message": error, "per_type": per_type,
                              "raw_turns": raw, "sample": sample_counts, "profile": profile_counts,
                              "price_per_mtok": prices.get(model)}
        with open(out_path, "w", encoding="utf-8") as f:  # after every model, not at the end
            json.dump(all_results, f, indent=2)
        if error:
            print(f"  INCOMPLETE - stopped at {error}")
        print_report(model, per_type, prices.get(model))
        if any(s and s["usage_estimated_any"] for s in per_type.values()):
            print("  WARNING: the server sent no usage for some requests; those token counts are len/4 estimates")
    print(f"\nResults written to {out_path}")

if __name__ == "__main__":
    main()
