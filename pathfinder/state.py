"""Derived state: a SQLite projection of the ledger. Always rebuildable, never
the source of truth - the daemon does ledger.append() then state.apply() so
live writes and rebuild share one code path."""
import json
import math
import re
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import clock

# m4: off-topic queries' cosine topped out at 0.32-0.37 on the transplanted corpus; live, a
# one-page wiki answered every query with that page and the model kept searching anyway.
VECTOR_FLOOR = 0.45
WAITING = " waiting for approval"
RECORD_HEADER = ("Tools you called here (from the ledger; names and arguments, not results).\n"
                 "ok = the tool returned, not that it did what was asked:")
_BREAKS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")  # everything a line split can happen on
CALL_ARGS_CLIP = 120  # D52: characters of a call's compact-JSON arguments kept in history
_TOKEN = re.compile(r"\\u[0-9a-f]{4}|\\.|.", re.S)  # one escape sequence or one character


def _one_line_args(args) -> str:
    """The arguments as one line of at most CALL_ARGS_CLIP characters, marked when cut, and never
    cut inside an escape sequence: escape first, then clip."""
    text = _BREAKS.sub(lambda m: "\\u%04x" % ord(m[0]),
                       json.dumps(args, separators=(",", ":"), ensure_ascii=False))
    if len(text) <= CALL_ARGS_CLIP:
        return text
    out, room = "", CALL_ARGS_CLIP - len("...(truncated)")
    for token in _TOKEN.findall(text):
        if len(out) + len(token) > room:
            break
        out += token
    return out + "...(truncated)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_ts REAL, last_ts REAL, turns INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT, ts REAL, ref TEXT);
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
CREATE TABLE IF NOT EXISTS pages (rowid INTEGER PRIMARY KEY, path TEXT UNIQUE, title TEXT, mtime REAL, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(title, text, content='pages', content_rowid='rowid');
CREATE TABLE IF NOT EXISTS vectors (path TEXT PRIMARY KEY, model TEXT, vec BLOB);
CREATE TABLE IF NOT EXISTS summaries (session TEXT PRIMARY KEY, upto INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS memory_passes (ts REAL, since INTEGER, upto INTEGER, read INTEGER, applied INTEGER, proposed INTEGER);
CREATE TABLE IF NOT EXISTS memory_applies (
    seq INTEGER PRIMARY KEY, ts REAL, target TEXT, op TEXT, old TEXT, new TEXT, approval TEXT
);
CREATE TABLE IF NOT EXISTS pins (
    server TEXT, tool TEXT, description_hash TEXT, schema_hash TEXT, params_json TEXT, ts REAL,
    PRIMARY KEY (server, tool)
);
CREATE TABLE IF NOT EXISTS installs (
    id TEXT PRIMARY KEY, name TEXT, version TEXT, state TEXT, ts REAL, detail TEXT
);
"""

NEW_TABLES = ("signals", "decisions", "judgments", "deliveries", "reactions", "presence", "signal_turns", "jobs",
              "pages_fts", "pages", "vectors", "summaries", "memory_passes", "memory_applies", "pins", "installs")


@dataclass
class Today:
    """The counters `rules.decide` needs, all local-day scoped (`clock.day_start`)."""
    spoken: int               # decisions {action: speak} today - the daily budget
    judgments: int            # judgment events today - the judgment_cap
    last_signal_ts: float | None  # ts of the last signal turn opened (rate limit)
    burst: int                # signal turns opened in the last 60 s
    recent_counts: dict       # kind -> signals of that kind in the last 600 s, incl. this one (count_over)
    away: bool = True         # the newest presence is stale/idle (claude_code's "away")
    jobs: int = 0             # job_start events today - jobs.py's own daily budget


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.touch(mode=0o600, exist_ok=True)  # born 0600: SQLite gives -wal and -shm the db's mode
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
        if "ref" not in {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}:
            self.db.execute("ALTER TABLE messages ADD COLUMN ref TEXT")  # D52: a record row's call id
        self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('cursor', '0')")
        self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('memory_cursor', '0')")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @property
    def cursor(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key = 'cursor'").fetchone()
        return int(row[0])

    @property
    def memory_cursor(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key = 'memory_cursor'").fetchone()
        return int(row[0]) if row else 0

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
        # history is what was said and what was called: a user event needs origin "user" (not a
        # signal envelope), an assistant event needs final=true (not tool chatter mid-turn);
        # calls come only from the ledger's own tool_call/tool_result/approval_pending/approval
        if event["type"] == "user" and event.get("origin") == "user" and not event.get("job"):  # a job steer is not chat
            self._insert_message(seq, session, "user", event.get("text", ""), event["ts"],
                                 "chat_approval" if event.get("harness") else None)  # answered by the harness
        if event["type"] == "assistant" and event.get("final"):
            self._insert_message(seq, session, "assistant", event.get("text", ""), event["ts"])
        # D52: what was called, from the ledger's own events - name and clipped args, never a result
        gate = event["type"] == "approval_pending" and event.get("origin") == "user" \
            and event.get("messages")  # a broker gate: a proposal has no messages, a job's gate has origin "job"
        if event["type"] == "tool_call" and event.get("origin") == "user" or gate:
            name = event["name"] if event["type"] == "tool_call" else event["tool"]
            line = _BREAKS.sub(lambda m: "\\u%04x" % ord(m[0]), name) + " " + _one_line_args(event.get("args"))
            ref = event["id"] if event["type"] == "tool_call" else "approval:" + event["id"]
            self.db.execute(
                "INSERT OR IGNORE INTO messages (seq, session, role, content, ts, ref) VALUES (?, ?, 'calls', ?, ?, ?)",
                (seq, session, line + (f" (approval {event['id']})" + WAITING if gate else ""), event["ts"], ref))
        if event["type"] == "approval":  # only the row of this approval's own gate; none (an older gate) -> nothing
            if event["decision"] == "approved":  # the resumed turn's tool_call writes the real row
                self.db.execute("DELETE FROM messages WHERE ref = ?", ("approval:" + event["id"],))
            else:
                self.db.execute(f"UPDATE messages SET content = substr(content, 1, length(content) - {len(WAITING)}) "
                                f"|| ' denied' WHERE ref = ? AND substr(content, -{len(WAITING)}) = ?",
                                ("approval:" + event["id"], WAITING))
        if event["type"] == "tool_result" and event.get("origin") == "user":
            self.db.execute(  # the call's own row, by the id both events carry
                "UPDATE messages SET content = content || ? WHERE seq = "
                "(SELECT MAX(seq) FROM messages WHERE session = ? AND role = 'calls' AND ref = ?)",
                (" ok" if event.get("ok") else " failed", session, event["id"]))
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
        if event["type"] == "session_summary":
            self.db.execute(
                "INSERT INTO summaries (session, upto, text) VALUES (?, ?, ?) "
                "ON CONFLICT(session) DO UPDATE SET upto = excluded.upto, text = excluded.text",
                (session, event["upto"], event["text"]))
        if event["type"] == "memory_pass":
            self.db.execute(
                "INSERT INTO memory_passes (ts, since, upto, read, applied, proposed) VALUES (?, ?, ?, ?, ?, ?)",
                (event["ts"], event.get("since", 0), event.get("upto", 0), event.get("read", 0),
                 event.get("applied", 0), event.get("proposed", 0)))
            self.db.execute("UPDATE meta SET value = ? WHERE key = 'memory_cursor'",
                            (str(event.get("upto", 0)),))
        if event["type"] == "memory_apply":
            self.db.execute(
                "INSERT INTO memory_applies (seq, ts, target, op, old, new, approval) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seq, event["ts"], event["target"], event["op"], event.get("old"), event.get("new"),
                 event.get("approval")))
        if event["type"] == "pin_accept":
            self.db.execute(
                "INSERT OR REPLACE INTO pins (server, tool, description_hash, schema_hash, params_json, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event["server"], event["tool"], event["description_hash"], event["schema_hash"],
                 json.dumps(event["params"]), event["ts"]))
        if event["type"] == "pin_revoke":
            if event.get("tool"):
                self.db.execute("DELETE FROM pins WHERE server = ? AND tool = ?", (event["server"], event["tool"]))
            else:  # tool omitted - the whole server is forgotten
                self.db.execute("DELETE FROM pins WHERE server = ?", (event["server"],))
        if event["type"] == "install_step":
            self.db.execute(
                "INSERT INTO installs (id, name, version, state, ts, detail) VALUES (?, '', '', 'installing', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = 'installing', ts = excluded.ts, detail = excluded.detail",
                (event["id"], event["ts"], event.get("step", "")))
        if event["type"] == "install_done":
            self.db.execute(
                "INSERT INTO installs (id, name, version, state, ts, detail) VALUES (?, ?, ?, 'installed', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, version = excluded.version, "
                "state = 'installed', ts = excluded.ts, detail = excluded.detail",
                (event["id"], event["name"], event["version"], event["ts"], event.get("backup", "")))
        if event["type"] == "install_failed" and event.get("id"):
            # id == "" is a check that failed before any server id was even computed (propose
            # time) - nothing to project into a row for that.
            self.db.execute(
                "INSERT INTO installs (id, name, version, state, ts, detail) VALUES (?, ?, '', 'failed', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = 'failed', ts = excluded.ts, detail = excluded.detail",
                (event["id"], event.get("name", ""), event["ts"], event.get("reason", "")))
        if event["type"] == "uninstalled":
            self.db.execute("UPDATE installs SET state = 'uninstalled', ts = ? WHERE id = ?",
                            (event["ts"], event["id"]))
        if event["type"] == "job_failed":
            # "stopped" is the one reason the user themselves caused (interrupt) - the CLI shows
            # it as its own state rather than lumping a cancel in with a model/internal failure.
            state_val = "cancelled" if event.get("reason") == "stopped" else "failed"
            self.db.execute(
                "UPDATE jobs SET ended_ts = ?, state = ?, reason = ? WHERE id = ?",
                (event["ts"], state_val, event.get("reason", ""), event["id"]))
        # a pass's own events (origin "memory") must not look like the user is still there -
        # they would keep the session alive forever and the pass would never see it go idle.
        # Nor may a screen's 30 s `presence` heartbeat or a signal's silent triage (D41: live, one
        # session ran four days) - of a signal turn, only the notice it delivered is conversation.
        # Nor a job's own events: acting on a job - a steer, an approval - is not conversation (D63).
        origin = event.get("origin")
        if (session and origin not in ("memory", "job") and not event.get("job") and event["type"] != "presence"
                and (origin != "signal" or event["type"] == "assistant")):
            self.db.execute("UPDATE sessions SET last_ts = ? WHERE id = ?", (event["ts"], session))
        self.db.execute("UPDATE meta SET value = ? WHERE key = 'cursor'", (str(seq),))

    def _insert_message(self, seq, session, role, content, ts, ref=None):
        self.db.execute(
            "INSERT OR IGNORE INTO messages (seq, session, role, content, ts, ref) VALUES (?, ?, ?, ?, ?, ?)",
            (seq, session, role, content, ts, ref),
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
        self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('memory_cursor', '0')")
        self.db.commit()
        return self.catch_up(ledger)

    def history(self, session: str) -> list:
        summary = self.db.execute("SELECT upto, text FROM summaries WHERE session = ?", (session,)).fetchone()
        upto = summary[0] if summary is not None else 0
        out = [{"role": "system", "content": "Earlier in this conversation, summarized: " + summary[1]}] if summary else []
        calls = []  # consecutive `calls` rows fold into one system message before the turn's answer
        for role, content in self.db.execute(
                "SELECT role, content FROM messages WHERE session = ? AND seq > ? ORDER BY seq", (session, upto)):
            if role == "calls":
                calls.append(content)
                continue
            if calls:
                out.append(self._calls_message(calls))
                calls = []
            out.append({"role": role, "content": content})
        if calls:
            out.append(self._calls_message(calls))
        return out

    @staticmethod
    def _calls_message(calls: list) -> dict:
        return {"role": "system", "content": RECORD_HEADER + "\n" + "\n".join(calls)}

    def session_messages(self, session: str, after: int, upto: int) -> list:
        """`session`'s own messages with seq in `(after, upto]`, as render_line-compatible event
        dicts - every stored row already passed `_project`'s own filters (a real user turn, a
        final assistant reply), so "user"/"assistant" and `final=True` are safe to fill back in."""
        rows = self.db.execute(
            "SELECT seq, role, content FROM messages WHERE session = ? AND seq > ? AND seq <= ? "
            "AND role IN ('user', 'assistant') ORDER BY seq",
            (session, after, upto)).fetchall()
        out = []
        for seq, role, content in rows:
            if role == "user":
                out.append({"seq": seq, "type": "user", "origin": "user", "text": content})
            else:
                out.append({"seq": seq, "type": "assistant", "origin": "user", "final": True, "text": content})
        return out

    def last_user_ts(self) -> float | None:
        """The ts of the last `user`-role message - the trigger's 'hands idle' clock."""
        row = self.db.execute("SELECT MAX(ts) FROM messages WHERE role = 'user'").fetchone()
        return row[0] if row and row[0] is not None else None

    def last_user_seq(self, session: str) -> int | None:
        """Max seq of `session`'s own user messages - a user turn's `edit_memory` has no seq of
        its own to cite, so this is the evidence the gate fills in for it."""
        row = self.db.execute(
            "SELECT MAX(seq) FROM messages WHERE session = ? AND role = 'user'", (session,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def nth_newest_message_seq(self, session: str, n: int) -> int | None:
        """The seq of the session's n-th newest message, or None when fewer than n exist - the
        boundary a pass's session summary keeps verbatim (the last n-1 messages stay out of it)."""
        row = self.db.execute(
            "SELECT seq FROM messages WHERE session = ? AND role IN ('user', 'assistant') "
            "ORDER BY seq DESC LIMIT 1 OFFSET ?",
            (session, n - 1)).fetchone()
        return row[0] if row else None

    def signal_kind(self, signal_key: str) -> str | None:
        row = self.db.execute("SELECT kind FROM signals WHERE key = ?", (signal_key,)).fetchone()
        return row[0] if row else None

    def reaction_kinds(self, delivery_id: str) -> str:
        """The kinds behind one delivery's `decision speak` rows, joined - what a `[reaction]`
        ledger line says it was a reaction to. 'unknown' when nothing is found."""
        rows = self.db.execute(
            "SELECT signal_keys FROM decisions WHERE notice_id = ? AND signal_keys IS NOT NULL",
            (delivery_id,)).fetchall()
        kinds = []
        for (keys_json,) in rows:
            for key in json.loads(keys_json):
                kind = self.signal_kind(key)
                if kind and kind not in kinds:
                    kinds.append(kind)
        return ", ".join(kinds) if kinds else "unknown"

    def summary(self, session: str):
        row = self.db.execute("SELECT upto, text FROM summaries WHERE session = ?", (session,)).fetchone()
        return None if row is None else {"upto": row[0], "text": row[1]}

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

    def open_approvals(self, session: str, max_age_s: float) -> list:
        """D52: `session`'s pending approvals newer than its last user message - a gate the owner
        has talked past is not open. A job's gate counts from any session (D63) while it is younger
        than `max_age_s` (`session_resume_s`): a forgotten one must not swallow a new session's reply."""
        last = self.db.execute("SELECT MAX(ts) FROM messages WHERE session = ? AND role = 'user' "
                               "AND (ref IS NULL OR ref != 'chat_approval')", (session,)).fetchone()[0]
        return [p for p in self.pending()
                if (p["session"] == session or p["job"] and time.time() - p["ts"] <= max_age_s)
                and (last is None or p["ts"] > last)]

    def pending_memory(self) -> list:
        """The pending approvals whose tool is `edit_memory` - `pathfinder approvals`'s memory view."""
        return [p for p in self.pending() if p["tool"] == "edit_memory"]

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

    def presence(self):
        """The newest row across sessions: the owner has one pair of hands (5a)."""
        row = self.db.execute("SELECT ts, idle_s FROM presence ORDER BY ts DESC LIMIT 1").fetchone()
        return None if row is None else {"ts": row[0], "idle_s": row[1]}

    def latest_delivery(self):
        row = self.db.execute("SELECT id FROM deliveries ORDER BY ts DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def has_delivery(self, id: str) -> bool:
        return self.db.execute("SELECT 1 FROM deliveries WHERE id = ?", (id,)).fetchone() is not None

    def context_cwds(self, since: float) -> set:
        """cwd of every `context` signal (the attach sense's `attach:<cwd>` key) at or after
        `since` - the catalog's work-context tags read this, not `signals` directly."""
        rows = self.db.execute("SELECT key FROM signals WHERE kind = 'context' AND ts >= ?", (since,)).fetchall()
        return {k[len("attach:"):] for (k,) in rows if k.startswith("attach:")}

    def seen(self, key: str, since: float) -> bool:
        """True when `key` was recorded (any sense, any kind) at or after `since` - the 24 h
        dedupe window is the caller's `since = now - 86400`."""
        row = self.db.execute("SELECT 1 FROM signals WHERE key = ? AND ts >= ?", (key, since)).fetchone()
        return row is not None

    def today(self, now: float | None = None, idle_threshold_s: float | None = None) -> Today:
        """`idle_threshold_s` (only the daemon's signal-turn triage passes it) fills
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
        if idle_threshold_s is not None:
            row = self.presence()
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

    def index_page(self, path: str, title: str, text: str, mtime: float, vector=None, model: str = "") -> None:
        """Upsert a page (delete + insert) and keep the external-content FTS table in sync: the
        'delete' command needs the OLD rowid and content, so it runs before the row is replaced.
        One explicit transaction, like `apply()`: the pass thread and a user turn's own thread
        can both call this on the same connection."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.db.execute("SELECT rowid, title, text FROM pages WHERE path = ?", (path,)).fetchone()
                if old is not None:
                    self.db.execute(
                        "INSERT INTO pages_fts (pages_fts, rowid, title, text) VALUES ('delete', ?, ?, ?)", old)
                    self.db.execute("DELETE FROM pages WHERE path = ?", (path,))
                cur = self.db.execute("INSERT INTO pages (path, title, mtime, text) VALUES (?, ?, ?, ?)",
                                      (path, title, mtime, text))
                self.db.execute("INSERT INTO pages_fts (rowid, title, text) VALUES (?, ?, ?)",
                                (cur.lastrowid, title, text))
                if vector is not None:
                    blob = struct.pack(f"<{len(vector)}f", *vector)
                    self.db.execute(
                        "INSERT INTO vectors (path, model, vec) VALUES (?, ?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET model = excluded.model, vec = excluded.vec",
                        (path, model, blob))
                else:
                    # a page re-indexed without an embedding must not keep a stale vector
                    self.db.execute("DELETE FROM vectors WHERE path = ?", (path,))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def remove_page(self, path: str) -> None:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.db.execute("SELECT rowid, title, text FROM pages WHERE path = ?", (path,)).fetchone()
                if old is not None:
                    self.db.execute(
                        "INSERT INTO pages_fts (pages_fts, rowid, title, text) VALUES ('delete', ?, ?, ?)", old)
                    self.db.execute("DELETE FROM pages WHERE path = ?", (path,))
                self.db.execute("DELETE FROM vectors WHERE path = ?", (path,))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def pages(self) -> list:
        rows = self.db.execute("SELECT path, title, mtime FROM pages ORDER BY mtime DESC").fetchall()
        return [{"path": r[0], "title": r[1], "mtime": r[2]} for r in rows]

    def search(self, query: str, query_vector=None, model: str = "", top_k: int = 3, fts: bool = True) -> list:
        """Hybrid search: FTS5 prefix match on every query token, cosine over the vectors of
        `model`, merged by reciprocal rank fusion (k=60). `query_vector=None` (or `fts=False`)
        runs one branch alone - the D10 fallback is the same function with an argument missing."""
        ranks: dict[str, float] = {}
        info: dict[str, tuple] = {}  # path -> (title, text)
        if fts:
            tokens = re.findall(r"\w+", query)
            if tokens:
                match = " OR ".join('"' + t.replace('"', '""') + '"*' for t in tokens)
                rows = self.db.execute(
                    "SELECT rowid FROM pages_fts WHERE pages_fts MATCH ? ORDER BY rank LIMIT 20", (match,)
                ).fetchall()
                for i, (rowid,) in enumerate(rows, start=1):
                    prow = self.db.execute("SELECT path, title, text FROM pages WHERE rowid = ?", (rowid,)).fetchone()
                    if prow is None:
                        continue
                    path, title, text = prow
                    ranks[path] = ranks.get(path, 0.0) + 1 / (60 + i)
                    info[path] = (title, text)
        if query_vector is not None:
            qnorm = math.sqrt(sum(x * x for x in query_vector))
            if qnorm > 0:
                scored = []
                # ponytail: cosine over every row in Python - fine to ~10k pages; sqlite-vec is the upgrade path
                for path, blob in self.db.execute("SELECT path, vec FROM vectors WHERE model = ?", (model,)):
                    vec = struct.unpack(f"<{len(blob) // 4}f", blob)
                    vnorm = math.sqrt(sum(x * x for x in vec))
                    if vnorm == 0:
                        continue
                    cos = sum(a * b for a, b in zip(query_vector, vec)) / (qnorm * vnorm)
                    if cos >= VECTOR_FLOOR:
                        scored.append((cos, path))
                scored.sort(key=lambda x: -x[0])
                for i, (_, path) in enumerate(scored[:20], start=1):
                    ranks[path] = ranks.get(path, 0.0) + 1 / (60 + i)
                    if path not in info:
                        prow = self.db.execute("SELECT title, text FROM pages WHERE path = ?", (path,)).fetchone()
                        if prow is not None:
                            info[path] = prow
        top = sorted(ranks.items(), key=lambda x: -x[1])[:top_k]
        return [{"path": p, "title": info[p][0], "score": s, "snippet": info[p][1][:200]} for p, s in top]

    def memory_applies_after(self, seq: int = 0, limit: int = 20, skip_undone: bool = False) -> list:
        """Applies with seq > `seq`, newest first, capped at `limit` - undo's 'last n' view.
        `skip_undone` (undo's own call) replays the log as a stack: a real edit pushes, an
        undo's own `approval='undo'` row pops the edit it just reversed. What is left, newest
        first, is exactly what has NOT yet been undone - however edits and undos interleave, so
        a second `undo 1` walks further back instead of re-targeting what the first call undid."""
        rows = self.db.execute(
            "SELECT seq, target, op, old, new, approval FROM memory_applies WHERE seq > ? ORDER BY seq",
            (seq,)).fetchall()
        if skip_undone:
            active = []
            for r in rows:
                if r[5] == "undo":
                    if active:
                        active.pop()
                else:
                    active.append(r)
            rows = active
        rows = list(reversed(rows))[:limit]
        return [{"seq": r[0], "target": r[1], "op": r[2], "old": r[3], "new": r[4]} for r in rows]

    def memory_stats(self, now: float | None = None) -> dict:
        """Passes/lines-read/applied per local day for today and the two days before, pending
        proposals, and judgments per local day for the last seven days - `pathfinder memory`'s
        numbers and the stalled-memory trigger. Day boundaries come from `clock.day_starts`
        only (no date maths here)."""
        now = now if now is not None else time.time()
        bounds = clock.day_starts(now, 4)  # today + 3 previous starts -> 3 bounded day windows
        days = []
        for i in range(3):
            start = bounds[i]
            next_start = bounds[i - 1] if i > 0 else now + 1
            passes, read = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(read), 0) FROM memory_passes WHERE ts >= ? AND ts < ?",
                (start, next_start)).fetchone()
            applied = self.db.execute(
                "SELECT COUNT(*) FROM memory_applies WHERE ts >= ? AND ts < ?", (start, next_start)).fetchone()[0]
            days.append({"start": start, "passes": passes, "read": read, "applied": applied})
        jbounds = clock.day_starts(now, 8)  # today + 7 previous starts -> 7 bounded day windows
        judgments = []
        for i in range(7):
            start = jbounds[i]
            next_start = jbounds[i - 1] if i > 0 else now + 1
            judgments.append(self.db.execute(
                "SELECT COUNT(*) FROM judgments WHERE ts >= ? AND ts < ?", (start, next_start)).fetchone()[0])
        return {"days": days, "pending": len(self.pending_memory()), "judgments": judgments}

    def installed_names(self) -> set:
        """Server ids currently in the `installed` state - the installer's clash check."""
        rows = self.db.execute("SELECT id FROM installs WHERE state = 'installed'").fetchall()
        return {r[0] for r in rows}

    def install(self, id: str):
        row = self.db.execute(
            "SELECT id, name, version, state, ts, detail FROM installs WHERE id = ?", (id,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "version": row[2], "state": row[3], "ts": row[4], "detail": row[5]}

    def pins(self) -> dict:
        """server -> tool -> {"description": hash, "schema": hash, "params": [...]} -
        `mcp.pins.PinStore.load`'s source, rebuilt by `pin_accept`/`pin_revoke` events."""
        rows = self.db.execute(
            "SELECT server, tool, description_hash, schema_hash, params_json FROM pins").fetchall()
        out: dict = {}
        for server, tool, description_hash, schema_hash, params_json in rows:
            out.setdefault(server, {})[tool] = {
                "description": description_hash, "schema": schema_hash, "params": json.loads(params_json),
            }
        return out
