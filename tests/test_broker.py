import tempfile
import unittest
from pathlib import Path

from pathfinder import policy
from pathfinder.broker import CHAT_APPROVAL, USER_DENIED_TEXT, Broker, Gate, chat_approval
from pathfinder.ledger import Ledger
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
        self.assertEqual(b.call("c0", "nope", {}),
                         {"ok": False, "refused": True, "error": "no such tool: nope"})

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
        self.assertEqual(result, {"ok": False, "refused": True, "denied": True, "error": policy.DENIED_TEXT})

    def _assert_refused(self, b, emitted, id, name, args, error, **extra):
        result = b.call(id, name, args)
        self.assertEqual(result, {"ok": False, "refused": True, "error": error, **extra})
        self.assertEqual(emitted, [("tool_call", {"id": id, "name": name, "args": args}),
                                   ("tool_result", {"id": id, "name": name, "ok": False, "result": error,
                                                    "refused": True, **extra})])

    def test_a_call_refused_at_the_door_is_recorded_as_refused(self):
        """D69: the four early returns emit tool_call then tool_result with `refused`."""
        b, emitted = self._broker()
        self._assert_refused(b, emitted, "c1", "nope", {"x": 1}, "no such tool: nope")
        bad = _spec("typed_thing", params={"n": {"type": "integer"}}, required=["n"])
        b, emitted = self._broker()
        b.tools = {**TOOLS, "typed_thing": bad}
        result = b.call("c2", "typed_thing", {"n": "not a number"})
        self.assertEqual([k for k, _ in emitted], ["tool_call", "tool_result"])
        self.assertTrue(result["refused"] and emitted[1][1]["refused"] and emitted[0][1]["args"] == {"n": "not a number"})
        def no(args):
            raise ValueError("bad time")
        b, emitted = self._broker()
        b.tools = {**TOOLS, "norm_thing": _spec("norm_thing", normalize=no)}
        self._assert_refused(b, emitted, "c3", "norm_thing", {}, "bad time")
        b, emitted = self._broker(origin="system")
        self._assert_refused(b, emitted, "c4", "write_thing", {}, policy.DENIED_TEXT, denied=True)

    def test_an_unknown_tool_name_is_bounded_in_events_and_error(self):
        b, emitted = self._broker()
        result = b.call("c1", "n" * 3000, {})
        self.assertTrue(all(len(p["name"]) <= 64 for _, p in emitted) and len(result["error"]) < 100)

    def test_unknown_tool_of_an_off_sense_names_the_sense(self):
        """B2: with the calendar off, `list_events` is refused with the reason, not as a name that never was."""
        from pathfinder import signals, tools
        self.addCleanup(tools.OFF_TOOLS.clear)
        signals.load({"senses": {}})
        b, emitted = self._broker()
        self._assert_refused(b, emitted, "c1", "list_events", {}, "no such tool: list_events (the calendar sense is off)")

    def test_an_allowed_call_is_not_marked_refused(self):
        b, emitted = self._broker()
        self.assertNotIn("refused", b.call("c1", "read_thing", {}))
        self.assertTrue(all("refused" not in p for _, p in emitted))

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

    def _result_of(self, handler):
        b, emitted = self._broker()
        b.tools = {**TOOLS, "t": _spec("t", handler=handler)}
        return b.call("c1", "t", {}), emitted

    def test_tool_result_loses_bidi_zero_width_ansi_and_controls(self):
        """D62: a web page, a mail body or an MCP server's text reached the model as it came."""
        hostile = "\x1b[31mred\x1b[0m \u202egnp.exe\u200b\x07\x00 \x1b]8;;https://example.com\x07link\x1b]8;;\x07\ufeff"
        result, _ = self._result_of(lambda control=None: hostile)
        self.assertEqual(result, {"ok": True, "result": "red gnp.exe link"})
        result, _ = self._result_of(lambda control=None: {"k\u200b": [hostile, (hostile, 1)], "n": None})
        self.assertEqual(result["result"], {"k": ["red gnp.exe link", ["red gnp.exe link", 1]], "n": None})

    def test_tool_result_keeps_newlines_tabs_and_spacing(self):
        code = 'def f():\n\tif x:\n        return {"a":  1}\n\n# 한국어  주석 👍\n'
        result, _ = self._result_of(lambda control=None: code)
        self.assertEqual(result["result"], code)  # code and a page keep their shape
        result, _ = self._result_of(lambda control=None: {"text": "a\r\nb\u00a0 c\u2028d\re\r\n"})
        self.assertEqual(result["result"], {"text": "a\nb  c d e\n"})  # CRLF a newline; other whitespace one space

    def test_error_text_passes_the_filter(self):
        def boom(control=None):
            raise RuntimeError("\x1b[31mka\u202eboom\u200b\x07\n\tat line 2")
        result, _ = self._result_of(boom)
        self.assertEqual(result, {"ok": False, "error": "kaboom\n\tat line 2"})

    def test_ledger_keeps_the_raw_result(self):
        """The filter is on the way to the model; the ledger holds what the tool returned."""
        raw = {"text": "\x1b[31mred\x1b[0m\u202e\u200b\x07\r\n"}
        with tempfile.TemporaryDirectory() as tmp:
            led = Ledger(Path(tmp))
            b = Broker({"t": _spec("t", handler=lambda control=None: raw)}, "user", {}, control=None,
                       emit=lambda kind, payload: led.append(kind, origin="user", **payload))
            self.assertEqual(b.call("c1", "t", {})["result"], {"text": "red\n"})  # what the model sees
            [event] = [e for e in led.read() if e["type"] == "tool_result"]
        self.assertEqual(event["result"], raw)

    def test_raw_broker_returns_the_unclipped_unfiltered_result(self):
        """The clone's path (U7): the store caps and the renderer filters, so the broker hands back the value."""
        raw = {"text": "\x1b[31mred\x1b[0m" + "x" * 20000}
        failing = _spec("f", handler=lambda control=None: (_ for _ in ()).throw(RuntimeError("\x07" + "y" * 9000)))
        b = Broker({"t": _spec("t", handler=lambda control=None: raw), "f": failing}, "user", {}, control=None,
                   emit=lambda kind, payload: None, raw=True)
        self.assertEqual(b.call("c1", "t", {}), {"ok": True, "result": raw})
        self.assertEqual(b.call("c2", "f", {}), {"ok": False, "error": "\x07" + "y" * 9000})


