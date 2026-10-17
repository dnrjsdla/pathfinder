import os
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

    def test_history_is_said_and_called_only(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="signal", trust="user",
                      text="a signal envelope, not a said thing")
        self._record("assistant", session="s2", turn="t1", text="mid-turn tool chatter")  # no final
        self._record("user", session="s2", turn="t1", origin="user", trust="user",
                      text="the real thing said")
        self._record("tool_result", session="s2", turn="t1", origin="user", id="c9", name="x", ok=True,
                     result="a result, not a said thing")  # no tool_call: no tool record, never the result
        self._record("assistant", session="s2", turn="t1", final=False, withdrawn=True, text="a withdrawn draft")
        self._record("assistant", session="s2", turn="t1", final=True, text="the real reply")

        self.assertEqual(self.state.history("s2"), [
            {"role": "user", "content": "the real thing said"},
            {"role": "assistant", "content": "the real reply"},
        ])

    def test_withdrawn_draft_survives_rebuild(self):
        """Invariant 2: the withdrawn draft stays in the ledger, never in history, across a rebuild."""
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="any reminders?")
        self._record("assistant", session="s2", turn="t1", origin="user", text="none", final=False,
                     withdrawn={"flags": ["unsupported_state"], "why": "no tool result"})
        self._record("assistant", session="s2", turn="t1", origin="user", text="two", final=True)
        rebuilt = State(self.home / "rebuilt.db")
        self.addCleanup(rebuilt.close)
        rebuilt.rebuild(self.ledger)
        self.assertIn("none", [e["text"] for e in self.ledger.read() if e["type"] == "assistant" and e.get("withdrawn")])
        self.assertEqual(rebuilt.history("s2"), [{"role": "user", "content": "any reminders?"},
                                                 {"role": "assistant", "content": "two"}])

    def _call(self, id, name, args, ok=True, result="RESULT-TEXT", origin="user", **kw):
        self._record("tool_call", session="s2", turn="t1", origin=origin, id=id, name=name, args=args, **kw)
        self._record("tool_result", session="s2", turn="t1", origin=origin, id=id, name=name, ok=ok,
                     result=result, **kw)

    def test_tool_record_folds_before_the_answer(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="find it")
        self._call("c1", "search_tools", {"query": "gmail"})
        self._call("c2", "read_file", {"path": "/x"}, ok=False)
        self._record("assistant", session="s2", turn="t1", final=True, text="done")
        history = self.state.history("s2")
        self.assertEqual([m["role"] for m in history], ["user", "system", "assistant"])
        self.assertIn("ok = the tool returned, not that it did what was asked", history[1]["content"])
        self.assertTrue(history[1]["content"].endswith(
            '\nsearch_tools {"query":"gmail"} ok\nread_file {"path":"/x"} failed'))
        rebuilt = State(self.home / "rebuilt.db")
        self.addCleanup(rebuilt.close)
        rebuilt.rebuild(self.ledger)
        self.assertEqual(rebuilt.history("s2"), history)  # reindex rebuilds the rows

    def test_tool_record_holds_no_result_text(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._call("c1", "write", {"n": 1}, result="SECRET-RESULT")
        self.assertNotIn("SECRET-RESULT", "".join(m["content"] for m in self.state.history("s2")))

    def test_tool_record_clips_arguments(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._call("c1", "write", {"text": "한" * 500})
        self.assertEqual(self._lines(), [['write ' + ('{"text":"' + "한" * 500 + '"}')[:106] + "...(truncated) ok"]])

    def test_clipped_arguments_stay_within_the_clip_and_end_on_a_whole_escape(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._call("c1", "w", {"t": "\u2028" * 120})
        self._call("c2", "w", {"t": "a" * 5 + "\x01" * 40})  # every control char is a 6-character escape
        for call in self._lines()[0]:
            args = call[2:-len(" ok")]
            self.assertLessEqual(len(args), 120)
            self.assertTrue(args.endswith("...(truncated)"))
            body = args[:-len("...(truncated)")]
            self.assertRegex(body, r'^(\{"t":"([^\\]|\\u[0-9a-f]{4})*)$')  # no half escape at the cut

    def test_a_record_line_is_one_line(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._call("c1", "w", {"t": "a\u2028b\u2029c\x85d\x0be\nf"})
        (line,) = self._lines()[0]
        self.assertEqual(len(line.splitlines()), 1)
        self.assertEqual(len(self.state.history("s2")[-1]["content"].splitlines()), 3)  # two header lines + one

    def test_a_result_marks_its_own_call(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._record("tool_call", session="s2", turn="t1", origin="user", id="c1", name="a", args={})
        self._record("tool_call", session="s2", turn="t1", origin="user", id="c2", name="b", args={})
        self._record("tool_result", session="s2", turn="t1", origin="user", id="c1", name="a", ok=True, result="r")
        self.assertEqual(self._lines(), [["a {} ok", "b {}"]])

    def test_a_call_refused_at_the_door_reads_refused_or_denied(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._call("c1", "nope", {}, ok=False, refused=True)
        self._call("c2", "write_file", {}, ok=False, refused=True, denied=True)
        self.assertEqual(self._lines(), [["nope {} refused", "write_file {} denied"]])

    def test_a_signal_or_job_result_does_not_mark_the_owners_row(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._record("tool_call", session="s2", turn="t1", origin="user", id="c1", name="a", args={})
        for origin in ("signal", "job"):
            self._record("tool_result", session="s2", turn="t2", origin=origin, id="c1", name="a", ok=True, result="r")
        self.assertEqual(self._lines(), [["a {}"]])

    def _gate(self, id, name, args, **kw):
        self._record("approval_pending", session="s2", turn="t1", origin="user", id=id, call_id="m-" + id,
                     tool=name, args=args, messages=[{"role": "user", "content": "x"}], **kw)

    def _lines(self):
        return [m["content"].splitlines()[2:] for m in self.state.history("s2") if m["role"] == "system"]

    def test_tool_record_states(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._call("c1", "a", {"n": 1})
        self._call("c2", "b", {"n": 2}, ok=False)
        self._gate("p1", "c", {"n": 3})
        self._record("approval", session="s2", turn="t1", origin="user", id="p1", decision="denied")
        self._gate("p2", "d", {"n": 4})
        self.assertEqual(self._lines(), [['a {"n":1} ok', 'b {"n":2} failed', 'c {"n":3} (approval p1) denied',
                                          'd {"n":4} (approval p2) waiting for approval']])
        self._record("approval", session="s2", turn="t1", origin="user", id="p2", decision="approved")
        self.assertEqual(self._lines(), [['a {"n":1} ok', 'b {"n":2} failed',
                                          'c {"n":3} (approval p1) denied']])
        self._call("m-p2", "d", {"n": 4})  # the resumed turn's own call
        self.assertEqual(self._lines()[0][-1], 'd {"n":4} ok')
        rebuilt = State(self.home / "rebuilt.db")
        self.addCleanup(rebuilt.close)
        rebuilt.rebuild(self.ledger)
        self.assertEqual(rebuilt.history("s2"), self.state.history("s2"))

    def _decide(self, id, decision):
        self._record("approval", session="s2", turn="t1", origin="user", id=id, decision=decision)

    def _same_after_rebuild(self):
        rebuilt = State(self.home / "rebuilt.db")
        self.addCleanup(rebuilt.close)
        rebuilt.rebuild(self.ledger)
        self.assertEqual(rebuilt.history("s2"), self.state.history("s2"))

    def test_two_open_gates_each_keep_their_own_row(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._gate("p1", "send_mail", {})
        self._gate("p2", "delete_file", {})
        self._decide("p1", "denied")  # not the newest gate
        self.assertEqual(self._lines(), [["send_mail {} (approval p1) denied", "delete_file {} (approval p2) waiting for approval"]])
        self._same_after_rebuild()

    def test_approve_one_gate_then_deny_the_other(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._gate("p1", "send_mail", {})
        self._gate("p2", "delete_file", {})
        self._decide("p1", "approved")
        self._call("m-p1", "send_mail", {})
        self._decide("p2", "denied")
        self.assertEqual(self._lines(), [["delete_file {} (approval p2) denied", "send_mail {} ok"]])
        self._same_after_rebuild()

    def test_an_approval_without_a_row_changes_nothing(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._record("approval_pending", session="s2", turn="t1", origin="user", id="old", call_id="m-old",
                     tool="x", args={}, messages=[])  # before this unit: no row
        self._gate("p2", "delete_file", {})
        self._decide("old", "denied")
        self._decide("old", "approved")
        self.assertEqual(self._lines(), [["delete_file {} (approval p2) waiting for approval"]])

    def test_a_denied_proposal_does_not_mark_a_gates_row(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._gate("p1", "send_mail", {})
        self._record("approval_pending", session="s2", origin="user", id="q1", call_id="q1", tool="edit_memory",
                     args={}, messages=[])
        self._decide("q1", "denied")
        self.assertEqual(self._lines(), [["send_mail {} (approval p1) waiting for approval"]])

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
        self._record("approval_pending", session="s2", origin="job", id="job", call_id="c3", tool="x", args={},
                     messages=[{"role": "user", "content": "x"}], job="j1")
        self.assertEqual([p["id"] for p in self.state.open_approvals("s2", 7200)], ["gate", "job"])
        # no user line yet; a job's gate counts from any session (D63)
        self.assertEqual([p["id"] for p in self.state.open_approvals("s3", 7200)], ["other", "job"])
        self._record("user", session="s2", turn="t2", origin="user", trust="user", text="something else")
        self.assertEqual(self.state.open_approvals("s2", 7200), [])

    def test_presence_is_the_newest_row_across_sessions(self):
        """5a: the owner has one pair of hands - a screen moved into a new session typed in the old one."""
        self.assertIsNone(self.state.presence())
        self._record("presence", session="s1", origin="user", idle_s=0)
        second = self._record("presence", session="s2", origin="user", idle_s=50)
        self.assertEqual(self.state.presence(), {"ts": second["ts"], "idle_s": 50})
        again = self._record("presence", session="s1", origin="user", idle_s=7)
        self.assertEqual(self.state.presence(), {"ts": again["ts"], "idle_s": 7})
        self.assertFalse(self.state.today(idle_threshold_s=20).away)  # fresh and typing, whichever session

    def test_proposals_and_job_gates_are_not_calls(self):
        self._record("session_start", session="s2")
        self._record("approval_pending", session="s2", origin="user", id="q1", call_id="q1", tool="edit_memory",
                     args={}, messages=[])
        self._record("approval_pending", session="s2", origin="job", id="q2", call_id="c", tool="x", args={},
                     messages=[{"role": "user", "content": "x"}], job="j1")
        self._record("approval", session="s2", origin="user", id="q1", decision="denied")
        self.assertEqual(self.state.history("s2"), [])

    def test_assistant_description_of_a_call_is_no_record(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="go")
        self._record("assistant", session="s2", turn="t1", final=True, text='a {"n":1} ok')
        self.assertEqual([m["role"] for m in self.state.history("s2")], ["user", "assistant"])

    def test_record_skips_signal_and_job_calls(self):
        self._record("session_start", session="s2")
        self._call("c1", "a", {}, origin="signal")
        self._call("c2", "b", {}, origin="job", job="j1")
        self.assertEqual(self.state.history("s2"), [])  # a ledger with no user-origin tool_call has no record

    def test_summary_boundary_ignores_record_rows(self):
        self._record("session_start", session="s2")
        self._record("user", session="s2", turn="t1", origin="user", trust="user", text="u1")
        self._call("c1", "a", {})
        self._record("assistant", session="s2", turn="t1", final=True, text="a1")
        self._record("user", session="s2", turn="t2", origin="user", trust="user", text="u2")
        self._call("c2", "b", {})
        seqs = [m["seq"] for m in self.state.session_messages("s2", 0, 10**6)]
        self.assertEqual([m["type"] for m in self.state.session_messages("s2", 0, 10**6)],
                         ["user", "assistant", "user"])
        self.assertEqual(self.state.nth_newest_message_seq("s2", 2), seqs[1])  # not a calls row

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
        self._record("session_summary", session="s1", origin="memory", upto=1, text="summary")
        after = self.state.session("s1")["last_ts"]
        self.assertEqual(before, after)  # a pass's own writing must not look like the user is still there

    def test_job_events_do_not_keep_a_session_alive(self):
        """D63: a job's own events, and the owner acting on a job - a steer, a gate's approval -
        are not conversation; a job no longer revives a session D41 ended."""
        self._record("session_start", session="s1")
        self._record("user", session="s1", turn="t1", origin="user", trust="user", text="hi")
        before = self.state.session("s1")["last_ts"]
        self._record("job_start", session="s1", origin="job", id="j1", goal="research")
        self._record("user", session="s1", origin="user", job="j1", trust="user", text="shorter", steer=True)
        self._record("approval_pending", session="s1", origin="job", id="g1", call_id="c1", tool="write_file",
                     args={}, messages=[{"role": "user", "content": "x"}], job="j1")
        self._record("approval", session="s1", turn="", origin="user", id="g1", decision="approved", job="j1")
        self._record("job_done", session="s1", origin="job", id="j1", summary="found it", steps=2, usage={})
        self.assertEqual(self.state.session("s1")["last_ts"], before)
        said = self._record("user", session="s1", turn="t2", origin="user", trust="user", text="and?")
        self.assertEqual(self.state.session("s1")["last_ts"], said["ts"])  # the owner's own word still counts

    def test_open_approvals_includes_a_job_gate_from_another_session(self):
        """D63: a job's gate is answered by the harness from the screen's session; the asking
        session's last user message still decides whether it has been talked past."""
        self._record("session_start", session="old")
        self._record("session_start", session="new")
        self._record("user", session="new", turn="t1", origin="user", trust="user", text="hi")
        self._record("approval_pending", session="old", origin="job", id="jg", call_id="c1", tool="write_file",
                     args={}, messages=[{"role": "user", "content": "x"}], job="j1")
        self._record("approval_pending", session="old", origin="user", id="sg", call_id="c2", tool="x",
                     args={}, messages=[])  # not a job's: it stays in its own session
        self.assertEqual([p["id"] for p in self.state.open_approvals("new", 7200)], ["jg"])
        self._record("user", session="new", turn="t2", origin="user", trust="user", text="응")  # got the line
        self.assertEqual(self.state.open_approvals("new", 7200), [])  # said again: it goes to the model

    def test_a_stale_job_gate_from_another_session_is_not_open(self):
        """U32's review: a job gate left open for days would swallow the first reply of every new
        session; from another session it counts only while it is fresh (`session_resume_s`)."""
        self._record("session_start", session="A")
        self._record("session_start", session="C")
        for id, age in (("stale", 3 * 86400), ("fresh", 600)):
            self._record("approval_pending", session="A", origin="job", id=id, call_id=id, tool="write_file",
                         args={}, messages=[{"role": "user", "content": "x"}], job="j-" + id)
            self.state.db.execute("UPDATE approvals SET ts = ts - ? WHERE id = ?", (age, id))
        self.assertEqual([p["id"] for p in self.state.open_approvals("C", 7200)], ["fresh"])
        self.assertEqual([p["id"] for p in self.state.open_approvals("A", 7200)], ["stale", "fresh"])  # its own

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
            "gh": {"alpha": {"description": "sha256:d1", "schema": "sha256:s1", "params": ["owner"]}},
        })
        # a second accept for the same (server, tool) replaces, never duplicates
        self._record("pin_accept", server="gh", tool="alpha",
                     description_hash="sha256:d2", schema_hash="sha256:s1", params=["owner"])
        self.assertEqual(self.state.pins()["gh"]["alpha"]["description"], "sha256:d2")

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
