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

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_ts REAL, last_ts REAL, turns INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, call_id TEXT, session TEXT, turn TEXT, tool TEXT,
    args_json TEXT, messages_json TEXT, ts REAL, decision TEXT, seq INTEGER
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
CREATE TABLE IF NOT EXISTS pages (rowid INTEGER PRIMARY KEY, path TEXT UNIQUE, title TEXT, mtime REAL, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(title, text, content='pages', content_rowid='rowid');
CREATE TABLE IF NOT EXISTS vectors (path TEXT PRIMARY KEY, model TEXT, vec BLOB);
CREATE TABLE IF NOT EXISTS memory_applies (
    seq INTEGER PRIMARY KEY, ts REAL, target TEXT, op TEXT, old TEXT, new TEXT, approval TEXT
);
CREATE TABLE IF NOT EXISTS pins (
    server TEXT, tool TEXT, description_hash TEXT, schema_hash TEXT, params_json TEXT, ts REAL,
    annotations_hash TEXT, PRIMARY KEY (server, tool)
);
CREATE TABLE IF NOT EXISTS installs (
    id TEXT PRIMARY KEY, name TEXT, version TEXT, state TEXT, ts REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS results (seq INTEGER PRIMARY KEY, clone TEXT, session TEXT, turn TEXT, call_id TEXT,
  tool TEXT, ok INTEGER, body TEXT);
CREATE TABLE IF NOT EXISTS clones (id TEXT PRIMARY KEY, parent TEXT, preset TEXT, state TEXT, session TEXT,
  turn TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS gates (seq INTEGER PRIMARY KEY, clone TEXT, session TEXT, turn TEXT, call_id TEXT,
  tool TEXT, args_json TEXT, effect TEXT, brief TEXT, decision TEXT, reported INTEGER, result INTEGER, ts REAL,
  title TEXT, recipient TEXT, scope TEXT);
CREATE TABLE IF NOT EXISTS context (seq INTEGER PRIMARY KEY, clone TEXT, session TEXT, turn TEXT, type TEXT,
  event_json TEXT);
CREATE INDEX IF NOT EXISTS context_by_clone ON context (clone, session, seq);
"""

NEW_TABLES = ("signals", "decisions", "judgments", "deliveries", "reactions", "presence", "signal_turns",
              "pages_fts", "pages", "vectors", "memory_applies", "pins", "installs",
              "results", "clones", "gates", "context")
# the v2 events a clone's window is assembled from (a result's body stays in `results`)
CONTEXT_TYPES = frozenset({"user", "assistant", "result", "turn_start", "turn_end", "gate", "resume", "interrupted",
                           "summary"})
_GATE_COLUMNS = ("seq, clone, session, turn, call_id, tool, args_json, effect, brief, decision, reported, result, ts, "
                 "title, recipient, scope")


@dataclass
class Today:
    """The counters `rules.decide` needs, all local-day scoped (`clock.day_start`)."""
    spoken: int               # decisions {action: speak} today - the daily budget
    judgments: int            # judgment events today - the judgment_cap
    last_signal_ts: float | None  # ts of the last signal turn opened (rate limit)
    burst: int                # signal turns opened in the last 60 s
    recent_counts: dict       # kind -> signals of that kind in the last 600 s, incl. this one (count_over)
    away: bool = True         # the newest presence is stale/idle (claude_code's "away")


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
        # shipped (v2: approvals.seq) is added here, so an existing home keeps starting.
        if "seq" not in {r[1] for r in self.db.execute("PRAGMA table_info(approvals)")}:
            self.db.execute("ALTER TABLE approvals ADD COLUMN seq INTEGER")  # v2: one approval-id namespace
        gate_columns = {r[1] for r in self.db.execute("PRAGMA table_info(gates)")}
        for column in ("title", "recipient", "scope"):  # P2: what a gate names, and an approval's "always" scope
            if column not in gate_columns:
                self.db.execute(f"ALTER TABLE gates ADD COLUMN {column} TEXT")
        if "annotations_hash" not in {r[1] for r in self.db.execute("PRAGMA table_info(pins)")}:
            self.db.execute("ALTER TABLE pins ADD COLUMN annotations_hash TEXT")  # P2: annotations are pinned
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
        # history is what was said: a user event needs origin "user" (not a signal envelope),
        # an assistant event needs final=true (not tool chatter mid-turn)
        if event["type"] == "user" and event.get("origin") == "user":
            self._insert_message(seq, session, "user", event.get("text", ""), event["ts"])
        if event["type"] == "assistant" and event.get("final"):
            self._insert_message(seq, session, "assistant", event.get("text", ""), event["ts"])
        if event["type"] == "turn_end" and session:
            if event.get("kind") != "gated":  # a gate is a pause, not an exchange
                self.db.execute("UPDATE sessions SET turns = turns + 1 WHERE id = ?", (session,))
        if event["type"] == "approval_pending":
            self.db.execute(
                "INSERT OR IGNORE INTO approvals (id, call_id, session, turn, tool, args_json, messages_json, ts, decision, "
                "seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (event["id"], event["call_id"], session, event.get("turn", ""), event["tool"],
                 json.dumps(event["args"]), json.dumps(event["messages"]), event["ts"], seq),
            )
        if event["type"] == "approval":
            self.db.execute("UPDATE approvals SET decision = ? WHERE id = ?", (event["decision"], event["id"]))
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
        if event["type"] == "memory_apply":
            self.db.execute(
                "INSERT INTO memory_applies (seq, ts, target, op, old, new, approval) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seq, event["ts"], event["target"], event["op"], event.get("old"), event.get("new"),
                 event.get("approval")))
        if event["type"] == "pin_accept":
            self.db.execute(
                "INSERT OR REPLACE INTO pins (server, tool, description_hash, schema_hash, params_json, ts, "
                "annotations_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event["server"], event["tool"], event["description_hash"], event["schema_hash"],
                 json.dumps(event["params"]), event["ts"], event.get("annotations_hash")))
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
        if event.get("clone"):
            self._project_clone(seq, event)
        # a pass's own events (origin "memory") must not look like the user is still there -
        # they would keep the session alive forever and the pass would never see it go idle.
        # Nor may a screen's 30 s `presence` heartbeat or a signal's silent triage (D41: live, one
        # session ran four days) - of a signal turn, only the notice it delivered is conversation.
        # Nor a job's own events (D63).
        origin = event.get("origin")
        if (session and origin not in ("memory", "job") and event["type"] != "presence"
                and (origin != "signal" or event["type"] == "assistant")):
            self.db.execute("UPDATE sessions SET last_ts = ? WHERE id = ?", (event["ts"], session))
        self.db.execute("UPDATE meta SET value = ? WHERE key = 'cursor'", (str(seq),))

    def _project_clone(self, seq, event):
        """v2 events (they carry `clone`): context rows, the result store, gates, clone rows. A gate
        changes no clone state (V22); a clone moves only on turn_start, turn_end and interrupted."""
        kind, clone, session, turn = event["type"], event["clone"], event.get("session", ""), event.get("turn", "")
        if kind in CONTEXT_TYPES:
            row = {k: v for k, v in event.items() if not (kind == "result" and k == "body")}
            self.db.execute("INSERT OR IGNORE INTO context (seq, clone, session, turn, type, event_json) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (seq, clone, session, turn, kind, json.dumps(row)))
        if kind == "result":
            self.db.execute("INSERT OR IGNORE INTO results (seq, clone, session, turn, call_id, tool, ok, body) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (seq, clone, session, turn, event.get("call_id", ""), event.get("tool", ""),
                             int(bool(event.get("ok"))), json.dumps(event.get("body"), ensure_ascii=False)))
            if event.get("gate"):  # a handed-off call's outcome: its gate points at it
                self.db.execute("UPDATE gates SET result = ? WHERE seq = ?", (seq, event["gate"]))
        if kind == "gate":
            self.db.execute(f"INSERT OR IGNORE INTO gates ({_GATE_COLUMNS}) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?, NULL)",
                            (seq, clone, session, turn, event["call_id"], event["tool"], json.dumps(event["args"]),
                             event.get("effect", ""), event.get("brief", ""), event["ts"], event.get("title", ""),
                             event.get("recipient", "")))
        if kind == "resume":
            self.db.execute("UPDATE gates SET decision = ?, scope = ? WHERE seq = ? AND decision IS NULL",  # first wins
                            (event["decision"], event.get("scope", ""), event["gate"]))
        if kind == "user" and event.get("gate"):  # a report turn: one event for every gate it reports
            self.db.executemany("UPDATE gates SET reported = 1 WHERE seq = ?", [(g,) for g in event["gate"]])
        if kind == "spawn":
            self.db.execute("INSERT OR IGNORE INTO clones (id, parent, preset, state, session, turn, ts) "
                            "VALUES (?, ?, ?, 'parked', ?, ?, ?)",
                            (clone, event.get("parent", ""), event.get("preset", ""), session, turn, event["ts"]))
        if kind in ("turn_start", "turn_end", "interrupted"):
            self.db.execute("UPDATE clones SET state = ?, session = ?, turn = ?, ts = ? WHERE id = ?",
                            ("running" if kind == "turn_start" else "parked", session, turn, event["ts"], clone))

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
        # v1's dead tables (U10a): a reindex of an older home removes them
        self.db.executescript("".join(f"DROP TABLE IF EXISTS {t};" for t in ("jobs", "summaries", "memory_passes")))
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('cursor', '0')")
        self.db.commit()
        return self.catch_up(ledger)

    def history(self, session: str) -> list:
        rows = self.db.execute("SELECT role, content FROM messages WHERE session = ? ORDER BY seq", (session,))
        return [{"role": role, "content": content} for role, content in rows]

    def last_user_seq(self, session: str) -> int | None:
        """Max seq of `session`'s own user messages - a user turn's `edit_memory` has no seq of
        its own to cite, so this is the evidence the gate fills in for it."""
        row = self.db.execute(
            "SELECT MAX(seq) FROM messages WHERE session = ? AND role = 'user'", (session,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def session(self, sid: str):
        row = self.db.execute(
            "SELECT id, started_ts, last_ts, turns FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "started_ts": row[1], "last_ts": row[2], "turns": row[3]}

    def result(self, seq: int) -> dict | None:
        """A stored result by its seq (`r:N`), body included - `read_result` is this one SELECT."""
        row = self.db.execute("SELECT seq, clone, session, turn, call_id, tool, ok, body FROM results "
                              "WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            return None
        return {"seq": row[0], "clone": row[1], "session": row[2], "turn": row[3], "call_id": row[4],
                "tool": row[5], "ok": bool(row[6]), "body": json.loads(row[7])}

    def context(self, clone: str, session: str, after: int) -> list[dict]:
        """`clone`'s recorded events in `session` with seq > `after`, oldest first (a result without its body)."""
        rows = self.db.execute("SELECT event_json FROM context WHERE clone = ? AND session = ? AND seq > ? "
                               "ORDER BY seq", (clone, session, after))
        return [json.loads(r[0]) for r in rows]

    def last_fold(self, clone: str, session: str) -> dict | None:
        """The newest `summary` event of `clone` in `session`."""
        row = self.db.execute("SELECT event_json FROM context WHERE clone = ? AND session = ? AND type = 'summary' "
                              "ORDER BY seq DESC LIMIT 1", (clone, session)).fetchone()
        return None if row is None else json.loads(row[0])

    def clone_row(self, id: str) -> dict | None:
        row = self.db.execute("SELECT id, parent, preset, state, session, turn, ts FROM clones WHERE id = ?",
                              (id,)).fetchone()
        return None if row is None else dict(zip(("id", "parent", "preset", "state", "session", "turn", "ts"), row))

    @staticmethod
    def _gate_row(row) -> dict:
        gate = dict(zip(_GATE_COLUMNS.split(", "), row))
        gate["args"] = json.loads(gate.pop("args_json"))
        gate["reported"] = bool(gate["reported"])
        return gate

    def gate(self, seq: int) -> dict | None:
        """A gate by its seq: `decision` is None while it is open; `result` is the seq of the
        handed-off call's `result`, None until it ran."""
        row = self.db.execute(f"SELECT {_GATE_COLUMNS} FROM gates WHERE seq = ?", (seq,)).fetchone()
        return None if row is None else self._gate_row(row)

    def open_gates(self) -> list[dict]:
        """Every gate not yet reported, decided or not (`decision` None while open), oldest first."""
        rows = self.db.execute(f"SELECT {_GATE_COLUMNS} FROM gates WHERE reported = 0 ORDER BY seq")
        return [self._gate_row(r) for r in rows]

    def session_grants(self, session: str) -> set[str]:
        """The tools of `session`'s gates approved with scope `session` (Q5: "always allow" for this session)."""
        rows = self.db.execute("SELECT DISTINCT tool FROM gates WHERE session = ? AND decision = 'approved' "
                               "AND scope = 'session'", (session,))
        return {r[0] for r in rows}

    def approval_item(self, seq: int) -> dict | None:
        """What `approve <seq>` names: a clone's gate, or a proposal by its `approval_pending` seq."""
        gate = self.gate(seq)
        if gate is not None:
            return {"kind": "gate", **gate}
        row = self.db.execute("SELECT id FROM approvals WHERE seq = ?", (seq,)).fetchone()
        return None if row is None else {"kind": "proposal", **self.approval(row[0])}

    def pending(self) -> list:
        """Approvals with no decision yet, oldest first - what survives a restart."""
        rows = self.db.execute(
            "SELECT id, session, turn, tool, args_json, ts FROM approvals WHERE decision IS NULL ORDER BY ts"
        ).fetchall()
        return [{"id": r[0], "session": r[1], "turn": r[2], "tool": r[3], "args": json.loads(r[4]), "ts": r[5]}
                for r in rows]

    def open_approvals(self, session: str) -> list:
        """D52: `session`'s pending approvals newer than its last user message - a gate the owner
        has talked past is not open."""
        last = self.db.execute("SELECT MAX(ts) FROM messages WHERE session = ? AND role = 'user'",
                               (session,)).fetchone()[0]
        return [p for p in self.pending()
                if p["session"] == session and (last is None or p["ts"] > last)]

    def pending_memory(self) -> list:
        """The pending approvals whose tool is `edit_memory` - `pathfinder approvals`'s memory view."""
        return [p for p in self.pending() if p["tool"] == "edit_memory"]

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
        away = True
        if idle_threshold_s is not None:
            row = self.presence()
            fresh = row is not None and now - row["ts"] < 60
            away = not fresh or row["idle_s"] >= idle_threshold_s
        return Today(spoken, judgments, last_signal_ts, burst, recent_counts, away)

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
        """server -> tool -> {"description": hash, "schema": hash, "annotations": hash or None (a pin from
        before P2), "params": [...]} - `mcp.pins.PinStore.load`'s source, rebuilt by `pin_accept`/`pin_revoke`."""
        rows = self.db.execute(
            "SELECT server, tool, description_hash, schema_hash, annotations_hash, params_json FROM pins").fetchall()
        out: dict = {}
        for server, tool, description_hash, schema_hash, annotations_hash, params_json in rows:
            out.setdefault(server, {})[tool] = {
                "description": description_hash, "schema": schema_hash, "annotations": annotations_hash,
                "params": json.loads(params_json),
            }
        return out
