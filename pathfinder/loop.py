"""The agent loop: drives `step` (an adapter stream already bound to an
endpoint and tools) until the model answers, the budget runs out, or the
turn is cancelled. The loop never sees a model name, an origin, or a
policy - those are the caller's business."""
import collections
import json
import threading
from dataclasses import dataclass

from .broker import Gate


@dataclass
class Outcome:
    kind: str  # "answer" | "exhausted" | "cancelled"
    text: str = ""
    steps: int = 0
    usage: dict | None = None


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
    if assistant_idx is None:
        return
    tool_calls = messages[assistant_idx].get("tool_calls") or []
    if not tool_calls:
        return
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
        messages.append({"role": "tool", "tool_call_id": tc_id, "content": json.dumps(result)})


def run(step, messages, *, call_tool, emit, budget=8, control=None, resume=False, final_step=None):
    usage = None
    said = []  # everything streamed to the user this turn: what was said must reach history

    def gated(g):
        g.said = "".join(said)  # the gate question the user just read must reach the ledger too
        return g

    if resume:
        try:
            settle(messages, call_tool, control)  # finish the calls the gate left outstanding first
        except Gate as g:
            raise gated(g)

    i = 0
    while i < budget:
        if control is not None:
            _drain(control, messages, envelope=False)  # a steer queued at the last boundary
            control.steer_cut.clear()  # a cut flagged during a tool would discard the next request whole
            if control.interrupt.is_set():
                # The real adapter only notices `cancel` between chunks - after the request went
                # out and the first token came back. Check here so no request is spent.
                return Outcome("cancelled", text="".join(said), steps=i, usage=usage)
        i += 1

        text_parts = []
        tool_calls = None
        cut = None  # None | "interrupt" | "steer"
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

        text = "".join(text_parts)

        if cut == "interrupt":
            return Outcome("cancelled", text="".join(said + [text]), steps=i, usage=usage)

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
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            return Outcome("answer", text="".join(said), steps=i, usage=usage)

        try:
            settle(messages, call_tool, control)  # a Gate propagates out of here
        except Gate as g:
            raise gated(g)

    if final_step is not None:
        # D30: the budget is spent and the last reply was still a tool call. One more request
        # with no tools, so the turn ends in words ("found nothing" beats silence) and no
        # `user` message is left dangling for the next turn to keep answering. Live: three
        # turns in a row exhausted on repeated searches and said nothing.
        if control is not None and control.interrupt.is_set():
            return Outcome("cancelled", text="".join(said), steps=budget, usage=usage)
        text_parts = []
        for kind, payload in final_step(messages):
            if kind == "text":
                text_parts.append(payload)
                emit("delta", payload)
            elif kind == "usage":
                usage = _sum_usage(usage, payload)
            elif kind == "cancelled":
                return Outcome("cancelled", text="".join(said + text_parts), steps=budget, usage=usage)
        if text_parts:
            said.append("".join(text_parts))
            messages.append({"role": "assistant", "content": "".join(text_parts)})
    return Outcome("exhausted", text="".join(said), steps=budget, usage=usage)
