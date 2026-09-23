"""Derived state: a SQLite projection of the ledger. Always rebuildable, never
the source of truth - the daemon does ledger.append() then state.apply() so
live writes and rebuild share one code path."""
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_ts REAL, last_ts REAL, turns INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, call_id TEXT, session TEXT, turn TEXT, tool TEXT,
    args_json TEXT, messages_json TEXT, ts REAL, decision TEXT, job TEXT
);
CREATE TABLE IF NOT EXISTS signals (key TEXT PRIMARY KEY, ts REAL, sense TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    signal_key TEXT, ts REAL, action TEXT, why TEXT,
    notice_id TEXT, session TEXT, text TEXT, signal_keys TEXT
);
CREATE TABLE IF NOT EXISTS judgments (ts REAL);
CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, ts REAL, session TEXT, via TEXT);
CREATE TABLE IF NOT EXISTS reactions (ts REAL, delivery_id TEXT, vote TEXT);
CREATE TABLE IF NOT EXISTS presence (session TEXT PRIMARY KEY, ts REAL, idle_s REAL);
CREATE TABLE IF NOT EXISTS signal_turns (ts REAL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, session TEXT, goal TEXT, started_ts REAL, ended_ts REAL,
    state TEXT, summary TEXT, reason TEXT, steps INTEGER, usage_json TEXT
);
"""

NEW_TABLES = ("signals", "decisions", "judgments", "deliveries", "reactions", "presence", "signal_turns", "jobs")


@dataclass
class Today:
    """The counters `rules.decide` needs, all local-day scoped (`clock.day_start`)."""
    spoken: int               # decisions {action: speak} today - the daily budget
    judgments: int            # judgment events today - the judgment_cap
    last_signal_ts: float | None  # ts of the last signal turn opened (rate limit)
    burst: int                # signal turns opened in the last 60 s
    recent_counts: dict       # kind -> signals of that kind in the last 600 s, incl. this one (count_over)
    away: bool = True         # target session's presence is stale/idle (claude_code's "away")
    jobs: int = 0             # job_start events today - jobs.py's own daily budget


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE in apply), so the
        # cursor check and the projection are one atomic step even with a second process.
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()  # check_same_thread=False removes the guard, not the race
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS leaves an older table alone: a column added after a partition
        # shipped (partition 4: approvals.job) is added here, so an existing home keeps starting.
        if "job" not in {r[1] for r in self.db.execute("PRAGMA table_info(approvals)")}:
            self.db.execute("ALTER TABLE approvals ADD COLUMN job TEXT")
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
        if event["type"] == "user" and event.get("origin") == "user" and not event.get("job"):  # a job steer is not chat
            self._insert_message(seq, session, "user", event.get("text", ""), event["ts"])
        if event["type"] == "assistant" and event.get("final"):
            self._insert_message(seq, session, "assistant", event.get("text", ""), event["ts"])
        if event["type"] == "turn_end" and session:
            if event.get("kind") != "gated":  # a gate is a pause, not an exchange
                self.db.execute("UPDATE sessions SET turns = turns + 1 WHERE id = ?", (session,))
        if event["type"] == "approval_pending":
            self.db.execute(
                "INSERT OR IGNORE INTO approvals (id, call_id, session, turn, tool, args_json, messages_json, ts, decision, job) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (event["id"], event["call_id"], session, event.get("turn", ""), event["tool"],
                 json.dumps(event["args"]), json.dumps(event["messages"]), event["ts"], event.get("job")),
            )
            if event.get("job"):  # `pathfinder jobs` shows a paused job as gated, not running
                self.db.execute("UPDATE jobs SET state = 'gated' WHERE id = ?", (event["job"],))
        if event["type"] == "approval":
            self.db.execute("UPDATE approvals SET decision = ? WHERE id = ?", (event["decision"], event["id"]))
            self.db.execute("UPDATE jobs SET state = 'running' WHERE state = 'gated' AND id = "
                            "(SELECT job FROM approvals WHERE id = ?)", (event["id"],))
        if event["type"] == "signal":
            self.db.execute("INSERT OR IGNORE INTO signals (key, ts, sense, kind) VALUES (?, ?, ?, ?)",
                            (event["key"], event["ts"], event["sense"], event["kind"]))
        if event["type"] == "decision":
            keys = event.get("signal_keys")
            self.db.execute(
                "INSERT INTO decisions (signal_key, ts, action, why, notice_id, session, text, signal_keys) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event.get("signal_key"), event["ts"], event["action"], event.get("why", ""),
                 event.get("notice_id"), session, event.get("text"), json.dumps(keys) if keys else None))
        if event["type"] == "judgment":
            self.db.execute("INSERT INTO judgments (ts) VALUES (?)", (event["ts"],))
        if event["type"] == "delivery":
            self.db.execute("INSERT OR IGNORE INTO deliveries (id, ts, session, via) VALUES (?, ?, ?, ?)",
                            (event["id"], event["ts"], session, event["via"]))
        if event["type"] == "reaction":
            self.db.execute("INSERT INTO reactions (ts, delivery_id, vote) VALUES (?, ?, ?)",
                            (event["ts"], event.get("delivery_id"), event["vote"]))
        if event["type"] == "presence" and session:
            self.db.execute(
                "INSERT INTO presence (session, ts, idle_s) VALUES (?, ?, ?) "
                "ON CONFLICT(session) DO UPDATE SET ts = excluded.ts, idle_s = excluded.idle_s",
                (session, event["ts"], event["idle_s"]))
        if event["type"] == "turn_start" and event.get("origin") == "signal":
            self.db.execute("INSERT INTO signal_turns (ts) VALUES (?)", (event["ts"],))
        if event["type"] == "job_start":
            self.db.execute(
                "INSERT OR IGNORE INTO jobs (id, session, goal, started_ts, state) VALUES (?, ?, ?, ?, 'running')",
                (event["id"], session, event["goal"], event["ts"]))
        if event["type"] == "job_done":
            usage = event.get("usage")
            self.db.execute(
                "UPDATE jobs SET ended_ts = ?, state = 'done', summary = ?, steps = ?, usage_json = ? WHERE id = ?",
                (event["ts"], event.get("summary", ""), event.get("steps"),
                 json.dumps(usage) if usage is not None else None, event["id"]))
        if event["type"] == "job_failed":
            # "stopped" is the one reason the user themselves caused (interrupt) - the CLI shows
            # it as its own state rather than lumping a cancel in with a model/internal failure.
            state_val = "cancelled" if event.get("reason") == "stopped" else "failed"
            self.db.execute(
                "UPDATE jobs SET ended_ts = ?, state = ?, reason = ? WHERE id = ?",
                (event["ts"], state_val, event.get("reason", ""), event["id"]))
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
        drops = "sessions", "messages", "meta", "approvals", *NEW_TABLES
        self.db.executescript("".join(f"DROP TABLE {t};" for t in drops))
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
            "SELECT id, session, turn, tool, args_json, ts, job FROM approvals WHERE decision IS NULL ORDER BY ts"
        ).fetchall()
        return [{"id": r[0], "session": r[1], "turn": r[2], "tool": r[3], "args": json.loads(r[4]), "ts": r[5],
                "job": r[6]} for r in rows]

    def approval(self, id: str):
        row = self.db.execute(
            "SELECT id, session, turn, tool, args_json, messages_json, ts, decision, call_id, job FROM approvals WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "session": row[1], "turn": row[2], "tool": row[3],
                "args": json.loads(row[4]), "messages": json.loads(row[5]), "ts": row[6], "decision": row[7],
                "call_id": row[8], "job": row[9]}

    def latest_session(self, max_age_s: float, now=None):
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT id FROM sessions WHERE ? - last_ts <= ? ORDER BY last_ts DESC LIMIT 1",
            (now, max_age_s),
        ).fetchone()
        return row[0] if row else None

    def presence(self, session: str):
        row = self.db.execute("SELECT ts, idle_s FROM presence WHERE session = ?", (session,)).fetchone()
        return None if row is None else {"ts": row[0], "idle_s": row[1]}

    def latest_delivery(self):
        row = self.db.execute("SELECT id FROM deliveries ORDER BY ts DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def has_delivery(self, id: str) -> bool:
        return self.db.execute("SELECT 1 FROM deliveries WHERE id = ?", (id,)).fetchone() is not None

    def seen(self, key: str, since: float) -> bool:
        """True when `key` was recorded (any sense, any kind) at or after `since` - the 24 h
        dedupe window is the caller's `since = now - 86400`."""
        row = self.db.execute("SELECT 1 FROM signals WHERE key = ? AND ts >= ?", (key, since)).fetchone()
        return row is not None

    def today(self, now: float | None = None, session: str | None = None,
             idle_threshold_s: float | None = None) -> Today:
        """`session`/`idle_threshold_s` (only the daemon's signal-turn triage passes them) fill
        in `away` - a predicate (claude_code's) only ever sees `(signal, value, today, now)`,
        never `rules` or `state` itself, so the threshold has to be applied here, not there."""
        now = now if now is not None else time.time()
        start = clock.day_start(now)
        spoken = self.db.execute(
            "SELECT COUNT(*) FROM decisions WHERE action = 'speak' AND ts >= ?", (start,)).fetchone()[0]
        judgments = self.db.execute("SELECT COUNT(*) FROM judgments WHERE ts >= ?", (start,)).fetchone()[0]
        last_signal_ts = self.db.execute("SELECT MAX(ts) FROM signal_turns").fetchone()[0]
        burst = self.db.execute(
            "SELECT COUNT(*) FROM signal_turns WHERE ts >= ?", (now - 60,)).fetchone()[0]
        # count_over's window (should-fix 5): signals of a kind in the last 10 minutes, not a
        # whole-day count - read from `signals`, not `decisions`, because a signal is recorded
        # before its own decision is, so "including this one" falls out for free.
        recent_counts = dict(self.db.execute(
            "SELECT kind, COUNT(*) FROM signals WHERE ts >= ? GROUP BY kind", (now - 600,)).fetchall())
        jobs_today = self.db.execute("SELECT COUNT(*) FROM jobs WHERE started_ts >= ?", (start,)).fetchone()[0]
        away = True
        if session is not None and idle_threshold_s is not None:
            row = self.presence(session)
            fresh = row is not None and now - row["ts"] < 60
            away = not fresh or row["idle_s"] >= idle_threshold_s
        return Today(spoken, judgments, last_signal_ts, burst, recent_counts, away, jobs_today)

    def undelivered(self) -> list:
        """Notices from a `decision speak` never confirmed by a `delivery` - what a daemon
        killed mid-outbox must still hand over on the next start."""
        rows = self.db.execute(
            "SELECT DISTINCT d.notice_id, d.session, d.text, d.why, d.signal_keys FROM decisions d "
            "LEFT JOIN deliveries dl ON dl.id = d.notice_id "
            "WHERE d.action = 'speak' AND d.notice_id IS NOT NULL AND dl.id IS NULL"
        ).fetchall()
        return [{"id": r[0], "session": r[1], "text": r[2], "why": r[3],
                "signal_keys": json.loads(r[4]) if r[4] else []} for r in rows]

    def _job_row(self, r):
        return {"id": r[0], "session": r[1], "goal": r[2], "started_ts": r[3], "ended_ts": r[4],
                "state": r[5], "summary": r[6], "reason": r[7], "steps": r[8],
                "usage": json.loads(r[9]) if r[9] else None}

    _JOB_COLUMNS = "id, session, goal, started_ts, ended_ts, state, summary, reason, steps, usage_json"

    def jobs(self) -> list:
        """Every job, newest first - `pathfinder jobs`."""
        rows = self.db.execute(f"SELECT {self._JOB_COLUMNS} FROM jobs ORDER BY started_ts DESC").fetchall()
        return [self._job_row(r) for r in rows]

    def job(self, id: str):
        row = self.db.execute(f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?", (id,)).fetchone()
        return None if row is None else self._job_row(row)

    def running_jobs(self) -> list:
        """Jobs with no end event - what a fresh `Manager` sweeps at daemon start. A job paused
        at a gate is 'gated'; the sweep tells the two apart by its pending approval row."""
        rows = self.db.execute(
            f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE state IN ('running', 'gated') ORDER BY started_ts").fetchall()
        return [self._job_row(r) for r in rows]
