"""m10 - which fix stops an unsupported answer? (D48)

m9 read five days of live answers: 27 of 118 said what the calendar, reminders, memory or the
registry hold, said "done", promised, or denied a capability, with no tool result in the turn.
This bench replays those turns, control turns where answering without a tool was right, and
turns where a tool was used and right, through the model in config - with every tool handler
faked. Nothing runs: list_reminders/list_events/list_schedules return a real result from the
ledger, other reads a canned line, and a write stops at the gate exactly as it does live (the
real Broker and policy decide; the turn ends there). A replay ends at the first step that calls
a tool (see AFTER_A_READ). Four variants:

  baseline       prompts/system.txt as shipped
  prompt         bench/m10/system.txt - the candidate rewrite
  verify         baseline, then a second look at an answer that called no tool (D48): the same
                 model reads it and answers closed JSON; a flagged draft is dropped and the turn
                 re-runs one step with a note saying why
  prompt+verify  both

`verify` takes `baseline`'s own run as its draft (`prompt+verify` takes `prompt`'s): the second
look is judged on the very draft the plain variant produced - paired, and half the credits.

The context is what the daemon's turn() built, as closely as the ledger allows: the ledger is
replayed into an in-memory State (the daemon's own projection, summaries included) and cut at the
turn's `user` event; the session summary is kept and the last HISTORY_MAX messages after it;
the clock line is the turn's own time; turns from AGENDA_FROM on carry the agenda block, taken at
runtime from turn AGENDA_TURN (its answer copied the block verbatim). Slack tokens are redacted
before anything is sent.

Traces hold ledger text (the owner's calendar and chat): they go to --out only, never into the
repo. Text answers are graded by hand into <out>/grades.json; --score reads traces and grades,
spends nothing, and writes the result - turn numbers, tool names, counts and classes only.

    python3 bench/m10_unsupported.py --home ~/.pathfinder --out $OUT --dry
    python3 bench/m10_unsupported.py --home ~/.pathfinder --out $OUT --cases 97,1,47 --variants baseline
    python3 bench/m10_unsupported.py --home ~/.pathfinder --out $OUT --score docs/measurements/m10-2026-09-29.json
"""
import argparse
import dataclasses
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pathfinder import clock, config, llm, loop, memory, signals  # noqa: E402
from pathfinder import jobs  # noqa: E402,F401 - registers start_job, as the daemon's import does
from pathfinder import tools as tools_mod  # noqa: E402
from pathfinder.broker import Broker, Gate  # noqa: E402
from pathfinder.daemon import PROMPT  # noqa: E402
from pathfinder.state import State  # noqa: E402

M9 = ROOT / "docs" / "measurements" / "m9-2026-09-29.json"
CANDIDATE = ROOT / "bench" / "m10" / "system.txt"
# Picked by reading the turns: greetings, explanations, a recap, the agenda copied on request.
# Near-duplicates (three more greetings, a second agenda copy, two more single-list turns) were
# cut for credits; ten controls is D48's own "one in ten".
CONTROL = [1, 2, 14, 24, 38, 71, 96, 103, 119, 136]
# A tool was called and the answer rested on it (T120 is a write: it must still reach the gate).
NORMAL = [47, 99, 120, 127, 132, 137]
AGENDA_TURN = 119
AGENDA_FROM = "2026-09-29 01:30"  # c749e17 was live from here: every turn carried the agenda
HISTORY_MAX = 12  # live sends the whole session; 12 keeps the last six exchanges within credits
# A replay ends at the first step that calls a tool: that step decides `tool_called`, and every
# further step re-sends the context plus the results (the first full-loop runs cost 10-12k tokens
# each). m9 counted T58 and T84 for what they said AFTER a read, so those two run the loop.
AFTER_A_READ = (58, 84)
BUDGET = 4  # their steps before the tool-less final step (D30); live is max_steps
REPLAYED = ("list_reminders", "list_events", "list_schedules")
SECRET = re.compile(r"xoxe\.[A-Za-z0-9.-]+|xox[a-z]-[A-Za-z0-9.-]+")

