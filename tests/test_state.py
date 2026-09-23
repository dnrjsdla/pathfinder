import tempfile
import time
import unittest
from pathlib import Path

from pathfinder import clock
from pathfinder.ledger import Ledger
from pathfinder.state import State


class StateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.db_path = self.home / "state.db"
        self.state = State(self.db_path)

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def _record(self, type, **kw):
        event = self.ledger.append(type, **kw)
        self.state.apply(event)
        return event

    def _build_conversation(self):
        self._record("session_start", session="s1")
        self._record("user", session="s1", turn="t1", origin="user", trust="user", text="hi")
        self._record("turn_start", session="s1", turn="t1", origin="user")
        self._record("assistant", session="s1", turn="t1", text="not final yet")  # no final flag
        self._record("assistant", session="s1", turn="t1", final=True, text="hello back")
        self._record("turn_end", session="s1", turn="t1", origin="user")

    def test_reindex_restores_history(self):
        self._build_conversation()
        before = self.state.history("s1")
        self.assertEqual(before, [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello back"},
        ])

        self.state.db.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                p.unlink()

        rebuilt = State(self.db_path)
        rebuilt.rebuild(self.ledger)
        self.assertEqual(rebuilt.history("s1"), before)

        # apply is idempotent: replaying an already-applied event changes nothing
        last_event = list(self.ledger.read())[-1]
        rebuilt.apply(last_event)
        rebuilt.apply(last_event)
        self.assertEqual(rebuilt.history("s1"), before)

    def test_restore_said_only(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="signal", trust="user",
                      text="a signal envelope, not a said thing")
        self._record("assistant", session="s2", turn="t1", text="mid-turn tool chatter")  # no final
        self._record("user", session="s2", turn="t1", origin="user", trust="user",
                      text="the real thing said")
        self._record("assistant", session="s2", turn="t1", final=True, text="the real reply")

        self.assertEqual(self.state.history("s2"), [
            {"role": "user", "content": "the real thing said"},
            {"role": "assistant", "content": "the real reply"},
        ])

    def test_session(self):
        self._record("session_start", session="s3")
        self._record("turn_end", session="s3", turn="t1", origin="user")
        info = self.state.session("s3")
        self.assertEqual(info["id"], "s3")
        self.assertEqual(info["turns"], 1)
        self.assertIn("started_ts", info)
        self.assertIn("last_ts", info)
        self.assertIsNone(self.state.session("no-such-session"))

    def test_apply_is_idempotent_where_it_can_actually_break(self):
        """history() cannot double up (INSERT OR IGNORE); the turn counter can."""
        self._build_conversation()
        for event in self.ledger.read():
            self.state.apply(event)  # redelivery of everything
        self.assertEqual(self.state.session("s1")["turns"], 1)

    def test_two_states_on_one_db_count_each_turn_once(self):
        """The daemon and a second process projecting the same ledger into one DB."""
        events = [
            self.ledger.append("session_start", session="s1"),
            self.ledger.append("user", session="s1", turn="t1", origin="user", trust="user", text="hi"),
            self.ledger.append("turn_end", session="s1", turn="t1", origin="user"),
        ]
        other = State(self.db_path)
        for event in events:
            self.state.apply(event)
            other.apply(event)
        self.assertEqual(other.session("s1")["turns"], 1)
        self.assertEqual(len(self.state.history("s1")), 1)
        other.close()

    def test_old_approvals_table_gains_job_column(self):
        """Review blocker: a home from before partition 4 has `approvals` without `job`;
        CREATE TABLE IF NOT EXISTS leaves it that way, so the daemon must migrate or it dies
        on its first `pending()`."""
        self.state.close()
        self.db_path.unlink()
        import sqlite3
        db = sqlite3.connect(self.db_path)
        db.execute("CREATE TABLE approvals (id TEXT PRIMARY KEY, call_id TEXT, session TEXT, turn TEXT, "
                   "tool TEXT, args_json TEXT, messages_json TEXT, ts REAL, decision TEXT)")
        db.execute("INSERT INTO approvals VALUES ('a1', 'c1', 's1', 't1', 'write_file', '{}', '[]', 1.0, NULL)")
        db.commit()
        db.close()
        self.state = State(self.db_path)
        self.assertEqual([p["id"] for p in self.state.pending()], ["a1"])
        self.assertIsNone(self.state.pending()[0]["job"])


class PageIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.db_path = self.home / "state.db"
        self.state = State(self.db_path)

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def test_search_fts_prefix_matches_korean_particles(self):
        self.state.index_page("wiki/a.md", "A", "캘린더가 비어 있다", 1.0)
        self.state.index_page("wiki/b.md", "B", "자전거 체인 정비", 2.0)
        hits = self.state.search("캘린더")
        self.assertEqual([h["path"] for h in hits], ["wiki/a.md"])

    def test_search_hybrid_rrf_order(self):
        # FTS matches page A only ("사과" -> a's text); vector ranks page B above A.
        self.state.index_page("wiki/a.md", "A", "사과 바나나", 1.0, vector=[0.0, 1.0, 0.0], model="m")
        self.state.index_page("wiki/b.md", "B", "포도 딸기", 2.0, vector=[1.0, 0.0, 0.0], model="m")
        hits = self.state.search("사과", query_vector=[1.0, 0.0, 0.0], model="m", top_k=2)
        self.assertEqual([h["path"] for h in hits], ["wiki/a.md", "wiki/b.md"])

        fts_only = self.state.search("사과", query_vector=None, top_k=2)
        self.assertEqual([h["path"] for h in fts_only], ["wiki/a.md"])

    def test_search_ignores_vectors_of_another_model(self):
        self.state.index_page("wiki/a.md", "A", "무관한 내용", 1.0, vector=[1.0, 0.0, 0.0], model="x")
        hits = self.state.search("no-match-token", query_vector=[1.0, 0.0, 0.0], model="y")
        self.assertEqual(hits, [])

    def test_index_page_without_vector_drops_stale_vector(self):
        self.state.index_page("wiki/a.md", "A", "text one", 1.0, vector=[1.0, 0.0, 0.0], model="m")
        self.state.index_page("wiki/a.md", "A", "text two", 2.0)  # re-indexed with no embedding
        row = self.state.db.execute("SELECT 1 FROM vectors WHERE path = ?", ("wiki/a.md",)).fetchone()
        self.assertIsNone(row)

    def test_rebuild_drops_pages(self):
        self.state.index_page("wiki/a.md", "A", "text", 1.0)
        self.assertEqual(len(self.state.pages()), 1)
        self.state.rebuild(self.ledger)
        self.assertEqual(self.state.pages(), [])


class MemoryStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.db_path = self.home / "state.db"
        self.state = State(self.db_path)

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def _record(self, type, **kw):
        event = self.ledger.append(type, **kw)
        self.state.apply(event)
        return event

    def _reopen(self):
        """Delete the DB file and rebuild a fresh State from the ledger."""
        self.state.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                p.unlink()
        fresh = State(self.db_path)
        fresh.rebuild(self.ledger)
        return fresh

    def test_history_folds_after_summary(self):
        self._record("session_start", session="s1")
        seqs = [self._record("user", session="s1", turn="t1", origin="user", trust="user",
                             text=f"u{i}")["seq"] for i in range(6)]
        self.assertEqual(len(self.state.history("s1")), 6)

        self._record("session_summary", session="s1", upto=seqs[2], text="summary text")
        after = self.state.history("s1")
        self.assertEqual(after[0], {"role": "system",
                                    "content": "Earlier in this conversation, summarized: summary text"})
        self.assertEqual([m["content"] for m in after[1:]], ["u3", "u4", "u5"])

    def test_memory_cursor_follows_pass(self):
        self.assertEqual(self.state.memory_cursor, 0)
        self._record("memory_pass", origin="memory", since=0, upto=42, read=5, applied=1, proposed=0)
        self.assertEqual(self.state.memory_cursor, 42)

        rebuilt = self._reopen()
        self.assertEqual(rebuilt.memory_cursor, 42)
        rebuilt.close()

    def test_memory_stats_counts_by_local_day(self):
        today_start, yesterday_start = clock.day_starts(time.time(), 2)
        seq = [0]

        def apply(type, ts, **data):
            seq[0] += 1
            self.state.apply({"seq": seq[0], "ts": ts, "type": type, "session": "", "turn": "",
                              "origin": "memory", "trust": "self", **data})

        apply("memory_pass", today_start + 10, since=0, upto=5, read=5, applied=1, proposed=0)
        apply("memory_apply", today_start + 20, target="wiki/a.md", op="add", old=None, new="x", approval=None)
        apply("memory_pass", yesterday_start + 10, since=0, upto=3, read=2, applied=0, proposed=1)
        apply("judgment", today_start + 5)
        apply("judgment", yesterday_start + 5)

        stats = self.state.memory_stats(time.time())
        self.assertEqual(len(stats["days"]), 3)
        self.assertEqual(stats["days"][0], {"start": today_start, "passes": 1, "read": 5, "applied": 1})
        self.assertEqual(stats["days"][1]["passes"], 1)
        self.assertEqual(stats["days"][1]["read"], 2)
        self.assertEqual(stats["days"][1]["applied"], 0)
        self.assertEqual(stats["days"][2], {"start": clock.day_starts(time.time(), 3)[2], "passes": 0,
                                            "read": 0, "applied": 0})
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(len(stats["judgments"]), 7)
        self.assertEqual(stats["judgments"][0], 1)
        self.assertEqual(stats["judgments"][1], 1)
        self.assertEqual(stats["judgments"][2], 0)

    def test_reindex_restores_summaries_and_applies(self):
        self._record("session_start", session="s1")
        self._record("user", session="s1", turn="t1", origin="user", trust="user", text="hi")
        self._record("session_summary", session="s1", upto=1, text="summary")
        self._record("memory_pass", origin="memory", since=0, upto=1, read=1, applied=1, proposed=0)
        self._record("memory_apply", origin="memory", target="wiki/a.md", op="add", old=None, new="x")

        rebuilt = self._reopen()
        self.assertEqual(rebuilt.summary("s1"), {"upto": 1, "text": "summary"})
        self.assertEqual(rebuilt.memory_cursor, 1)
        applies = rebuilt.memory_applies_after(0)
        self.assertEqual(len(applies), 1)
        self.assertEqual(applies[0]["target"], "wiki/a.md")
        rebuilt.close()


if __name__ == "__main__":
    unittest.main()
