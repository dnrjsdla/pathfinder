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
        return Broker(TOOLS, origin, grants or {}, control=None, **kw)

    def test_broker_unknown_deny_ask_allow(self):
        b = self._broker()
        self.assertEqual(b.call("c0", "nope", {}),
                         {"ok": False, "error": "no such tool: nope"})

        b = self._broker()
        result = b.call("c1", "read_thing", {})  # allow
        self.assertEqual(result, {"ok": True, "result": {"done": True}})

        b = self._broker()
        with self.assertRaises(Gate) as cm:
            b.call("c2", "write_thing", {})  # ask
        self.assertEqual((cm.exception.id, cm.exception.name), ("c2", "write_thing"))

        b = self._broker(origin="system")
        result = b.call("c3", "write_thing", {})  # deny
        self.assertEqual(result, {"ok": False, "denied": True, "error": policy.DENIED_TEXT})

    def test_an_unknown_tool_name_is_bounded_in_the_error(self):
        b = self._broker()
        result = b.call("c1", "n" * 3000, {})
        self.assertLess(len(result["error"]), 100)

    def test_unknown_tool_of_an_off_sense_names_the_sense(self):
        """B2: with the calendar off, `list_events` is refused with the reason, not as a name that never was."""
        from pathfinder import signals, tools
        self.addCleanup(tools.OFF_TOOLS.clear)
        signals.load({"senses": {}})
        b = self._broker()
        self.assertEqual(b.call("c1", "list_events", {}),
                         {"ok": False, "error": "no such tool: list_events (the calendar sense is off)"})

    def test_broker_approved_skips_policy(self):
        b = self._broker(approved={"c1": ("write_thing", {})})
        result = b.call("c1", "write_thing", {})
        self.assertEqual(result, {"ok": True, "result": {"done": True}})

    def test_broker_approval_is_single_use_and_exact(self):
        """The call id is a string the model chose and re-uses: one approval must not become a
        standing permission, nor cover the same id with different arguments (review, partition 2)."""
        b = self._broker(approved={"c1": ("write_thing", {})})
        b.call("c1", "write_thing", {})
        with self.assertRaises(Gate):
            b.call("c1", "write_thing", {})  # same id again: gated again
        b = self._broker(approved={"c1": ("write_thing", {"x": 1})})
        with self.assertRaises(Gate):
            b.call("c1", "write_thing", {})  # same id, other arguments: not what was approved

    def test_broker_denied_returns_user_denial(self):
        b = self._broker(denied={"c1"})
        result = b.call("c1", "write_thing", {})
        self.assertEqual(result, {"ok": False, "denied": True, "error": USER_DENIED_TEXT})

    def test_broker_coerce_error_before_policy(self):
        bad_spec = _spec("typed_thing", params={"n": {"type": "integer"}}, required=["n"])
        b = self._broker()
        b.tools = {**TOOLS, "typed_thing": bad_spec}
        result = b.call("c1", "typed_thing", {"n": "not a number"})
        self.assertFalse(result["ok"])

    def test_broker_handler_exception_is_ok_false(self):
        def boom(control=None):
            raise RuntimeError("kaboom")
        spec = _spec("boom_thing", handler=boom)
        b = self._broker()
        b.tools = {**TOOLS, "boom_thing": spec}
        result = b.call("c1", "boom_thing", {})
        self.assertEqual(result, {"ok": False, "error": "kaboom"})

    def test_broker_small_result_passes_through_unclipped(self):
        b = self._broker()
        result = b.call("c1", "read_thing", {})
        self.assertEqual(result, {"ok": True, "result": {"done": True}})

    def test_broker_returns_the_unclipped_unfiltered_result(self):
        """The clone's path (U7): the store caps and the renderer filters, so the broker hands back the value."""
        raw = {"text": "\x1b[31mred\x1b[0m" + "x" * 20000}
        failing = _spec("f", handler=lambda control=None: (_ for _ in ()).throw(RuntimeError("\x07" + "y" * 9000)))
        b = Broker({"t": _spec("t", handler=lambda control=None: raw), "f": failing}, "user", {}, control=None)
        self.assertEqual(b.call("c1", "t", {}), {"ok": True, "result": raw})
        self.assertEqual(b.call("c2", "f", {}), {"ok": False, "error": "\x07" + "y" * 9000})


WAY_OUT = " - if you meant something else, say it again · 다른 뜻이었다면 다시 말해 주세요"


class ChatApprovalTest(unittest.TestCase):
    class Open:
        def __init__(self, rows, gates=(), last=None):
            self.rows, self.gates, self.last = rows, list(gates), last

        def open_approvals(self, session):
            return self.rows

        def open_gates(self):
            return self.gates

        def last_user_seq(self, session):
            return self.last

    def test_chat_approval_lists_the_roots_open_gates_by_seq(self):
        """V22: an undecided gate of this session newer than its last user message gets a line by its seq,
        before the v1 rows; a decided one, another session's, or one talked past gets none."""
        gate = {"seq": 40, "session": "s", "decision": None, "tool": "write_file"}
        gates = [gate, {**gate, "seq": 41, "decision": "approved"}, {**gate, "seq": 42, "session": "t"},
                 {**gate, "seq": 9}]
        lines = chat_approval(self.Open([{"id": "a1", "tool": "edit_memory"}], gates, last=10), "s", "yes").splitlines()
        self.assertEqual([line.split(": /approve ")[1].split()[0] for line in lines], ["40", "a1"])
        self.assertIsNone(chat_approval(self.Open([], [{**gate, "decision": "denied"}]), "s", "yes"))

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
                             [line, line.replace("a1", "b2").replace("install_server", "edit_memory") + WAY_OUT],
                             phrase)
        for typed in ("  YES!! ", "Ｙｅｓ", "I'll   approve.", "I’ll approve", "승인~", "OK.", "Go\tAhead!~", "ㅇㅇ!",
                      "Do it . !"):
            self.assertIsNotNone(chat_approval(open_, "s", typed), typed)
        for near in ("yes, and also add X", "yess", "승인해주세요", "approve it", "no way", "ok ok", "!!!", "?",
                     "yes?", "네 그리고 메일도"):
            self.assertIsNone(chat_approval(open_, "s", near), near)
        self.assertIsNone(chat_approval(self.Open([]), "s", "yes"))  # no open gate: the model answers

    def test_chat_approval_line_says_how_to_get_past_it(self):
        """U32: a short reply to the assistant's own question is swallowed once - the line says how
        to get past it, once, at the end of the last gate's line."""
        one = chat_approval(self.Open([{"id": "a1", "tool": "write_file"}]), "s", "응")
        self.assertEqual(one, "승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve a1 or /deny a1 "
                              "(write_file; shell: pathfinder approve|deny a1) - if you meant something else, say it "
                              "again · 다른 뜻이었다면 다시 말해 주세요")
        two = chat_approval(self.Open([{"id": "a1", "tool": "write_file"}, {"id": "b2", "tool": "edit_memory"}]),
                            "s", "no").splitlines()
        self.assertEqual(two, [one.removesuffix(WAY_OUT), one.replace("a1", "b2").replace("write_file", "edit_memory")])


if __name__ == "__main__":
    unittest.main()
