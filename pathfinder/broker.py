"""The one door every tool call goes through. The broker never writes the
ledger itself - it emits, and the daemon records tool_call/tool_result on the
event-loop thread, so the single-writer rule from partition 1 still holds."""
import json
import unicodedata

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


def _said(text: str) -> str:
    """How a chat message is compared: NFKC, lowercase, spaces collapsed, trailing `.!~` dropped."""
    return " ".join(unicodedata.normalize("NFKC", text).lower().split()).rstrip(".!~ ")


# D52: a whole message that only approves or denies. Normalized like the message, because NFKC
# turns the compatibility jamo of ㅇㅇ into conjoining ones.
CHAT_APPROVAL = tuple(map(_said, (
    "승인", "승인해", "승인해줘", "승인합니다", "승인할게", "승인할게요", "허락", "허용", "진행해", "진행해줘",
    "네", "예", "응", "ㅇㅇ", "좋아", "그래", "ok", "okay", "yes", "y", "approve", "approved", "i approve",
    "i'll approve", "go ahead", "do it",
    "거부", "거절", "취소", "아니", "아니요", "no", "n", "deny", "cancel")))


def chat_approval(state, session, text):
    """D52: an approval phrase typed while a gate is open is answered by the harness, one line per
    open gate, and the gate stays pending. None: the message goes to the model."""
    if _said(text) not in CHAT_APPROVAL:
        return None
    return "\n".join(f"승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve {p['id']} "
                     f"or /deny {p['id']} ({p['tool']})" for p in state.open_approvals(session)) or None
