"""mirror.py - the registry copy. A `ThreadingHTTPServer` stands in for the registry: cursor
paging over `items`, and `changed` for any request carrying `updated_since`."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from pathfinder import catalog, mirror

OFFICIAL = "io.modelcontextprotocol.registry/official"


def _item(name, desc="", status="active"):
    return {"server": {"name": name, "description": desc, "version": "1.0.0"},
            "_meta": {OFFICIAL: {"status": status, "updatedAt": "2026-09-20T00:00:00Z"}}}


def _serve(items, changed=(), size=2):
    class H(BaseHTTPRequestHandler):
        requests = []
        page_size = size
        fail_after = None  # answer `fail_code` once this many requests have been seen
        fail_code = 500
        bad_cursor = "gone"  # the cursor the registry answers 400 to
        repeat = False  # always name the same next cursor

        def log_message(self, *a):
            pass

        def do_GET(self):
            cls = type(self)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cls.requests.append(q)
            cur = q.get("cursor", [""])[0]
            if cls.fail_after is not None and len(cls.requests) > cls.fail_after:
                code, body = cls.fail_code, b"{}"
            elif cur == cls.bad_cursor:
                code, body = 400, b"{}"
            else:
                rows = list(changed if "updated_since" in q else items)
                start = int(cur) if cur.isdigit() else 0
                size = cls.page_size
                meta = {"nextCursor": "same" if cls.repeat else str(start + size)} if start + size < len(rows) else {}
                code, body = 200, json.dumps({"servers": rows[start:start + size], "metadata": meta}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, H, f"http://127.0.0.1:{httpd.server_port}"


class MirrorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.items = [_item("x/brave-search-mcp", "Web search through Brave"), _item("io.github.a/mailer", "Read gmail"),
                      _item("io.github.b/weather", "Forecasts"), _item("io.github.c/notes"), _item("io.github.d/last")]
        self.httpd, self.H, self.url = _serve(self.items)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        patcher = mock.patch.object(mirror, "RETRY_DELAYS", (0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self, registry=None):
        mirror.refresh(self.home, registry or self.url)
        mirror._threads[str(self.home)].join(10)

    def meta(self):
        return dict(mirror._q(self.home, "SELECT key, value FROM meta"))

    def count(self):
        return mirror._q(self.home, "SELECT COUNT(*) FROM servers")[0][0]

    def test_first_sync_pages_into_the_copy_and_completes(self):
        self.sync()
        self.assertEqual(self.count(), 5)
        self.assertEqual(len(self.H.requests), 3)
        self.assertTrue(all(r["version"] == ["latest"] and "updated_since" not in r for r in self.H.requests))
        m = self.meta()
        self.assertEqual((m["complete"], m["cursor"], m["failed_at"]), ("1", "", ""))
        self.assertEqual(mirror._q(self.home, "SELECT MAX(seen) FROM servers")[0][0], 0)
        self.assertAlmostEqual(float(m["since"]), float(m["started_at"]) - 3600)

    def test_first_sync_resumes_from_the_stored_cursor(self):
        db = mirror._open(self.home)
        mirror._put(db, registry=self.url, cursor="2", started_at=1000.0)
        db.close()
        self.sync()
        self.assertEqual(self.H.requests[0]["cursor"], ["2"])
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.meta()["since"], str(1000.0 - 3600))

    def test_incremental_sync_uses_updated_since_and_marks_seen(self):
        self.sync()
        since = float(self.meta()["since"])
        changed = _item("io.github.c/notes", "Notes, now with words")
        changed["_meta"][OFFICIAL]["updatedAt"] = "2026-09-25T00:00:00Z"  # a real change moves updatedAt
        # the fake serves `changed` for updated_since requests: a second server carries it
        httpd, H, url = _serve(self.items, [changed])
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        db = mirror._open(self.home)
        mirror._put(db, registry=url)
        db.close()
        self.sync(url)
        self.assertEqual(H.requests[0]["updated_since"], [catalog.rfc3339(since)])
        self.assertEqual([i["server"]["name"] for i in mirror.changed_since(self.home, 0)], ["io.github.c/notes"])

    def test_search_matches_words_in_name_and_description(self):
        self.sync()
        names = lambda q: [i["server"]["name"] for i in mirror.search(self.home, q)[0]]  # noqa: E731
        self.assertEqual(names("brave search")[0], "x/brave-search-mcp")
        self.assertEqual(names("gmail"), ["io.github.a/mailer"])
        self.assertEqual(names("forec"), ["io.github.b/weather"])
        self.assertEqual(names("!!"), [])

    def test_awkward_queries_do_not_raise(self):
        self.sync()
        for q in ('say"hi', '"', 'brave*', '-brave', 'a:b', 'NEAR', 'NEAR(a b)', '(brave)', 'brave OR', "AND OR NOT", "'; --"):
            with self.subTest(q=q):
                self.assertIsInstance(mirror.search(self.home, q)[0], list)

    def test_query_terms_are_deduped_capped_and_short_ones_exact(self):
        self.assertEqual(mirror._match("a " * 2000), '"a"')
        self.assertEqual(mirror._match(" ".join(f"w{i}xx" for i in range(2000))).count(" OR "), mirror.MAX_TERMS - 1)
        self.assertEqual(mirror._match("my weather"), '"weather"* OR "my"')

    def test_the_term_cap_keeps_the_longest_words(self):
        terms = mirror._match("could you please help me find a server for gmail")
        self.assertIn('"gmail"*', terms)
        self.assertEqual(terms.count(" OR "), mirror.MAX_TERMS - 1)
        self.assertNotIn('"a"', terms)

    def test_fresh_db_opened_by_many_threads_at_once(self):
        for round_ in range(40):
            home = self.home / f"r{round_}"
            home.mkdir()
            errors, barrier = [], threading.Barrier(8)

            def work(i):
                barrier.wait()
                try:
                    (mirror.search, mirror.find, mirror.changed_since, mirror.search)[i % 4](home, *(("x",), ("x",), (0,), ("x",))[i % 4])
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
            threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(errors, [], f"round {round_}")
            self.assertEqual(mirror._q(home, "PRAGMA journal_mode")[0][0], "wal")

    def test_429_on_a_stored_cursor_keeps_it(self):
        db = mirror._open(self.home)
        mirror._put(db, registry=self.url, cursor="2", started_at=1000.0)
        db.close()
        self.H.fail_after, self.H.fail_code = 0, 429
        self.assert_ended_with_failure("429", 1)
        self.assertEqual(self.meta()["cursor"], "2")

    def test_sentence_query_ranks_the_weather_server_first(self):
        self.items.append(_item("io.github.e/inbox-myriad-issue", "Inbound items, issue lists, my items"))
        self.sync()
        top = mirror.search(self.home, "what is the weather in my city")[0][0]
        self.assertEqual(top["server"]["name"], "io.github.b/weather")

    def test_search_note_while_incomplete_and_after_a_failure(self):
        self.assertIn("several minutes", mirror.search(self.home, "x")[1])
        db = mirror._open(self.home)
        mirror._put(db, started_at=1000.0)
        db.execute("INSERT INTO servers (name, description, status, seen, item) VALUES ('a/b', '', 'active', 0, '{}')")
        db.close()
        self.assertRegex(mirror.search(self.home, "x")[1], r"still syncing \(1 so far, since \d\d:\d\d\)")
        db = mirror._open(self.home)
        mirror._put(db, failed_at=1000.0, error="HTTP Error 500")
        db.close()
        self.assertRegex(mirror.search(self.home, "x")[1], r"failed at \d\d:\d\d: HTTP Error 500")
        self.sync()
        self.assertEqual(mirror.search(self.home, "x")[1], "")

    def test_search_restarts_a_stalled_sync(self):
        self.assertEqual(self.count(), 0)
        mirror.search(self.home, "brave", self.url)
        mirror._threads[str(self.home)].join(10)
        self.assertEqual(self.meta()["complete"], "1")
        self.assertEqual(self.count(), 5)

    def test_failed_page_keeps_cursor_and_records_error(self):
        self.H.fail_after = 1
        with self.assertLogs("pathfinder.mirror", "WARNING"):
            self.sync()
        m = self.meta()
        self.assertEqual((m["cursor"], m.get("complete", "0")), ("2", "0"))
        self.assertIn("500", m["error"])
        self.assertTrue(m["failed_at"])
        self.assertEqual(self.count(), 2)
        self.H.fail_after = None
        self.sync()
        self.assertEqual((self.count(), self.meta()["failed_at"]), (5, ""))

    def test_a_stopped_sync_retries_once_after_a_pause_and_the_note_says_syncing(self):
        """D70: the retry comes by itself; a run that has begun clears the failure note; a retry that
        fails too schedules nothing more (the next search or the 24 h poll takes over)."""
        self.H.fail_after = 1
        timers = []
        real_timer = threading.Timer
        def timer(interval, fn, args):
            timers.append(interval)
            return real_timer(0.05, fn, args)
        with mock.patch.object(threading, "Timer", timer), self.assertLogs("pathfinder.mirror", "WARNING"):
            self.sync()
            self.assertEqual(timers, [mirror.RETRY_AFTER_FAILURE_S])
            self.H.fail_after = None
            for _ in range(100):  # the retry's own thread replaces the finished one
                time.sleep(0.02)
                th = mirror._threads.get(str(self.home))
                if th is not None and th.is_alive():
                    break
            mirror._threads[str(self.home)].join(10)
        self.assertEqual((self.count(), self.meta()["failed_at"]), (5, ""))
        # a failing retry schedules no second retry
        self.H.fail_after = 0
        timers.clear()
        with mock.patch.object(threading, "Timer", timer), self.assertLogs("pathfinder.mirror", "WARNING"):
            mirror.refresh(self.home, self.url, True)
            mirror._threads[str(self.home)].join(10)
        self.assertEqual(timers, [])
        self.assertTrue(self.meta()["failed_at"])
        # a run that has begun clears the failure before its first page: the note says syncing, not failed
        puts, real_put = [], mirror._put
        with mock.patch.object(mirror, "_put", lambda db, **kv: (puts.append(kv), real_put(db, **kv))), \
                self.assertLogs("pathfinder.mirror", "WARNING"):
            mirror._sync(self.home, self.url, True)
        self.assertEqual(puts[0].get("failed_at"), "")
        self.assertTrue(puts[-1]["failed_at"])

    def test_bad_cursor_restarts_the_first_sync(self):
        db = mirror._open(self.home)
        mirror._put(db, registry=self.url, cursor="gone")
        db.close()
        self.sync()
        self.assertEqual(self.count(), 5)
        self.assertEqual(self.meta()["complete"], "1")
        self.assertEqual(len(self.H.requests), 4)  # the refused cursor, then three pages

    def assert_ended_with_failure(self, text, max_requests):
        with self.assertLogs("pathfinder.mirror", "WARNING"):
            self.sync()
        self.assertLessEqual(len(self.H.requests), max_requests)
        self.assertTrue(self.meta()["failed_at"])
        self.assertIn(text, self.meta()["error"])

    def test_4xx_on_a_cursor_from_this_run_ends_the_sync(self):
        self.H.bad_cursor = "4"
        self.assert_ended_with_failure("400", 3)

    def test_429_ends_the_sync_without_retries(self):
        self.H.page_size, self.H.fail_after, self.H.fail_code = 1, 3, 429
        self.assert_ended_with_failure("429", 4)

    def test_repeated_cursor_ends_the_sync(self):
        self.H.repeat = True
        self.assert_ended_with_failure("cursor", 2)

    def test_page_cap_ends_the_sync(self):
        self.H.page_size = 1
        with mock.patch.object(mirror, "MAX_PAGES", 3):
            self.assert_ended_with_failure("more than 3 pages", 3)

    def test_open_failure_is_logged_and_stored_when_it_can_be(self):
        real, calls = mirror._open, []

        def flaky(home):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(home)
        with mock.patch.object(mirror, "_open", flaky), self.assertLogs("pathfinder.mirror", "WARNING"):
            mirror._sync(self.home, self.url)
        self.assertIn("locked", self.meta()["error"])
        with mock.patch.object(mirror, "_open", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs("pathfinder.mirror", "WARNING"):
            mirror._sync(self.home, self.url)  # nowhere to store it: no exception, only the warning

    def test_error_text_is_one_short_line(self):
        with mock.patch.object(mirror.catalog, "page", side_effect=OSError("a\nb " + "x" * 500)):
            with self.assertLogs("pathfinder.mirror", "WARNING"):
                mirror._sync(self.home, self.url)
        e = self.meta()["error"]
        self.assertTrue(e.startswith("a b x") and "\n" not in e and len(e) <= 200)

    def test_other_registry_empties_the_copy(self):
        self.sync()
        httpd, _, url = _serve([_item("y/only")])
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.sync(url)
        self.assertEqual([i["server"]["name"] for i in mirror.search(self.home, "only")[0]], ["y/only"])
        self.assertEqual(self.count(), 1)
        self.assertEqual(mirror.search(self.home, "brave")[0], [])

    def test_find_exact_then_suffix(self):
        self.sync()
        self.assertEqual([i["server"]["name"] for i in mirror.find(self.home, "x/brave-search-mcp")], ["x/brave-search-mcp"])
        self.assertEqual([i["server"]["name"] for i in mirror.find(self.home, "weather")], ["io.github.b/weather"])
        self.assertEqual(mirror.find(self.home, "eather"), [])

    def test_deleted_row_is_dropped_on_receipt(self):
        self.sync()
        httpd, _, url = _serve(self.items, [_item("io.github.c/notes", status="deleted")])
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        db = mirror._open(self.home)
        mirror._put(db, registry=url)
        db.close()
        self.sync(url)
        self.assertEqual(mirror.find(self.home, "notes"), [])
        self.assertEqual(self.count(), 4)

    def test_deprecated_not_searched_but_found_by_name(self):
        self.items[3] = _item("io.github.c/notes", "Old notes", status="deprecated")
        self.sync()
        self.assertEqual(mirror.search(self.home, "notes")[0], [])
        self.assertEqual(len(mirror.find(self.home, "notes")), 1)
        self.assertEqual(len(mirror.changed_since(self.home, -1)), 4)  # the deprecated row is no candidate

    def test_an_unchanged_row_fetched_again_is_not_new(self):
        db = mirror._open(self.home)
        item = _item("io.github.e/again")
        mirror._put(db, complete=1)
        mirror._upsert(db, item, 100)
        mirror._upsert(db, item, 200)  # the overlap hour brings it back
        self.assertEqual(mirror.changed_since(self.home, 150), [])
        item["_meta"][OFFICIAL]["updatedAt"] = "2026-09-25T00:00:00Z"
        mirror._upsert(db, item, 300)
        self.assertEqual(len(mirror.changed_since(self.home, 250)), 1)
        self.assertEqual(len(mirror.search(self.home, "again")[0]), 1)  # delete-then-insert kept the index right
        db.close()

    def test_db_is_0600(self):
        self.sync()
        self.assertEqual(os.stat(self.home / "registry.db").st_mode & 0o777, 0o600)

    def test_sync_thread_is_a_daemon_thread(self):
        mirror.refresh(self.home, self.url)
        t = mirror._threads[str(self.home)]
        self.assertTrue(t.daemon)
        t.join(10)


if __name__ == "__main__":
    unittest.main()
