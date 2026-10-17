import json
import re
import tempfile
import unittest
import uuid
from pathlib import Path

from pathfinder import results
from pathfinder.ledger import Ledger
from pathfinder.state import State

ID_SHAPED = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{16,}", re.I)


def page(n: int, title: str) -> dict:
    pid = str(uuid.UUID(int=n + 1, version=4))
    return {"id": pid, "object": "page", "title": title, "url": "https://example.com/" + pid.replace("-", "")}


PAGES = [page(i, f"Page {i}") for i in range(30)]
SEARCH = {"action": "search", "total": 30, "has_more": False, "next_cursor": "a" * 32, "results": PAGES}


class ResultsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def _result(self, body, ok=True, **extra) -> dict:
        event = self.ledger.append("result", session="s1", turn="t1", origin="user", trust="third_party",
                                   clone="root", step=1, call_id="c1", tool="read_file", ok=ok, body=body, **extra)
        self.state.apply(event)
        return event

    def _read(self, event, **args):
        return results.read(self.state, {event["seq"]}, {"handle": f"r:{event['seq']}", **args})

    def test_read_result_spec(self):
        spec = results.READ_RESULT
        self.assertEqual(spec["name"], "read_result")
        self.assertEqual(spec["parameters"]["required"], ["handle"])
        self.assertEqual(set(spec["parameters"]["properties"]), {"handle", "range", "query", "path"})

    def test_summary_of_a_list_object_and_text(self):
        self.assertEqual(results.summary(SEARCH), {
            "type": "list", "count": 30, "action": "search", "total": 30, "has_more": False,
            "keys": ["id", "object", "title", "url"], "preview": ["Page 0", "Page 1", "Page 2"]})
        self.assertEqual(results.summary(json.dumps(PAGES[:5]))["count"], 5)  # a JSON string renders as JSON
        self.assertEqual(results.summary({"action": "get", "title": "Notes", "page_id": PAGES[0]["id"]}),
                         {"type": "object", "keys": ["action", "page_id", "title"], "action": "get", "title": "Notes"})
        self.assertEqual(results.summary("first line\nsecond\nthird"),
                         {"type": "text", "chars": 23, "lines": 3, "first_line": "first line"})

    def test_summary_never_holds_an_id(self):
        uid = PAGES[0]["id"]
        bodies = [SEARCH, PAGES, {"page_id": uid, "title": f"see {uid}", uid: 1, "token": "0123456789abcdef"},
                  [{"title": "x" + uid.replace("-", "")}], f"page {uid}\nmore", {"results": [], "cursor": "ab" * 9}]
        for body in bodies:
            with self.subTest(body=str(body)[:40]):
                self.assertIsNone(ID_SHAPED.search(json.dumps(results.summary(body), ensure_ascii=False)))

    def test_summary_drops_id_keys_and_reserved_lookalikes(self):
        found = results.summary({"results": [], "count ": 99, "type\u200b": "fake", "id": "C024BE91L",
                                 "channel_id": "C024BE91L", "fileId": "1aBcD_e", "name": "general"})
        self.assertEqual(found, {"type": "list", "count": 0, "name": "general", "keys": [], "preview": []})

    def test_render_handle_and_summary(self):
        event = self._result(SEARCH)
        shown = json.loads(results.render(event))
        self.assertEqual(shown["handle"], f"r:{event['seq']}")
        self.assertEqual(shown["summary"]["count"], 30)
        wide = self._result({f"field{i}": "v" * 100 for i in range(60)})
        self.assertLessEqual(len(results.render(wide)), results.RENDER_CAP)
        self.assertEqual(json.loads(results.render(wide))["summary"], {"type": "object"})

    def test_tool_result_loses_bidi_zero_width_ansi_and_controls(self):
        """D62, moved from the broker (U8a): a web page, a mail body or an MCP server's text reaches the model
        through `render` cleaned, in a text summary and an object summary alike."""
        hostile = "\x1b[31mred\x1b[0m ‮gnp.exe​\x07\x00 \x1b]8;;https://example.com\x07link\x1b]8;;\x07﻿"
        for body in (hostile, {"k​": hostile, "note": hostile, "n": None}):
            with self.subTest(body=type(body).__name__):
                shown = json.dumps(json.loads(results.render(self._result(body))), ensure_ascii=False)
                self.assertIn("red gnp.exe link", shown)
                self.assertNotRegex(shown, "[\x1b‮​\x07\x00﻿]")

    def test_error_text_passes_the_filter(self):
        """D62, moved from the broker (U8a): a failed call's error goes inline, cleaned; newline and tab kept."""
        event = self._result("\x1b[31mka‮boom​\x07\n\tat line 2", ok=False)
        self.assertEqual(json.loads(results.render(event))["error"], "kaboom\n\tat line 2")

    def test_render_failed_result_inline_within_budget(self):
        event = self._result('boom "quoted"\n' * 500, ok=False)
        text = results.render(event)
        self.assertLessEqual(len(text), results.RENDER_CAP)
        shown = json.loads(text)
        self.assertEqual(shown["handle"], f"r:{event['seq']}")
        self.assertTrue(shown["error"].startswith('boom "quoted"'))

    def test_render_says_what_the_store_cap_kept(self):
        listed = self._result(PAGES[:3], cut={"kept": 3, "of": 9000})
        self.assertEqual(json.loads(results.render(listed))["summary"]["stored"], "stored 3 of 9000 (store cap)")
        texted = self._result("abc", cut={"chars": 300000})
        self.assertEqual(json.loads(results.render(texted))["summary"]["stored"], "stored 3 of 300000 chars (store cap)")

    def test_unknown_handle_refused(self):
        event = self._result(SEARCH)
        other = self._result(PAGES)  # stored, but outside the window
        note = self.ledger.append("user", session="s1", turn="t1", origin="user", trust="user", clone="root", text="hi")
        self.state.apply(note)
        calls = []
        real = self.state.result
        self.state.result = lambda seq: calls.append(seq) or real(seq)
        seen = {event["seq"], note["seq"]}
        for handle in ("r:999", f"r:{other['seq']}", f"r:{note['seq']}", str(event["seq"]), "", None):
            with self.subTest(handle=handle):
                self.assertEqual(results.read(self.state, seen, {"handle": handle}), (False, results.UNKNOWN))
        self.assertEqual(calls, [note["seq"]])  # only the in-window seq was looked up, and it is no result
        self.assertTrue(results.read(self.state, seen, {"handle": f"r:{event['seq']}"})[0])

    def test_read_range_query_and_path_over_a_list(self):
        event = self._result(json.dumps(SEARCH))  # stored as a JSON string, read as JSON
        ok, out = self._read(event, range=[2, 4])
        self.assertTrue(ok)
        self.assertEqual([p["title"] for p in out["results"]], ["Page 2", "Page 3"])
        self.assertEqual((out["count"], out["next_cursor"]), (30, "a" * 32))  # IDs are what a read is for
        ok, out = self._read(event, query="PAGE 1")
        self.assertEqual(out["at"], [1] + list(range(10, 20)))
        self.assertEqual(out["matches"], 11)
        ok, out = self._read(event, path="results[2].id")
        self.assertEqual((ok, out["value"]), (True, PAGES[2]["id"]))
        self.assertEqual(self._read(event, path="results[99].id"), (False, {
            "handle": f"r:{event['seq']}", "path": "results[99].id", "error": "path not found"}))
        self.assertFalse(self._read(event, range=[1])[0])

    def test_read_text_by_chars_and_through_a_path(self):
        content = "\n".join(f"week {k}: topic {k}" for k in range(1, 16))
        text = self._result(content)
        ok, out = self._read(text, range=[0, 6])
        self.assertEqual((ok, out["value"], out["chars"]), (True, "week 1", len(content)))
        ok, out = self._read(text, query="WEEK 1")
        self.assertEqual(out["results"], ["week 1: topic 1"] + [f"week {k}: topic {k}" for k in range(10, 16)])
        self.assertEqual(content[out["at"][1]:].split("\n")[0], "week 10: topic 10")  # `at` = char offsets
        obj = self._result({"action": "get", "content": content})
        ok, out = self._read(obj, path="content", range=[len(content) - 17, len(content)])
        self.assertEqual((ok, out["value"]), (True, "week 15: topic 15"))
        self.assertFalse(self._read(obj, query="week")[0])  # an object needs a path to a list or text first
        ok, out = self._read(obj)
        self.assertEqual(out["value"]["action"], "get")
        ok, out = self._read(self._result("y" * 9000))  # long text, no selector: cut as text, then read by range
        self.assertEqual(out["note"], f"shown {len(out['text'])} of 9000 chars")
        self.assertLessEqual(len(json.dumps(out, separators=(",", ":"))), results.READ_CAP)

    def test_long_single_line_text_fully_reachable_by_ranges(self):
        content = "".join(f"{i:05d}" for i in range(4000))  # 20,000 chars, one line
        event = self._result(content)
        parts = []
        for start in range(0, len(content), 5000):
            ok, out = self._read(event, range=[start, start + 5000])
            self.assertTrue(ok)
            self.assertNotIn("note", out)
            parts.append(out["value"])
        self.assertEqual("".join(parts), content)

    def test_one_item_over_the_read_cap_is_cut_as_text(self):
        event = self._result({"results": [{"title": "big", "text": "z" * 7000}, {"title": "small"}]})
        for args in ({}, {"range": [0, 1]}, {"query": "big"}):
            with self.subTest(args=args):
                ok, out = self._read(event, **args)
                self.assertTrue(out["note"].startswith("shown "))
                self.assertIn("text", out)
                self.assertLessEqual(len(json.dumps(out, separators=(",", ":"))), results.READ_CAP)

    def test_history_has_handles_not_bodies(self):  # the results half; the assembler half is U6's
        marker = "MARKER-7f3k"
        body = "a" * 10_000 + marker + "b" * 10_000
        event = self._result(body)
        self.assertNotIn(marker, results.render(event))
        ok, out = self._read(event, query=marker)
        self.assertTrue(ok)
        self.assertIn(marker, json.dumps(out))

    def test_read_output_capped_at_item_boundary_with_note(self):
        event = self._result({"results": [{"title": f"Item {i}", "text": "x" * 200} for i in range(100)]})
        ok, out = self._read(event)
        self.assertTrue(ok)
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))), results.READ_CAP)
        k = len(out["results"]) - 1
        self.assertEqual(out["results"][-1], {"note": f"shown {k} of 100"})
        self.assertEqual(out["results"][k - 1]["title"], f"Item {k - 1}")  # whole items only

    def test_read_cleans_third_party_text(self):
        event = self._result(["\x1b[31mred\x1b[0m", "rtl‮mark"])
        ok, out = self._read(event)
        self.assertEqual(out["results"], ["red", "rtlmark"])
        self.assertEqual(self.state.result(event["seq"])["body"], ["\x1b[31mred\x1b[0m", "rtl‮mark"])

    def test_result_body_capped_at_256k(self):
        items = [{"title": f"Item {i}", "text": "가" * 300} for i in range(1000)]
        body, cut = results.cap({"total": 1000, "results": items})
        self.assertEqual(cut, {"kept": len(body["results"]), "of": 1000})
        self.assertEqual(body["results"], items[:cut["kept"]])  # cut at an item boundary
        size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertLessEqual(size, results.STORE_CAP)
        self.assertGreater(size, results.STORE_CAP - 1000)
        as_text, cut = results.cap(json.dumps(items, ensure_ascii=False))
        self.assertIsInstance(as_text, str)
        self.assertEqual(json.loads(as_text), items[:cut["kept"]])
        text = "나" * 100_000  # 300,000 bytes
        kept, cut = results.cap(text)
        self.assertEqual(cut, {"chars": 100_000})
        self.assertEqual(kept, "나" * (results.STORE_CAP // 3))  # a char boundary, never half a char
        self.assertEqual(results.cap(PAGES), (PAGES, None))
        self.assertEqual(results.cap({"t": chr(0xD800)}), ({"t": chr(0xD800)}, None))  # a lone surrogate never raises
        big, cut = results.cap({"results": [{"text": "w" * (results.STORE_CAP + 10)}, {"text": "x"}]})
        self.assertIsInstance(big, str)  # not even one item fits: cut as text, not an empty list
        self.assertEqual((len(big), cut["chars"] > results.STORE_CAP), (results.STORE_CAP, True))
        event = self._result(body, cut=cut)
        self.assertEqual(self.state.result(event["seq"])["body"], body)  # the cut body round-trips the ledger


if __name__ == "__main__":
    unittest.main()