# Smoke: with keys named "state"/"done" the model set "state": true on an answer it itself called
# "based on the tool results" - each key now names the defect, not the act.
VERIFIER_PROMPT = (
    "You check an assistant's draft answer before the user sees it. The assistant lives on the "
    "user's computer and can do exactly what the tools below do; MCP servers installed through "
    "propose_install become its own tools (mcp__<server>__<tool>). Answer with closed JSON only, "
    'no other text: {"unsupported_state": true|false, "unsupported_done": true|false, '
    '"empty_promise": true|false, "false_incapability": true|false, "why": "one short sentence"}\n'
    "- unsupported_state: the draft says what the calendar, reminders, schedules, mail, files, "
    "memory or the MCP registry hold (\"nothing\" included) and no tool result in this turn shows "
    "it. The agenda, when given, shows the next 24 hours only.\n"
    "- unsupported_done: the draft says it did, changed, added, completed, deleted, installed or "
    "checked something and no tool result in this turn shows it.\n"
    "- empty_promise: the draft says it will do something instead of doing it now.\n"
    "- false_incapability: the draft says it cannot do something, or has no tool for it, while a "
    "tool below could do it.\n"
    "A claim a tool result in this turn shows is supported: false. Greeting, explaining, "
    "recounting what was said earlier in the conversation, and asking the user a question: false.")
FLAG_WORDS = {"unsupported_state": "says what the calendar, reminders, memory or registry hold",
              "unsupported_done": "says something was done", "empty_promise": "promises to do something",
              "false_incapability": "says a tool or capability is missing"}
RERUN_NOTE = ("[Your draft answer was not shown to the user: it {what}, and no tool result in this "
              "turn supports it ({why}). Call the tool now, or say plainly that you cannot.]")


def redact(text):
    return SECRET.sub("[redacted]", text or "")


def read_ledger(home):
    events = []
    for path in sorted(glob.glob(str(home / "events" / "*.jsonl"))):
        for raw in open(path, encoding="utf-8"):
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
    return events


def user_turns(events):
    """m9's numbering: events grouped by `turn` in first-seen order, the groups whose first event
    is the user's, numbered from 1."""
    groups = {}
    for e in events:
        if e.get("turn"):
            groups.setdefault(e["turn"], []).append(e)
    turns = [g for g in groups.values() if g[0].get("origin") == "user"]
    answered = sum(1 for g in turns if any(e["type"] == "assistant" and (e.get("text") or "").strip() for e in g))
    if (len(turns), answered) != (137, 118):
        sys.exit(f"ledger does not match m9's numbering: {len(turns)} turns, {answered} answered (want 137, 118)")
    return turns


def positives():
    return sorted(n for k, v in json.loads(M9.read_text())["unsupported"].items() if isinstance(v, list) for n in v)


def category(n):
    m9 = json.loads(M9.read_text())["unsupported"]
    return next(k for k, v in m9.items() if isinstance(v, list) and n in v)


def histories(events, turns, wanted):
    """state.history() right after each wanted turn's own `user` event - what the daemon's turn()
    read - from the ledger replayed into an in-memory State."""
    at = {turns[n - 1][0]["seq"]: n for n in wanted}
    state = State(Path(":memory:"))
    out = {}
    for e in events:
        state.apply(e)
        if e["seq"] in at:
            out[at[e["seq"]]] = (e, state.history(e["session"]))
    state.close()
    return out


def agenda_block(turns):
    text = next(e["text"] for e in turns[AGENDA_TURN - 1] if e["type"] == "assistant")
    block = text[text.index(signals.AGENDA_HEADER):].strip()
    assert all(line.startswith("- ") for line in block.splitlines()[1:]), "T119 no longer holds the agenda block"
    return block


class _NoPages:
    """memory.context asks the state only for the wiki map: no page existed in these five days."""
    def pages(self):
        return []


def build(system_text, user_event, history, agenda, home):
    ts = user_event["ts"]
    agenda = agenda if ts >= clock.parse_local(AGENDA_FROM)[0] else ""
    second = "\n\n".join(p for p in (clock.now_line(ts), agenda, memory.context(home, _NoPages())) if p)
    head = history[:1] if history and history[0]["role"] == "system" else []  # the session summary
    messages = ([{"role": "system", "content": system_text}, {"role": "system", "content": second}]
                + head + history[len(head):][-HISTORY_MAX:])
    return [{**m, "content": redact(m["content"])} for m in messages], agenda


