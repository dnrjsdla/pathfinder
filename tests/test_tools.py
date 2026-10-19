import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import tools as tools_mod
from pathfinder.tools import ToolSpec, coerce, normalize_local_datetime

SPEC = ToolSpec(
    name="thing",
    description="",
    params={
        "s": {"type": "string"},
        "n": {"type": "integer"},
        "f": {"type": "number"},
        "b": {"type": "boolean"},
    },
    required=["s"],
    handler=lambda **a: None,
)


class CoerceTest(unittest.TestCase):
    def test_coerce_casts_declared_types(self):
        args, err = coerce(SPEC, {"s": "hi", "n": "5", "f": "2.5", "b": "true"})
        self.assertEqual(err, "")
        self.assertEqual(args, {"s": "hi", "n": 5, "f": 2.5, "b": True})

    def test_coerce_drops_undeclared_keys(self):
        args, err = coerce(SPEC, {"s": "hi", "extra": "nope"})
        self.assertEqual(err, "")
        self.assertNotIn("extra", args)

    def test_coerce_non_object_arguments_are_refused_not_raised(self):
        spec = ToolSpec(name="t", description="", params={"n": {"type": "integer"}}, required=[], handler=None)
        self.assertEqual(coerce(spec, [1, 2]), ({}, "t: arguments must be an object"))

    def test_coerce_missing_required(self):
        args, err = coerce(SPEC, {})
        self.assertIn("missing required argument 's'", err)

    def test_coerce_uncastable_value(self):
        args, err = coerce(SPEC, {"s": "hi", "n": "not a number"})
        self.assertIn("must be an integer", err)

    def test_coerce_bool_string_false_is_false(self):
        # the bug this exists to prevent: python's bool("false") is True
        args, err = coerce(SPEC, {"s": "hi", "b": "false"})
        self.assertEqual(err, "")
        self.assertIs(args["b"], False)

    def test_coerce_parses_json_for_object_and_array(self):
        spec = ToolSpec(name="t", description="", required=[], handler=None,
                        params={"o": {"type": "object"}, "a": {"type": "array"}})
        self.assertEqual(coerce(spec, {"o": '{"Status": "Done"}', "a": "[1, 2]"}),
                         ({"o": {"Status": "Done"}, "a": [1, 2]}, ""))
        self.assertEqual(coerce(spec, {"o": {"k": 1}, "a": [3]}), ({"o": {"k": 1}, "a": [3]}, ""))  # as sent
        for args, why in (({"o": "not json"}, "'o' must be an object"), ({"o": "[1]"}, "'o' must be an object"),
                          ({"a": '{"k": 1}'}, "'a' must be an array"),
                          ({"a": "[" * 10**6 + "]" * 10**6}, "'a' must be an array")):  # too deep: refused, not raised
            self.assertIn(why, coerce(spec, args)[1])


class LocalDatetimeNormalizeTest(unittest.TestCase):
    def test_local_datetime_normalize(self):
        normalize = normalize_local_datetime("at")
        self.assertEqual(normalize({"at": "2026-09-22T09:00", "text": "x"}),
                         {"at": "2026-09-22 09:00", "text": "x"})
        self.assertEqual(normalize({"other": "unchanged"}), {"other": "unchanged"})  # key absent: no-op

    def test_local_datetime_normalize_refuses_offset_and_z(self):
        normalize = normalize_local_datetime("at")
        for bad in ("2026-09-22T09:00Z", "2026-09-22 09:00+09:00", "2026-09-22 09:00-05:00"):
            with self.assertRaises(ValueError):
                normalize({"at": bad})


class RunCommandCheckTest(unittest.TestCase):
    def _check(self, command):
        return tools_mod.TOOLS["run_command"].normalize({"command": command})

    def test_run_command_refuses_a_tool_name_as_a_command(self):
        with self.assertRaisesRegex(ValueError, "`read_file` is a tool, not a shell command: call it directly"):
            self._check("read_file ~/notes.txt")
        with mock.patch.dict(tools_mod.OFF_TOOLS, {"list_mail": "mail"}):
            with self.assertRaisesRegex(ValueError, "`list_mail` is a tool"):
                self._check("list_mail --limit 5")
        self.assertEqual(self._check("ls -la"), {"command": "ls -la"})

    def test_run_command_refuses_restarting_the_daemon(self):
        for command in ("pathfinder service restart", "python3 -m pathfinder service stop",
                        "launchctl kickstart -k gui/1000/pathfinder.daemon", "launchctl bootout gui/1000/pathfinder.daemon",
                        "launchctl unload ~/Library/LaunchAgents/pathfinder.daemon.plist",
                        "launchctl load -w ~/Library/LaunchAgents/pathfinder.daemon.plist"):
            with self.assertRaisesRegex(ValueError, "restarting PATHFINDER ends this turn", msg=command):
                self._check(command)
        for command in ("pathfinder service status", "launchctl list | grep pathfinder", "launchctl unload other.plist", "echo restart"):
            self.assertEqual(self._check(command), {"command": command})


def _oneline_before_u31(text, n=None):
    """`tools.oneline` as it was before U31, kept to prove the new one is byte-identical."""
    text = tools_mod._ANSI_RE.sub("", str(text))
    text = "".join(" " if ch.isspace() else ch for ch in text if ch.isspace() or ch.isprintable())
    return " ".join(text.split())[:n]


class CleanTest(unittest.TestCase):
    def test_oneline_is_clean_then_flatten(self):
        """D62: one character rule. `oneline` is `clean` plus flattening, and its output is unchanged."""
        every_bmp = "".join(chr(c) for c in range(0xD800)) + "".join(chr(c) for c in range(0xE000, 0x10000))
        corpus = [every_bmp, "\ud800 lone", "👩\u200d💻 \U0001F600\U000E0001\U0010FFFF", None, 42,
                  "\x1b[31mred\x1b[0m \x1b]0;title\x07 \x1b]8;;u\x1b\\x \x1b[ half \x1b", "  \r\n\t\v\f\x1c\x85\u2028 a  \n b\u3000c  "]
        corpus += [chr(c) + " x\ty " + chr(c) for c in range(0x10000)]
        for text in corpus:
            for n in (None, 0, 5, 300):
                self.assertEqual(tools_mod.oneline(text, n), _oneline_before_u31(text, n), repr(text)[:80])
                self.assertEqual(tools_mod.oneline(text, n), " ".join(tools_mod.clean(text).split())[:n])
        self.assertEqual(tools_mod.clean("a\u200d\tb\r\n  c\x1b[1m\rd"), "a\tb\n  c d")
        self.assertEqual(tools_mod.clean("👩\u200d💻"), "👩💻")  # D62's reversal clause: the joiner goes

    def test_read_file_returns_the_file_up_to_its_hard_cap(self):
        """v2: no 8,000-char clip (the store keeps and renders the result); a hard cap still bounds a read."""
        read = tools_mod.TOOLS["read_file"].handler
        with tempfile.TemporaryDirectory() as tmp:
            page, huge = Path(tmp) / "page.json", Path(tmp) / "huge.txt"
            page.write_text("x" * 20000, encoding="utf-8")
            huge.write_text("y" * (tools_mod.READ_CAP + 5), encoding="utf-8")
            self.assertEqual(read(str(page)), "x" * 20000)
            self.assertEqual(read(str(huge)), "y" * tools_mod.READ_CAP)


if __name__ == "__main__":
    unittest.main()
