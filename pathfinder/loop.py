"""The agent loop: drives `step` (an adapter stream already bound to an
endpoint and tools) until the model answers, the budget runs out, or the
turn is cancelled. The loop never sees a model name, an origin, or a
policy - those are the caller's business."""
import collections
import json
import threading
from dataclasses import dataclass

from .broker import Gate
from .llm import LLMError
from .signals import AGENDA_HEADER
from .state import RECORD_HEADER
from .tools import oneline


@dataclass
class Outcome:
    kind: str  # "answer" | "exhausted" | "cancelled"
    text: str = ""
    steps: int = 0
    usage: dict | None = None
    check: str | None = None  # the second look (D51): "passed" | "flagged" | "failed"; None = not checked


class TurnControl:
    """The one handle a running turn hands to everything that can interrupt or
    steer it - the adapter's `cancel=`, exec.py's watcher thread, the broker's
    tool handlers. `is_set()` covers both interrupt and a mid-stream steer cut,
    so the adapter needs no special case for either."""

    def __init__(self):
        self.interrupt = threading.Event()
        self.steer_cut = threading.Event()
        self.steers = collections.deque()
        self.in_tool = False

    def is_set(self) -> bool:
        return self.interrupt.is_set() or self.steer_cut.is_set()


# The envelope a mid-stream steer travels in - measured in m2 (HISTORY.md):
# without it, the model does not know its answer was cut and rewrites the
# whole thing from the start. Copied verbatim from bench/m2_steer.py.
STEER_ENVELOPE = ("[The user interrupted mid-answer with the instruction below. Apply it and continue "
                  "from exactly where the answer above stopped; do not repeat what was already written.]\n")

# The second look (D51) - measured in m10 (HISTORY.md). Copied verbatim from
# bench/m10_unsupported.py; U17's test_second_look_matches_the_bench will hold them byte-identical.
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
    "recounting what was said earlier in the conversation, and asking the user a question: false. "
    "What the assistant itself called earlier, and what is waiting for the user's approval, is "
    "supported by the record given below when it is given there. A recap of this conversation - "
    "what was said or done earlier - is answered from the conversation and needs no tool.")
FLAG_WORDS = {"unsupported_state": "says what the calendar, reminders, memory or registry hold",
              "unsupported_done": "says something was done", "empty_promise": "promises to do something",
              "false_incapability": "says a tool or capability is missing"}
RERUN_NOTE = ("[Your draft answer was not shown to the user: it {what}, and no tool result in this "
              "turn supports it ({why}). Call the tool now, or say plainly that you cannot.]")


def _sum_usage(total, delta):
    if not delta:
        return total
    total = dict(total or {})
    for k, v in delta.items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
    return total


def _drain(control, messages, envelope):
    """Move every queued steer into one user message. Plain at a tool boundary;
    wrapped in STEER_ENVELOPE when it follows a mid-stream cut."""
    if control is None or not control.steers:
        return
    parts = []
    while control.steers:
        parts.append(control.steers.popleft())
    text = "\n".join(parts)
    if envelope:
        text = STEER_ENVELOPE + text
    messages.append({"role": "user", "content": text})


def settle(messages, call_tool, control=None):
    """Run the trailing assistant message's tool calls that have no `tool`
    result yet, in order. A `Gate` propagates, leaving `messages` with the
    results obtained so far - which is why "trailing" is found by scanning
    back for the last assistant message, not by assuming it is messages[-1]:
    a call already settled before the gate leaves its `tool` result after it."""
    assistant_idx = None
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "assistant":
            assistant_idx = idx
            break
    results = []  # what this call appended: the caller tells a refused call (D69) from a tool that ran
    if assistant_idx is None:
        return results
    tool_calls = messages[assistant_idx].get("tool_calls") or []
    if not tool_calls:
        return results
    answered = {m["tool_call_id"] for m in messages[assistant_idx:] if m.get("role") == "tool"}
    for tc in tool_calls:
        tc_id = tc["id"]
        if tc_id in answered:
            continue
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (TypeError, ValueError):
            args = {}
        if control is not None:
            control.in_tool = True
        try:
            result = call_tool(tc_id, name, args)
        finally:
            if control is not None:
                control.in_tool = False
        messages.append({"role": "tool", "tool_call_id": tc_id, "content": json.dumps(result, ensure_ascii=False)})
        results.append(result)
    return results


def verifier_input(tools, messages):
    """What the second look sees, as bench m10 built it: the tool names, the agenda, the answer
    before the turn's user message, the latest tool-call record (D68), that message, and the draft (`messages[-1]`)."""
    u = max(k for k, m in enumerate(messages) if m["role"] == "user")
    before = [m for m in messages[:u] if m["role"] == "assistant"]
    agenda = next((p for p in messages[1]["content"].split("\n\n") if p.startswith(AGENDA_HEADER)), "")
    lines = [f"Tools: {', '.join(tools)}"]
    if agenda:
        lines.append("Agenda the system gave this turn:\n" + agenda)
    if before:
        lines.append("The assistant's previous answer: " + before[-1]["content"][:600])
    rec = next((m["content"] for m in reversed(messages[:u]) if m["role"] == "system"
                and m["content"].startswith(RECORD_HEADER)), "")
    if rec:
        lines.append("The assistant's tool calls in earlier turns, from the ledger (names, arguments and "
                     "state - not results):\n" + rec[len(RECORD_HEADER) + 1:][:1500])
    lines += ["The user: " + messages[u]["content"][:1000], "No tool was called in this turn.",
              "Draft: " + messages[-1]["content"]]
    return "\n\n".join(lines)


