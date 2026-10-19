import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

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

    def _gate(self, id, name, args, **kw):
        self._record("approval_pending", session="s2", turn="t1", origin="user", id=id, call_id="m-" + id,
                     tool=name, args=args, messages=[{"role": "user", "content": "x"}], **kw)

    def _decide(self, id, decision):
        self._record("approval", session="s2", turn="t1", origin="user", id=id, decision=decision)

    def test_open_approvals(self):
        """D52: pending rows of this session newer than its last user message."""
        self._record("session_start", session="s2")
        self._record("approval_pending", session="s2", origin="user", id="talked", call_id="c0", tool="x",
                     args={}, messages=[])
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._gate("gate", "send_mail", {})
        self._record("approval_pending", session="s2", origin="user", id="decided", call_id="c1", tool="x",
                     args={}, messages=[])
        self._decide("decided", "denied")
        self._record("approval_pending", session="s3", origin="user", id="other", call_id="c2", tool="x",
                     args={}, messages=[])
        self.assertEqual([p["id"] for p in self.state.open_approvals("s2")], ["gate"])
        # no user line yet
        self.assertEqual([p["id"] for p in self.state.open_approvals("s3")], ["other"])
        self._record("user", session="s2", turn="t2", origin="user", trust="user", text="something else")
        self.assertEqual(self.state.open_approvals("s2"), [])

    def test_presence_is_the_newest_row_across_sessions(self):
        """5a: the owner has one pair of hands - a screen moved into a new session typed in the old one."""
        self.assertIsNone(self.state.presence())
        self._record("presence", session="s1", origin="user", idle_s=0)
        second = self._record("presence", session="s2", origin="user", idle_s=50)
        self.assertEqual(self.state.presence(), {"ts": second["ts"], "idle_s": 50})
        again = self._record("presence", session="s1", origin="user", idle_s=7)
        self.assertEqual(self.state.presence(), {"ts": again["ts"], "idle_s": 7})
        self.assertFalse(self.state.today(idle_threshold_s=20).away)  # fresh and typing, whichever session

    def test_session(self):
        self._record("session_start", session="s3")
        self._record("turn_end", session="s3", turn="t1", origin="user")
        info = self.state.session("s3")
        self.assertEqual(info["id"], "s3")
        self.assertEqual(info["turns"], 1)
        self.assertIn("started_ts", info)
        self.assertIn("last_ts", info)
        self.assertIsNone(self.state.session("no-such-session"))

    def test_memory_origin_events_do_not_move_last_ts(self):
        self._record("session_start", session="s1")
        self._record("user", session="s1", turn="t1", origin="user", trust="user", text="hi")
        before = self.state.session("s1")["last_ts"]
        self._record("memory_apply", session="s1", origin="memory", target="wiki/a.md", op="add", old=None, new="x")
        after = self.state.session("s1")["last_ts"]
        self.assertEqual(before, after)  # a pass's own writing must not look like the user is still there

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

    def test_old_approvals_table_gains_seq_column(self):
        """A home from before v2 has `approvals` without `seq` (and with v1's `job`); CREATE TABLE
        IF NOT EXISTS leaves it that way, so State adds the column or `approve <seq>` finds nothing."""
        self.state.close()
        self.db_path.unlink()
        import sqlite3
        db = sqlite3.connect(self.db_path)
        db.execute("CREATE TABLE approvals (id TEXT PRIMARY KEY, call_id TEXT, session TEXT, turn TEXT, "
                   "tool TEXT, args_json TEXT, messages_json TEXT, ts REAL, decision TEXT, job TEXT)")
        db.commit()
        db.close()
        self.state = State(self.db_path)
        self.assertIn("seq", {r[1] for r in self.state.db.execute("PRAGMA table_info(approvals)")})
        event = self._record("approval_pending", session="s1", origin="user", id="q1", call_id="q1",
                             tool="edit_memory", args={}, messages=[])
        item = self.state.approval_item(event["seq"])
        self.assertEqual((item["kind"], item["id"]), ("proposal", "q1"))

    def test_reindex_drops_v1_dead_tables(self):
        """U10a: `pathfinder reindex` on a home from before v2 removes v1's jobs, summaries and memory_passes."""
        self.state.db.executescript("CREATE TABLE jobs (id TEXT); CREATE TABLE summaries (session TEXT); "
                                    "CREATE TABLE memory_passes (ts REAL);")
        self.state.rebuild(self.ledger)
        tables = {r[0] for r in self.state.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables & {"jobs", "summaries", "memory_passes"}, set())

    def test_context_cwds_reader(self):
        """The catalog's work-context tags read this - `attach:<cwd>` keys of `context` signals,
        at or after `since`; a signal of another kind (even one that also has a `key`) is left out."""
        self._record("signal", origin="signal", trust="self", sense="attach", kind="context",
                     key="attach:/home/user/proj", summary="working", fields={"cwd": "/home/user/proj"})
        self._record("signal", origin="signal", trust="third_party", sense="mail", kind="new_mail",
                     key="not-a-context-key", summary="mail", fields={})
        since = time.time() - 10
        self.assertEqual(self.state.context_cwds(since), {"/home/user/proj"})
        self.assertEqual(self.state.context_cwds(time.time() + 10), set())

    def test_db_wal_shm_born_0600(self):
        old = os.umask(0o022)  # the usual umask, under which SQLite alone creates the db 0644
        try:
            state = State(self.home / "fresh.db")
            state.apply(self.ledger.append("session_start", session="s1"))  # a write: -wal and -shm exist
            for suffix in ("", "-wal", "-shm"):
                self.assertEqual(os.stat(f"{self.home / 'fresh.db'}{suffix}").st_mode & 0o777, 0o600, suffix)
            state.close()
        finally:
            os.umask(old)


