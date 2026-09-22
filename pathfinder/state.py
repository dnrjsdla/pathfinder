"""Derived state: a SQLite projection of the ledger. Always rebuildable, never
the source of truth - the daemon does ledger.append() then state.apply() so
live writes and rebuild share one code path."""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_ts REAL, last_ts REAL, turns INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, call_id TEXT, session TEXT, turn TEXT, tool TEXT,
    args_json TEXT, messages_json TEXT, ts REAL, decision TEXT
);
"""


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE in apply), so the
        # cursor check and the projection are one atomic step even with a second process.
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()  # check_same_thread=False removes the guard, not the race
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('cursor', '0')")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @property
    def cursor(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key = 'cursor'").fetchone()
        return int(row[0])

    def apply(self, event: dict) -> None:
        seq = event["seq"]
        session = event.get("session") or None
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading the cursor
            try:
                if seq <= self.cursor:
                    self.db.execute("ROLLBACK")
                    return  # idempotent: an already-applied event (rebuild, redelivery, a second process) is a no-op
                self._project(seq, session, event)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _project(self, seq, session, event):
        """Runs inside apply()'s transaction: the cursor advances with the projection or not at all."""
        if event["type"] == "session_start" and session:
            self.db.execute(
                "INSERT OR IGNORE INTO sessions (id, started_ts, last_ts, turns) VALUES (?, ?, ?, 0)",
                (session, event["ts"], event["ts"]),
            )
        # only what was said is remembered: a user event needs origin "user" (not a
        # signal envelope), an assistant event needs final=true (not tool chatter mid-turn)
        if event["type"] == "user" and event.get("origin") == "user":
            self._insert_message(seq, session, "user", event.get("text", ""), event["ts"])
        if event["type"] == "assistant" and event.get("final"):
            self._insert_message(seq, session, "assistant", event.get("text", ""), event["ts"])
        if event["type"] == "turn_end" and session:
            if event.get("kind") != "gated":  # a gate is a pause, not an exchange
                self.db.execute("UPDATE sessions SET turns = turns + 1 WHERE id = ?", (session,))
        if event["type"] == "approval_pending":
            self.db.execute(
                "INSERT OR IGNORE INTO approvals (id, call_id, session, turn, tool, args_json, messages_json, ts, decision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (event["id"], event["call_id"], session, event.get("turn", ""), event["tool"],
                 json.dumps(event["args"]), json.dumps(event["messages"]), event["ts"]),
            )
        if event["type"] == "approval":
            self.db.execute("UPDATE approvals SET decision = ? WHERE id = ?", (event["decision"], event["id"]))
        if session:
            self.db.execute("UPDATE sessions SET last_ts = ? WHERE id = ?", (event["ts"], session))
        self.db.execute("UPDATE meta SET value = ? WHERE key = 'cursor'", (str(seq),))

    def _insert_message(self, seq, session, role, content, ts):
        self.db.execute(
            "INSERT OR IGNORE INTO messages (seq, session, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            (seq, session, role, content, ts),
        )

    def catch_up(self, ledger) -> int:
        for event in ledger.read(self.cursor):
            self.apply(event)
        return self.cursor

    def rebuild(self, ledger) -> int:
        self.db.executescript(
            "DROP TABLE sessions; DROP TABLE messages; DROP TABLE meta; DROP TABLE approvals;"
        )
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('cursor', '0')")
        self.db.commit()
        return self.catch_up(ledger)

    def history(self, session: str) -> list:
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE session = ? ORDER BY seq", (session,)
        ).fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    def session(self, sid: str):
        row = self.db.execute(
            "SELECT id, started_ts, last_ts, turns FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "started_ts": row[1], "last_ts": row[2], "turns": row[3]}

    def pending(self) -> list:
        """Approvals with no decision yet, oldest first - what survives a restart."""
        rows = self.db.execute(
            "SELECT id, session, turn, tool, args_json, ts FROM approvals WHERE decision IS NULL ORDER BY ts"
        ).fetchall()
        return [{"id": r[0], "session": r[1], "turn": r[2], "tool": r[3], "args": json.loads(r[4]), "ts": r[5]}
                for r in rows]

    def approval(self, id: str):
        row = self.db.execute(
            "SELECT id, session, turn, tool, args_json, messages_json, ts, decision, call_id FROM approvals WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "session": row[1], "turn": row[2], "tool": row[3],
                "args": json.loads(row[4]), "messages": json.loads(row[5]), "ts": row[6], "decision": row[7],
                "call_id": row[8]}

    def latest_session(self, max_age_s: float, now=None):
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT id FROM sessions WHERE ? - last_ts <= ? ORDER BY last_ts DESC LIMIT 1",
            (now, max_age_s),
        ).fetchone()
        return row[0] if row else None