def _second_look(verify_step, tools, messages):
    """(flags, why, usage) - flags None when the look failed: an LLMError, bad JSON, a cancel,
    anything. A failed look lets the draft stand; its usage still counts."""
    raw, usage = "", None
    try:
        ask = [{"role": "system", "content": VERIFIER_PROMPT},
               {"role": "user", "content": verifier_input(tools, messages)}]
        for kind, payload in verify_step(ask):
            if kind == "text":
                raw += payload
            elif kind == "usage":
                usage = _sum_usage(usage, payload)
            elif kind == "cancelled":
                return None, "", usage
        parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        # `why` is the verifier's words about text that may be a third party's (the agenda): one line, cut
        return [k for k in FLAG_WORDS if parsed.get(k) is True], oneline(parsed.get("why") or "", 200), usage
    except Exception:  # caught here alone: the look is the harness's own call, never the turn's end
        return None, "", usage


def run(step, messages, *, call_tool, emit, budget=8, control=None, resume=False, final_step=None,
        second_look=None):
    """`second_look`: the tool names offered this turn; when given, a final answer that came with
    no tool result is checked once by `final_step` (D51)."""
    usage, check = None, None
    called = resume  # a tool ran in this turn (a call refused at the door is not one, D69): no second look
    said = []  # everything streamed this turn reaches the ledger; only a withdrawn draft leaves it

    def with_said(e, parts=()):
        e.said = "".join(said + list(parts))  # a gate question, or text a model error cut, must reach the ledger
        e.check = check  # a re-run that gates or fails still owes turn_end its check
        return e

    if resume:
        try:
            settle(messages, call_tool, control)  # finish the calls the gate left outstanding first
        except Gate as g:
            raise with_said(g)

    i = 0
    while i < budget:
        if control is not None:
            _drain(control, messages, envelope=False)  # a steer queued at the last boundary
            control.steer_cut.clear()  # a cut flagged during a tool would discard the next request whole
            if control.interrupt.is_set():
                # The real adapter only notices `cancel` between chunks - after the request went
                # out and the first token came back. Check here so no request is spent.
                return Outcome("cancelled", text="".join(said), steps=i, usage=usage, check=check)
        i += 1

        text_parts = []
        tool_calls = None
        cut = None  # None | "interrupt" | "steer"
        try:
            for kind, payload in step(messages):
                if kind == "text":
                    text_parts.append(payload)
                    emit("delta", payload)
                elif kind == "tool_calls":
                    tool_calls = payload
                elif kind == "usage":
                    usage = _sum_usage(usage, payload)
                elif kind == "cancelled":
                    # No control at all behaves like an interrupt (partition 1's only case):
                    # only a live TurnControl with steer_cut set, not interrupt, is a steer.
                    cut = "steer" if (control is not None and control.steer_cut.is_set()) else "interrupt"
                    break
                # "reasoning" is deliberately ignored: never echoed into history
        except LLMError as e:
            raise with_said(e, text_parts)

        text = "".join(text_parts)

        if cut == "interrupt":
            return Outcome("cancelled", text="".join(said + [text]), steps=i, usage=usage, check=check)

        if cut == "steer":
            if text:
                messages.append({"role": "assistant", "content": text})
                said.append(text)
            control.steer_cut.clear()
            _drain(control, messages, envelope=True)
            continue  # a steer cut still counts as a step (the budget is a cost ceiling)

        said.append(text)
        assistant_msg = {"role": "assistant", "content": text}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            if second_look is not None and check is None and not called and text.strip() and i < budget:
                if control is not None:
                    control.in_tool = True  # a steer typed during the look waits for it; an interrupt still cuts
                try:
                    flags, why, used = _second_look(final_step, second_look, messages)
                finally:  # a cancel (BaseException) must not leave steers held for good
                    if control is not None:
                        control.in_tool = False
                usage = _sum_usage(usage, used)  # the harness's own call, like D30's: not a step
                check = "failed" if flags is None else "flagged" if flags else "passed"
                if flags:
                    # Already on the screen, so withdrawn in the open (D35): out of `said` and
                    # `messages`, a note says why, and the step re-runs once - never re-checked.
                    what = " and ".join(FLAG_WORDS[f] for f in flags)
                    messages.pop()
                    said.pop()
                    emit("retract", {"draft": text, "flags": flags, "what": what, "why": why})
                    messages.append({"role": "system",
                                     "content": RERUN_NOTE.format(what=what, why=why or "no reason given")})
                    continue
            return Outcome("answer", text="".join(said), steps=i, usage=usage, check=check)

        try:
            results = settle(messages, call_tool, control)  # a Gate propagates out of here
        except Gate as g:
            raise with_said(g)
        called = called or any(not r.get("refused") for r in results)

    if final_step is not None:
        # D30: the budget is spent and the last reply was still a tool call. One more request
        # with no tools, so the turn ends in words ("found nothing" beats silence) and no
        # `user` message is left dangling for the next turn to keep answering. Live: three
        # turns in a row exhausted on repeated searches and said nothing.
        if control is not None and control.interrupt.is_set():
            return Outcome("cancelled", text="".join(said), steps=budget, usage=usage, check=check)
        text_parts = []
        try:
            for kind, payload in final_step(messages):
                if kind == "text":
                    text_parts.append(payload)
                    emit("delta", payload)
                elif kind == "usage":
                    usage = _sum_usage(usage, payload)
                elif kind == "cancelled":
                    return Outcome("cancelled", text="".join(said + text_parts), steps=budget, usage=usage,
                                   check=check)
        except LLMError as e:
            raise with_said(e, text_parts)
        if text_parts:
            said.append("".join(text_parts))
            messages.append({"role": "assistant", "content": "".join(text_parts)})
    return Outcome("exhausted", text="".join(said), steps=budget, usage=usage, check=check)