class PinsTest(unittest.TestCase):
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

    def test_pin_accept_upserts_and_pins_reads_it_back(self):
        self._record("pin_accept", server="gh", tool="alpha",
                     description_hash="sha256:d1", schema_hash="sha256:s1", params=["owner"])
        self.assertEqual(self.state.pins(), {
            "gh": {"alpha": {"description": "sha256:d1", "schema": "sha256:s1", "annotations": None,
                             "params": ["owner"]}},  # a pin with no annotations digest: one from before P2
        })
        # a second accept for the same (server, tool) replaces, never duplicates
        self._record("pin_accept", server="gh", tool="alpha", description_hash="sha256:d2", schema_hash="sha256:s1",
                     annotations_hash="sha256:a1", params=["owner"])
        self.assertEqual(self.state.pins()["gh"]["alpha"]["description"], "sha256:d2")
        self.assertEqual(self.state.pins()["gh"]["alpha"]["annotations"], "sha256:a1")

    def test_pin_revoke_forgets_the_server(self):
        self._record("pin_accept", server="gh", tool="alpha",
                     description_hash="sha256:d1", schema_hash="sha256:s1", params=[])
        self._record("pin_accept", server="gh", tool="beta",
                     description_hash="sha256:d2", schema_hash="sha256:s2", params=[])
        self._record("pin_revoke", server="gh")  # tool omitted - the whole server is forgotten
        self.assertEqual(self.state.pins(), {})

    def test_pin_revoke_one_tool_leaves_the_rest(self):
        self._record("pin_accept", server="gh", tool="alpha",
                     description_hash="sha256:d1", schema_hash="sha256:s1", params=[])
        self._record("pin_accept", server="gh", tool="beta",
                     description_hash="sha256:d2", schema_hash="sha256:s2", params=[])
        self._record("pin_revoke", server="gh", tool="alpha")
        self.assertEqual(list(self.state.pins()["gh"]), ["beta"])

    def test_pins_survive_state_db_deletion(self):
        self._record("pin_accept", server="gh", tool="alpha",
                     description_hash="sha256:d1", schema_hash="sha256:s1", params=["owner"])
        before = self.state.pins()
        self.state.db.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                p.unlink()
        rebuilt = State(self.db_path)
        rebuilt.rebuild(self.ledger)
        self.assertEqual(rebuilt.pins(), before)
        rebuilt.close()


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

    def test_search_low_cosine_vector_is_not_a_hit(self):
        self.state.index_page("wiki/a.md", "A", "unrelated text", 1.0, vector=[1.0, 0.0], model="m")
        hits = self.state.search("nomatch", query_vector=[0.0, 1.0], model="m")  # cosine 0.0, below the floor
        self.assertEqual(hits, [])

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

    def test_reindex_restores_applies(self):
        self._record("session_start", session="s1")
        self._record("user", session="s1", turn="t1", origin="user", trust="user", text="hi")
        self._record("memory_apply", origin="memory", target="wiki/a.md", op="add", old=None, new="x")

        rebuilt = self._reopen()
        applies = rebuilt.memory_applies_after(0)
        self.assertEqual(len(applies), 1)
        self.assertEqual(applies[0]["target"], "wiki/a.md")
        rebuilt.close()


