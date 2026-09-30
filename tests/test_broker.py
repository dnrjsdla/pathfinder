import unittest

from pathfinder import policy
from pathfinder.broker import CHAT_APPROVAL, USER_DENIED_TEXT, Broker, Gate, chat_approval
from pathfinder.tools import ToolSpec


def _spec(name, handler=None, params=None, required=None, **kw):
    return ToolSpec(name=name, description="", params=params or {}, required=required or [],
                    handler=handler or (lambda control=None: {"done": True}), **kw)


READ = _spec("read_thing")
WRITE = _spec("write_thing", write=True)
TOOLS = {"read_thing": READ, "write_thing": WRITE}


class BrokerTest(unittest.TestCase):
    def _broker(self, origin="user", grants=None, **kw):
        emitted = []
        b = Broker(TOOLS, origin, grants or {}, control=None,
                  emit=lambda k, p: emitted.append((k, p)), **kw)
        return b, emitted

    def test_broker_unknown_deny_ask_allow(self):
        b, emitted = self._broker()
        self.assertEqual(b.call("c0", "nope", {}), {"ok": False, "error": "no such tool: nope"})

        b, emitted = self._broker()
        result = b.call("c1", "read_thing", {})  # allow
        self.assertEqual(result, {"ok": True, "result": {"done": True}})
        self.assertEqual([k for k, _ in emitted], ["tool_call", "tool_result"])

        b, emitted = self._broker()
        with self.assertRaises(Gate) as cm:
            b.call("c2", "write_thing", {})  # ask
        self.assertEqual((cm.exception.id, cm.exception.name), ("c2", "write_thing"))
        self.assertEqual(emitted, [])  # nothing emitted before a gate

        b, emitted = self._broker(origin="system")
        result = b.call("c3", "write_thing", {})  # deny
        self.assertEqual(result, {"ok": False, "denied": True, "error": policy.DENIED_TEXT})

    def test_refused_call_emits_nothing(self):
        b, emitted = self._broker(origin="system")
        self.assertTrue(b.call("c1", "write_thing", {})["denied"])  # policy refuses a write here
        self.assertEqual(emitted, [])  # no tool_call: the record can never show a call that did not run

    def test_broker_approved_skips_policy(self):
        b, emitted = self._broker(approved={"c1": ("write_thing", {})})
        result = b.call("c1", "write_thing", {})
        self.assertEqual(result, {"ok": True, "result": {"done": True}})

    def test_broker_approval_is_single_use_and_exact(self):
        """The call id is a string the model chose and re-uses: one approval must not become a
        standing permission, nor cover the same id with different arguments (review, partition 2)."""
        b, emitted = self._broker(approved={"c1": ("write_thing", {})})
        b.call("c1", "write_thing", {})
        with self.assertRaises(Gate):
            b.call("c1", "write_thing", {})  # same id again: gated again
        b, emitted = self._broker(approved={"c1": ("write_thing", {"x": 1})})
        with self.assertRaises(Gate):
            b.call("c1", "write_thing", {})  # same id, other arguments: not what was approved

    def test_broker_denied_returns_user_denial(self):
        b, emitted = self._broker(denied={"c1"})
        result = b.call("c1", "write_thing", {})
        self.assertEqual(result, {"ok": False, "denied": True, "error": USER_DENIED_TEXT})
        self.assertEqual(emitted, [])

    def test_broker_coerce_error_before_policy(self):
        bad_spec = _spec("typed_thing", params={"n": {"type": "integer"}}, required=["n"])
        b, emitted = self._broker()
        b.tools = {**TOOLS, "typed_thing": bad_spec}
        result = b.call("c1", "typed_thing", {"n": "not a number"})
        self.assertFalse(result["ok"])
        self.assertEqual(emitted, [])

    def test_broker_handler_exception_is_ok_false(self):
        def boom(control=None):
            raise RuntimeError("kaboom")
        spec = _spec("boom_thing", handler=boom)
        b, emitted = self._broker()
        b.tools = {**TOOLS, "boom_thing": spec}
        result = b.call("c1", "boom_thing", {})
        self.assertEqual(result, {"ok": False, "error": "kaboom"})

    def test_broker_clips_a_huge_result_before_returning_it(self):
        """Should-fix 7: loop.py appends `json.dumps(result)` unclipped - the broker is the
        one place every tool's result passes through, so it clips there."""
        huge = _spec("huge_thing", handler=lambda control=None: {"text": "x" * 20000})
        b, emitted = self._broker()
        b.tools = {**TOOLS, "huge_thing": huge}
        result = b.call("c1", "huge_thing", {})
        self.assertTrue(result["ok"])
        serialized = str(result["result"])
        self.assertLess(len(serialized), 20000)
        self.assertIn("omitted", serialized)
        # the ledger's own ("tool_result") emit still carries the full, unclipped result -
        # only what reaches the model (the return value) is bounded
        self.assertEqual(len(emitted[1][1]["result"]["text"]), 20000)

    def test_broker_small_result_passes_through_unclipped(self):
        b, emitted = self._broker()
        result = b.call("c1", "read_thing", {})
        self.assertEqual(result, {"ok": True, "result": {"done": True}})


class ChatApprovalTest(unittest.TestCase):
    class Open:
        def __init__(self, rows):
            self.rows = rows

        def open_approvals(self, session):
            return self.rows

    def test_chat_approval_phrases(self):
        """D52 part 2: the whole message, normalized, is one of the closed phrases."""
        open_ = self.Open([{"id": "a1", "tool": "install_server"}, {"id": "b2", "tool": "edit_memory"}])
        line = ("승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve a1 or /deny a1 "
                "(install_server; shell: pathfinder approve|deny a1)")
        self.assertEqual(len(CHAT_APPROVAL), 35)
        for phrase in ("승인", "승인해", "승인해줘", "승인합니다", "승인할게", "승인할게요", "허락", "허용", "진행해",
                       "진행해줘", "네", "예", "응", "ㅇㅇ", "좋아", "그래", "ok", "okay", "yes", "y", "approve",
                       "approved", "i approve", "i'll approve", "go ahead", "do it",
                       "거부", "거절", "취소", "아니", "아니요", "no", "n", "deny", "cancel"):
            self.assertEqual(chat_approval(open_, "s", phrase).splitlines(),
                             [line, line.replace("a1", "b2").replace("install_server", "edit_memory")], phrase)
        for typed in ("  YES!! ", "Ｙｅｓ", "I'll   approve.", "I’ll approve", "승인~", "OK.", "Go\tAhead!~", "ㅇㅇ!",
                      "Do it . !"):
            self.assertIsNotNone(chat_approval(open_, "s", typed), typed)
        for near in ("yes, and also add X", "yess", "승인해주세요", "approve it", "no way", "ok ok", "!!!", "?",
                     "yes?", "네 그리고 메일도"):
            self.assertIsNone(chat_approval(open_, "s", near), near)
        self.assertIsNone(chat_approval(self.Open([]), "s", "yes"))  # no open gate: the model answers


if __name__ == "__main__":
    unittest.main()