WAY_OUT = " - if you meant something else, say it again · 다른 뜻이었다면 다시 말해 주세요"


class ChatApprovalTest(unittest.TestCase):
    class Open:
        def __init__(self, rows):
            self.rows = rows

        def open_approvals(self, session, max_age_s):
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
            self.assertEqual(chat_approval(open_, "s", phrase, 7200).splitlines(),
                             [line, line.replace("a1", "b2").replace("install_server", "edit_memory") + WAY_OUT],
                             phrase)
        for typed in ("  YES!! ", "Ｙｅｓ", "I'll   approve.", "I’ll approve", "승인~", "OK.", "Go\tAhead!~", "ㅇㅇ!",
                      "Do it . !"):
            self.assertIsNotNone(chat_approval(open_, "s", typed, 7200), typed)
        for near in ("yes, and also add X", "yess", "승인해주세요", "approve it", "no way", "ok ok", "!!!", "?",
                     "yes?", "네 그리고 메일도"):
            self.assertIsNone(chat_approval(open_, "s", near, 7200), near)
        self.assertIsNone(chat_approval(self.Open([]), "s", "yes", 7200))  # no open gate: the model answers

    def test_chat_approval_line_says_how_to_get_past_it(self):
        """U32: a short reply to the assistant's own question is swallowed once - the line says how
        to get past it, once, at the end of the last gate's line."""
        one = chat_approval(self.Open([{"id": "a1", "tool": "write_file"}]), "s", "응", 7200)
        self.assertEqual(one, "승인은 채팅으로 받지 않습니다 · approvals are not taken in chat: /approve a1 or /deny a1 "
                              "(write_file; shell: pathfinder approve|deny a1) - if you meant something else, say it "
                              "again · 다른 뜻이었다면 다시 말해 주세요")
        two = chat_approval(self.Open([{"id": "a1", "tool": "write_file"}, {"id": "b2", "tool": "edit_memory"}]),
                            "s", "no", 7200).splitlines()
        self.assertEqual(two, [one.removesuffix(WAY_OUT), one.replace("a1", "b2").replace("write_file", "edit_memory")])


if __name__ == "__main__":
    unittest.main()