class CloneIndexTest(unittest.TestCase):
    """v2 partition 1: the result store, clone rows, gates and context rows, all derived."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def _record(self, type, **kw):
        event = self.ledger.append(type, clone=kw.pop("clone", "root"), **kw)
        self.state.apply(event)
        return event

    def _gate(self, call_id, turn):
        return self._record("gate", session="s1", turn=turn, origin="user", step=1, call_id=call_id,
                            tool="write_file", args={"path": "/tmp/x", "content": call_id}, effect="write", brief="b")

    def _day(self):
        """Two turns of the root in s1 (a read stored as r:N, two gates handed off, one reported
        approved with its result, one denied), a fold, and a stray turn in another session."""
        s = dict(session="s1", origin="user")
        self._record("spawn", parent="", preset="root", brief="", model="", budget=0)
        self._record("user", trust="user", text="open the file", turn="t1", **s)
        self._record("turn_start", turn="t1", item="user", resident="now", **s)
        self._record("assistant", turn="t1", step=1, model="p:m", stop_reason="tool_use", usage={}, text="",
                     final=False, blocks=[{"type": "tool_use", "id": "c1", "name": "read_file", "input": {}}], **s)
        read = self._record("result", turn="t1", trust="third_party", step=1, call_id="c1", tool="read_file",
                            ok=True, body={"results": [{"title": "a"}, {"title": "b"}]}, **s)
        first, second = self._gate("c2", "t1"), self._gate("c3", "t1")
        self._record("assistant", turn="t1", step=2, model="p:m", stop_reason="end_turn", usage={}, text="asked",
                     final=True, blocks=[{"type": "text", "text": "asked"}], **s)
        self._record("turn_end", turn="t1", kind="answer", steps=2, usage={}, **s)
        self._record("resume", turn="t1", trust="user", gate=first["seq"], decision="approved", by="socket", **s)
        ran = self._record("result", turn="t1", trust="third_party", gate=first["seq"], call_id="c2",
                           tool="write_file", ok=True, body="wrote 2 bytes", **s)
        self._record("resume", turn="t1", trust="user", gate=second["seq"], decision="denied", by="chat", **s)
        self._record("user", session="s1", turn="t2", gate=[first["seq"], second["seq"]], text="report")
        self._record("turn_start", session="s1", turn="t2", item="continue", resident="now")
        self._record("summary", session="s1", first=1, upto=read["seq"], text="old", handles=[], model="p:m", usage={})
        fold = self._record("summary", session="s1", first=1, upto=ran["seq"], text="new",
                            handles=[f"r:{read['seq']}"], model="p:m", usage={})
        self._record("interrupted", session="s1", turn="t2", step=0, why="restart")
        self._record("turn_end", session="s1", turn="t2", kind="interrupted", steps=0, usage={})
        self._record("user", trust="user", text="elsewhere", turn="t3", session="s2", origin="user")
        open_gate = self._gate("c4", "t3")
        proposal = self.ledger.append("approval_pending", session="s1", origin="memory", id="m1", call_id="m1",
                                      tool="edit_memory", args={"op": "add"}, messages=[])
        self.state.apply(proposal)
        return dict(read=read, first=first, second=second, ran=ran, fold=fold, open=open_gate, proposal=proposal)

    def _readers(self, seqs):
        st = self.state
        return {"results": [st.result(seqs[k]["seq"]) for k in ("read", "ran")],
                "gates": [st.gate(seqs[k]["seq"]) for k in ("first", "second", "open")],
                "open": st.open_gates(), "clone": st.clone_row("root"),
                "context": st.context("root", "s1", 0), "fold": st.last_fold("root", "s1"),
                "items": [st.approval_item(seqs[k]["seq"]) for k in ("first", "proposal")]}

    def test_projection(self):
        seqs = self._day()
        read, ran = self.state.result(seqs["read"]["seq"]), self.state.result(seqs["ran"]["seq"])
        self.assertEqual(read["body"], {"results": [{"title": "a"}, {"title": "b"}]})
        self.assertEqual((ran["body"], ran["ok"], ran["tool"]), ("wrote 2 bytes", True, "write_file"))
        self.assertIsNone(self.state.result(seqs["first"]["seq"]))  # a gate is not a result
        first, second, open_ = (self.state.gate(seqs[k]["seq"]) for k in ("first", "second", "open"))
        self.assertEqual((first["decision"], first["reported"], first["result"]), ("approved", True, ran["seq"]))
        self.assertEqual((second["decision"], second["reported"], second["result"]), ("denied", True, None))
        self.assertEqual((open_["decision"], open_["reported"], open_["args"]["content"]), (None, False, "c4"))
        decided = self._gate("c5", "t3")  # decided, its report never ran: still listed
        for decision in ("approved", "denied"):  # the first decision wins; a second resume is ignored
            self._record("resume", session="s1", turn="t3", trust="user", gate=decided["seq"], decision=decision,
                         by="socket")
        self.assertEqual([(g["seq"], g["decision"]) for g in self.state.open_gates()],
                         [(seqs["open"]["seq"], None), (decided["seq"], "approved")])
        self.assertEqual(self.state.clone_row("root")["state"], "parked")
        context = self.state.context("root", "s1", 0)
        self.assertNotIn("spawn", {e["type"] for e in context})
        self.assertTrue(all("body" not in e for e in context))
        self.assertNotIn("elsewhere", [e.get("text") for e in context])  # another session's window
        self.assertEqual([e["seq"] for e in self.state.context("root", "s1", seqs["ran"]["seq"])][0],
                         seqs["ran"]["seq"] + 1)
        self.assertEqual(self.state.last_fold("root", "s1")["text"], "new")
        self.assertIsNone(self.state.last_fold("root", "s2"))
        self.assertEqual(self.state.approval_item(seqs["first"]["seq"])["kind"], "gate")
        proposal = self.state.approval_item(seqs["proposal"]["seq"])
        self.assertEqual((proposal["kind"], proposal["id"]), ("proposal", "m1"))
        self.assertIsNone(self.state.approval_item(seqs["read"]["seq"]))

    def test_a_gate_changes_no_clone_state(self):
        self._record("spawn", parent="", preset="root", brief="", model="", budget=0)
        self._record("turn_start", session="s1", turn="t1", item="user", resident="")
        self._gate("c1", "t1")
        self.assertEqual(self.state.clone_row("root")["state"], "running")
        self._record("turn_end", session="s1", turn="t1", kind="answer", steps=1, usage={})
        self.assertEqual(self.state.clone_row("root")["state"], "parked")

    def test_session_grants_survive_rebuild(self):
        """Q5: an approval with scope `session` is a resume field; session_grants derives the tools from the gates,
        per session; a gate's title and recipient are stored; an old ledger's gate (no new fields) still projects."""
        def gate(session, call_id, tool, **extra):
            return self._record("gate", session=session, turn="t1", origin="user", step=1, call_id=call_id, tool=tool,
                                args={}, effect="write", brief="b", **extra)

        def resume(g, decision, **extra):
            self._record("resume", session=g["session"], turn="t1", trust="user", gate=g["seq"], decision=decision,
                         by="socket", **extra)

        old = gate("s1", "c0", "write_file")
        titled = gate("s1", "c1", "mcp__fake__pages", title="page_id: Trip plan", recipient="")
        tool_wide, other, denied = gate("s1", "c2", "add_event"), gate("s2", "c3", "add_reminder"), gate(
            "s1", "c4", "complete_reminder")
        resume(old, "approved")
        resume(titled, "approved", scope="session")
        resume(tool_wide, "approved", scope="tool")
        resume(other, "approved", scope="session")
        resume(denied, "denied", scope="session")

        def check():
            self.assertEqual(self.state.session_grants("s1"), {"mcp__fake__pages"})
            self.assertEqual(self.state.session_grants("s2"), {"add_reminder"})
            self.assertEqual(self.state.session_grants("s3"), set())
            row = self.state.gate(titled["seq"])
            self.assertEqual((row["title"], row["recipient"], row["scope"]), ("page_id: Trip plan", "", "session"))
            row = self.state.gate(old["seq"])
            self.assertEqual((row["title"], row["recipient"], row["scope"]), ("", "", ""))

        check()
        self.state.close()
        for p in self.home.glob("state.db*"):
            p.unlink()
        self.state = State(self.home / "state.db")
        self.state.rebuild(self.ledger)
        check()

    def test_old_gates_table_gains_columns(self):
        """A P1 home's `gates` has no title, recipient or scope; State adds them, so its gates still project."""
        self.state.close()
        for p in self.home.glob("state.db*"):
            p.unlink()
        db = sqlite3.connect(self.home / "state.db")
        db.execute("CREATE TABLE gates (seq INTEGER PRIMARY KEY, clone TEXT, session TEXT, turn TEXT, call_id TEXT, "
                   "tool TEXT, args_json TEXT, effect TEXT, brief TEXT, decision TEXT, reported INTEGER, "
                   "result INTEGER, ts REAL)")
        db.commit()
        db.close()
        self.state = State(self.home / "state.db")
        g = self._gate("c1", "t1")
        self.assertEqual(self.state.gate(g["seq"])["title"], "")

    def test_rebuild_restores_every_reader(self):
        seqs = self._day()
        before = self._readers(seqs)
        self.state.close()
        for p in self.home.glob("state.db*"):
            p.unlink()
        self.state = State(self.home / "state.db")
        self.state.rebuild(self.ledger)
        self.assertEqual(self._readers(seqs), before)


if __name__ == "__main__":
    unittest.main()
