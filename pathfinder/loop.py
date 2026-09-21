"""The agent loop: drives `step` (an adapter stream already bound to an
endpoint and tools) until the model answers, the budget runs out, or the
turn is cancelled. The loop never sees a model name, an origin, or a
policy - those are the caller's business."""
import json
from dataclasses import dataclass


@dataclass
class Outcome:
    kind: str  # "answer" | "exhausted" | "cancelled"
    text: str = ""
    steps: int = 0
    usage: dict | None = None


def _sum_usage(total, delta):
    if not delta:
        return total
    total = dict(total or {})
    for k, v in delta.items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
    return total


def run(step, messages, *, call_tool, emit, budget=8):
    usage = None
    said = []  # everything streamed to the user this turn: what was said must reach history
    for i in range(budget):
        text_parts = []
        tool_calls = None
        for kind, payload in step(messages):
            if kind == "text":
                text_parts.append(payload)
                emit("delta", payload)
            elif kind == "tool_calls":
                tool_calls = payload
            elif kind == "usage":
                usage = _sum_usage(usage, payload)
            elif kind == "cancelled":
                return Outcome("cancelled", text="".join(said + text_parts), steps=i, usage=usage)
            # "reasoning" is deliberately ignored: never echoed into history

        text = "".join(text_parts)
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
            return Outcome("answer", text="".join(said), steps=i + 1, usage=usage)

        for tc in tool_calls:
            result = call_tool(tc["name"], tc["arguments"])
            ok = result.get("ok", True) if isinstance(result, dict) else True
            emit("tool", {"name": tc["name"], "ok": ok})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})

    # The user watched these deltas arrive; dropping them here would leave a user message
    # with no answer in history after a restart.
    return Outcome("exhausted", text="".join(said), steps=budget, usage=usage)
