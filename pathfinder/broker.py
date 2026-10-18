"""The one door every tool call goes through. The broker never writes the
ledger itself - it returns the raw result (the clone caps and records it; the
store's renderer filters it on the way to the model)."""
import unicodedata

from . import policy
from .tools import OFF_TOOLS, coerce


USER_DENIED_TEXT = ("The user looked at this call and declined it. This is their decision, not an "
                    "error: do not ask again, retry, or route around it - tell them briefly and stop.")


class Gate(Exception):
    """A call needs the user's approval. Left to propagate out of `call` to the clone, which records
    the `gate` - a gate is an exception, not an outcome."""

    def __init__(self, id, name, arguments):
        super().__init__(f"gated: {name}")
        self.id = id
        self.name = name
        # not `self.args`: BaseException.args turns a dict into a tuple of its keys
        self.arguments = arguments


class Broker:
    def __init__(self, tools, origin, grants, *, control, approved=None, denied=frozenset()):
        self.tools = tools
        self.origin = origin
        self.grants = grants
        self.control = control
        # A decision is single-use and names the exact call: the tool-call id is a string the
        # model chose (and re-uses), so it cannot be a standing permission for the turn.
        # approved: {call_id: (name, args)}; denied: {call_id}. Both are consumed when matched.
        self.approved = dict(approved or {})
        self.denied = set(denied)

    @staticmethod
    def _refuse(error, **extra) -> dict:
        """A call turned away at the door is a failed call: the clone records its `result` with `ok: false`."""
        return {"ok": False, "error": error, **extra}

    def call(self, id, name, args) -> dict:
        spec = self.tools.get(name)
        if spec is None:
            name = str(name)[:64]  # model text: bounded
            off = f" (the {OFF_TOOLS[name]} sense is off)" if name in OFF_TOOLS else ""
            return self._refuse(f"no such tool: {name}{off}")

        coerced, error = coerce(spec, args)
        if error:
            return self._refuse(error)
        if spec.normalize:
            try:
                coerced = spec.normalize(coerced)  # before the gate: the user approves what will run
            except ValueError as e:
                return self._refuse(str(e))

        if id in self.denied:
            self.denied.discard(id)
            return {"ok": False, "denied": True, "error": USER_DENIED_TEXT}
        if self.approved.get(id) == (name, coerced):
            del self.approved[id]  # what the user approved, once; anything else goes through policy
        else:
            decision = policy.decide(spec, self.origin, coerced, self.grants)
            if decision == "deny":
                return self._refuse(policy.DENIED_TEXT, denied=True)
            if decision == "ask":
                raise Gate(id, name, coerced)

        try:
            result = spec.handler(**coerced, control=self.control)
            ok = True
        except Exception as e:
            result = str(e)
            ok = False
        return {"ok": True, "result": result} if ok else {"ok": False, "error": result}


def _said(text: str) -> str:
    """How a chat message is compared: NFKC, ’ as ', lowercase, spaces collapsed, trailing `.!~` dropped."""
    return " ".join(unicodedata.normalize("NFKC", text).replace("’", "'").lower().split()).rstrip(".!~ ")


# D52: a whole message that only approves or denies. Normalized like the message, because NFKC
# turns the compatibility jamo of ㅇㅇ into conjoining ones.
CHAT_APPROVAL = tuple(map(_said, (
    "승인", "승인해", "승인해줘", "승인합니다", "승인할게", "승인할게요", "허락", "허용", "진행해", "진행해줘",
    "네", "예", "응", "ㅇㅇ", "좋아", "그래", "ok", "okay", "yes", "y", "approve", "approved", "i approve",
    "i'll approve", "go ahead", "do it",
    "거부", "거절", "취소", "아니", "아니요", "no", "n", "deny", "cancel")))


def chat_approval(state, session, text):
    """D52: an approval phrase typed while a gate is open is answered by the harness, one line per
    open gate (the root's undecided gates of this session by seq, then v1 approvals), and the gate
    stays pending. A gate counts while it is newer than the session's last user message. None: the
    message goes to the model."""
    if _said(text) not in CHAT_APPROVAL:
        return None
    last = state.last_user_seq(session) or 0
    gates = [(g["seq"], g["tool"]) for g in state.open_gates()
             if g["session"] == session and g["decision"] is None and g["seq"] > last]
    lines = "\n".join(f"승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve {id} or /deny "
                      f"{id} ({tool}; shell: pathfinder approve|deny {id})"
                      for id, tool in gates + [(p["id"], p["tool"]) for p in state.open_approvals(session)])
    return f"{lines} - if you meant something else, say it again · 다른 뜻이었다면 다시 말해 주세요" if lines else None