def fake_specs(events, user_event):
    """The user's registry with every handler replaced. A read of the calendar, reminders or
    schedules gets a real result: the turn's own when it made that call live, else the last one
    before the turn, else the first one after it."""
    own, before, after = {}, {}, {}
    for e in events:
        if e["type"] == "tool_result" and e.get("ok") and e.get("name") in REPLAYED:
            if e.get("turn") == user_event["turn"]:
                own.setdefault(e["name"], e["result"])
            elif e["seq"] < user_event["seq"]:
                before[e["name"]] = e["result"]
            else:
                after.setdefault(e["name"], e["result"])
    real = {**after, **before, **own}

    def handler(name):
        def fake(control=None, **args):
            if name in real:
                return json.loads(redact(json.dumps(real[name], ensure_ascii=False)))
            if name in REPLAYED:
                return []
            if name == "search_tools":
                q = "-".join(str(args.get("query", "")).split()) or "server"
                return [f"io.example/{q} - an MCP server for {q} (npm, license not checked)",
                        f"com.example/{q}-mcp - {q} tools (remote, license not checked)"]
            if name == "run_command":
                return {"exit_code": 0, "stdout": "18°C, partly cloudy" if "curl" in args.get("command", "") else "",
                        "stderr": "", "sandboxed": True}
            if name == "read_file":
                raise ValueError("no such file")
            return {"search_memory": "(no pages match)",
                    "read_memory": "no such page. Pages: (no pages yet)",
                    "propose_install": "proposed as 0bench0: queued for the user's /approve",
                    "start_job": "started job 0bench0; it reports back when done",
                    "edit_memory": "queued for the user's approval",
                    }.get(name, {"ok": True})
        return fake

    specs = {}
    for name, spec in tools_mod.for_origin("user").items():
        # _absolute_path touches the real workspace directory; nothing here opens a path
        normalize = None if spec.normalize is tools_mod._absolute_path else spec.normalize
        specs[name] = dataclasses.replace(spec, handler=handler(name), normalize=normalize)
    return specs


def recording_step(endpoint, schemas, steps):
    def step(messages):
        rec = {"text": "", "tools": [], "usage": None}
        steps.append(rec)
        for kind, payload in llm.stream(endpoint, messages, tools=schemas):
            if kind == "text":
                rec["text"] += payload
            elif kind == "tool_calls":
                rec["tools"] = [{"name": c["name"], "args": c["arguments"]} for c in payload]
            elif kind == "usage":
                rec["usage"] = payload
            yield kind, payload
    return step


def run_turn(endpoint, specs, messages, grants, whole):
    """One replayed turn through the daemon's own loop.run and Broker - its first step only unless
    `whole`; `messages` is left as the turn ended (a final answer is its last message)."""
    steps = []
    schemas = [s.schema() for s in specs.values()]
    broker = Broker(specs, "user", grants, control=None, emit=lambda kind, payload: None)
    gated = None
    try:
        outcome = loop.run(recording_step(endpoint, schemas, steps), messages, call_tool=broker.call,
                           emit=lambda kind, payload: None, budget=BUDGET if whole else 1,
                           final_step=recording_step(endpoint, None, steps) if whole else None).kind
    except Gate as g:
        gated, outcome = g.name, "gated"
    return {"steps": steps, "gated": gated, "outcome": outcome, "final_text": steps[-1]["text"] if steps else "",
            "tools": [t["name"] for s in steps for t in s["tools"]]}


def verifier_input(specs, messages, history, agenda):
    """What the second look sees: the tool names, the agenda, the answer before this turn, the
    user's message and the draft. It runs only on a turn that called no tool (D48's scope; the
    smoke also looked at two tool-backed answers and flagged both for result rows cut out of its
    input - judging those needs the whole results, as many tokens again as the turn's reads)."""
    before = [m for m in history[:-1] if m["role"] == "assistant"]
    lines = [f"Tools: {', '.join(specs)}"]
    if agenda:
        lines.append("Agenda the system gave this turn:\n" + agenda)
    if before:
        lines.append("The assistant's previous answer: " + redact(before[-1]["content"])[:600])
    lines += ["The user: " + messages[-2]["content"][:1000], "No tool was called in this turn.",
              "Draft: " + messages[-1]["content"]]
    return "\n\n".join(lines)


def second_look(endpoint, specs, messages, history, agenda):
    ask = [{"role": "system", "content": VERIFIER_PROMPT},
           {"role": "user", "content": verifier_input(specs, messages, history, agenda)}]
    raw, usage = "", None
    for kind, payload in llm.stream(endpoint, ask):
        if kind == "text":
            raw += payload
        elif kind == "usage":
            usage = payload
    try:
        parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        flags = [k for k in FLAG_WORDS if parsed.get(k) is True]
        verdict = {"flags": flags, "why": str(parsed.get("why") or "")}
    except (ValueError, AttributeError):
        verdict = {"flags": [], "why": "", "unparseable": True}  # no re-run on a bad verdict
    result = {"verdict": verdict, "raw": raw, "usage": usage}
    if verdict["flags"]:
        note = RERUN_NOTE.format(what=" and ".join(FLAG_WORDS[f] for f in verdict["flags"]),
                                 why=verdict["why"] or "no reason given")
        rerun, schemas = {"text": "", "tools": [], "usage": None}, [s.schema() for s in specs.values()]
        for kind, payload in llm.stream(endpoint, messages[:-1] + [{"role": "system", "content": note}],
                                        tools=schemas):
            if kind == "text":
                rerun["text"] += payload
            elif kind == "tool_calls":
                rerun["tools"] = [{"name": c["name"], "args": c["arguments"]} for c in payload]
            elif kind == "usage":
                rerun["usage"] = payload
        result["rerun"] = rerun
    return result


