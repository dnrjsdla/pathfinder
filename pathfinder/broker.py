"""The one door every tool call goes through. The broker never writes the
ledger itself - it emits, and the daemon records tool_call/tool_result on the
event-loop thread, so the single-writer rule from partition 1 still holds."""
import json

from . import policy
from .tools import READ_CLIP, clip, coerce


def _clip_for_model(value):
    """Every tool result reaches `loop.settle` as `json.dumps(this)` - clip here, the one
    place, so no tool (or a future one) can push an unbounded blob into the model's context."""
    if isinstance(value, str):
        return clip(value)
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= READ_CLIP else clip(text)


USER_DENIED_TEXT = ("The user looked at this call and declined it. This is their decision, not an "
                    "error: do not ask again, retry, or route around it - tell them briefly and stop.")


class Gate(Exception):
    """A call needs the user's approval. Left to propagate out of loop.run/settle -
    a gate is an exception, not an outcome."""

    def __init__(self, id, name, arguments):
        super().__init__(f"gated: {name}")
        self.id = id
        self.name = name
        # not `self.args`: BaseException.args turns a dict into a tuple of its keys
        self.arguments = arguments
        self.said = ""  # filled by loop.run: the text streamed before the gate


class Broker:
    def __init__(self, tools, origin, grants, *, control, emit, approved=None, denied=frozenset()):
        self.tools = tools
        self.origin = origin
        self.grants = grants
        self.control = control
        self.emit = emit
        # A decision is single-use and names the exact call: the tool-call id is a string the
        # model chose (and re-uses), so it cannot be a standing permission for the turn.
        # approved: {call_id: (name, args)}; denied: {call_id}. Both are consumed when matched.
        self.approved = dict(approved or {})
        self.denied = set(denied)

    def call(self, id, name, args) -> dict:
        spec = self.tools.get(name)
        if spec is None:
            return {"ok": False, "error": f"no such tool: {name}"}

        coerced, error = coerce(spec, args)
        if error:
            return {"ok": False, "error": error}
        if spec.normalize:
            try:
                coerced = spec.normalize(coerced)  # before the gate: the user approves what will run
            except ValueError as e:
                return {"ok": False, "error": str(e)}

        if id in self.denied:
            self.denied.discard(id)
            return {"ok": False, "denied": True, "error": USER_DENIED_TEXT}
        if self.approved.get(id) == (name, coerced):
            del self.approved[id]  # what the user approved, once; anything else goes through policy
        else:
            decision = policy.decide(spec, self.origin, coerced, self.grants)
            if decision == "deny":
                return {"ok": False, "denied": True, "error": policy.DENIED_TEXT}
            if decision == "ask":
                raise Gate(id, name, coerced)

        self.emit("tool_call", {"id": id, "name": name, "args": coerced})
        try:
            result = spec.handler(**coerced, control=self.control)
            ok = True
        except Exception as e:
            result = str(e)
            ok = False
        self.emit("tool_result", {"id": id, "name": name, "ok": ok, "result": result})
        clipped = _clip_for_model(result)
        return {"ok": True, "result": clipped} if ok else {"ok": False, "error": clipped}