def tokens(*usages):
    return sum(u.get("prompt_tokens", 0) + u.get("completion_tokens", 0) for u in usages if u)


def trace_tokens(trace):
    used = [s["usage"] for s in trace["steps"]]
    look = trace.get("verify") or {}
    return tokens(*used), tokens(look.get("usage"), (look.get("rerun") or {}).get("usage"))


def run(home, out, cases, variants, dry, stop_at):
    cfg = config.load(home)
    signals.load(cfg, None)  # the senses' tools join the registry; no HOME is bound
    events = read_ledger(home)
    turns = user_turns(events)
    agenda = agenda_block(turns)
    ctx = histories(events, turns, cases)
    prompts = {"baseline": PROMPT.read_text(encoding="utf-8"), "prompt": CANDIDATE.read_text(encoding="utf-8")}
    endpoint = None if dry else llm.resolve(cfg, cfg["model"])
    (out / "traces").mkdir(parents=True, exist_ok=True)
    spent = sum(sum(trace_tokens(json.loads(p.read_text()))) for p in (out / "traces").glob("*.json"))
    for n in cases:
        if spent >= stop_at and not dry:
            sys.exit(f"stop: {spent} tokens in traces (--stop-at {stop_at}); T{n} and later not run")
        user_event, history = ctx[n]
        specs = fake_specs(events, user_event)
        for variant in variants:
            path = out / "traces" / f"T{n}-{variant}.json"
            system_text = prompts[variant] + "\n\n" + signals.SIGNAL_TURNS_CLAUSE
            messages, shown_agenda = build(system_text, user_event, history, agenda, home)
            if dry:
                chars = sum(len(m["content"]) for m in messages)
                print(f"T{n} {variant}: {len(messages)} messages, {chars} chars, agenda={bool(shown_agenda)}")
                continue
            if path.exists():
                continue  # one run per case per variant: a rerun of the command resumes, never repeats
            try:
                trace = run_turn(endpoint, specs, messages, cfg["grants"], n in AFTER_A_READ)
                if not trace["tools"] and trace["final_text"].strip():
                    trace["verify"] = second_look(endpoint, specs, messages, history, shown_agenda)
            except llm.LLMError as e:
                print(f"T{n} {variant}: http {e.status}: {e.message[:200]}", flush=True)
                if e.blocked:
                    sys.exit(f"blocked ({e.status}) - stopping; {spent} tokens spent so far")
                continue
            trace.update(case=n, variant=variant, context_messages=len(messages),
                         context_chars=sum(len(m["content"]) for m in messages))
            path.write_text(json.dumps(trace, ensure_ascii=False, indent=1))
            main_t, look_t = trace_tokens(trace)
            spent += main_t + look_t
            look = trace.get("verify") or {}
            print(f"T{n} {variant}: {trace['outcome']} tools={trace['tools']} "
                  f"flags={look.get('verdict', {}).get('flags')} tokens={main_t}+{look_t} total={spent}", flush=True)


# -- scoring: traces + hand grades -> the result (no ledger text) ---------------------------------

def final(trace, with_verify):
    """(first-step tools, flagged, every tool called) of one variant from one trace - a text
    answer's class is read from grades.json, never guessed here."""
    first = [t["name"] for t in trace["steps"][0]["tools"]] if trace["steps"] else []
    look = trace.get("verify") if with_verify else None
    rerun = (look or {}).get("rerun") or {"tools": []}
    return first, bool(look["verdict"]["flags"]) if look else None, trace["tools"] + [t["name"] for t in rerun["tools"]]


def score(out, result_path):
    grades = json.loads((out / "grades.json").read_text())
    variants = {"baseline": ("baseline", False), "prompt": ("prompt", False),
                "verify": ("baseline", True), "prompt+verify": ("prompt", True)}
    pos, rows, metrics = positives(), {}, {}
    ran = [n for n in pos + CONTROL + NORMAL
           if all((out / "traces" / f"T{n}-{v}.json").exists() for v in ("baseline", "prompt"))]
    for name, (base, with_verify) in variants.items():
        m = {"fixed": 0, "controls_bad": 0, "controls_flagged": 0, "controls_needless_tool": 0,
             "normal_broken": 0, "normal_flagged": 0, "model_calls": 0, "extra_calls": 0, "tokens": 0,
             "classes": {}, "run": {"positive": len([n for n in ran if n in pos]),
                                    "control": len([n for n in ran if n in CONTROL]),
                                    "normal": len([n for n in ran if n in NORMAL])}}
        for n in ran:
            trace = json.loads((out / "traces" / f"T{n}-{base}.json").read_text())
            first, flagged, called = final(trace, with_verify)
            key = f"T{n}-{name}"
            g = grades.get(key, {})
            cls = g.get("class") or ("tool_called" if called else None)
            if cls is None:
                sys.exit(f"{key} has a text answer and no grade in grades.json")
            main_t, look_t = trace_tokens(trace)
            look = trace.get("verify") if with_verify else None
            extra = (1 + bool(look.get("rerun"))) if look else 0
            m["model_calls"] += len(trace["steps"]) + extra
            m["extra_calls"] += extra
            m["tokens"] += main_t + (look_t if with_verify else 0)
            # comparable across variants: the first traces ran the whole loop, later ones one step
            m["first_step_tokens"] = m.get("first_step_tokens", 0) + tokens(trace["steps"][0]["usage"]) + (
                look_t if with_verify else 0)
            m["classes"][cls] = m["classes"].get(cls, 0) + 1
            kind = "positive" if n in pos else "control" if n in CONTROL else "normal"
            fixed = g.get("fixed", cls == "tool_called") if kind == "positive" else None
            if kind == "positive":
                m["fixed"] += bool(fixed)
            elif kind == "control":
                needless = cls == "tool_called"
                wrong_flag = bool(flagged) and not g.get("flag_correct")  # a control's draft can still promise
                m["controls_flagged"] += bool(flagged)
                m["controls_needless_tool"] += needless
                m["controls_bad"] += wrong_flag or needless
            else:
                broken = g.get("broken", cls != "tool_called")
                m["normal_broken"] += bool(broken)
                m["normal_flagged"] += bool(flagged)
            rows.setdefault(str(n), {"set": kind, **({"m9": category(n)} if kind == "positive" else {})})[name] = {
                "first_step_tools": first, "flagged": flagged, "tools": called, "class": cls,
                **({"fixed": bool(fixed)} if kind == "positive" else {})}
        metrics[name] = m
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    result.update({"sets": {"positive": pos, "control": CONTROL, "normal": NORMAL,
                            "not_run": [n for n in pos + CONTROL + NORMAL if n not in ran]},
                   "metrics": metrics, "cases": rows})
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    for name, m in metrics.items():
        r = m["run"]
        print(f"{name:14} fixed {m['fixed']}/{r['positive']}  controls bad {m['controls_bad']}/{r['control']} "
              f"(flagged {m['controls_flagged']}, needless tool {m['controls_needless_tool']})  "
              f"normal broken {m['normal_broken']}/{r['normal']} (flagged {m['normal_flagged']})  "
              f"calls {m['model_calls']} (+{m['extra_calls']})  tokens {m['tokens']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(Path.home() / ".pathfinder"))
    ap.add_argument("--out", required=True, help="scratch directory for traces and grades - never the repo")
    ap.add_argument("--cases", help="comma-separated turn numbers (default: every set)")
    ap.add_argument("--variants", default="baseline,prompt", help="baseline,prompt - verify rides on each run")
    ap.add_argument("--dry", action="store_true", help="print context sizes, call no model")
    ap.add_argument("--stop-at", type=int, default=450_000, help="start no case once the traces hold this many tokens")
    ap.add_argument("--score", metavar="RESULT_JSON", help="score traces + grades into this file; no model call")
    a = ap.parse_args()
    out = Path(a.out).expanduser()
    if ROOT in out.resolve().parents:
        sys.exit("--out must be outside the repository: traces hold ledger text")
    if a.score:
        score(out, Path(a.score))
    else:
        cases = [int(x) for x in a.cases.split(",")] if a.cases else positives() + CONTROL + NORMAL
        run(Path(a.home).expanduser(), out, cases, a.variants.split(","), a.dry, a.stop_at)
