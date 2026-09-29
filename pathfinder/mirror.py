"""The registry copy (D46): `<home>/registry.db`, a local mirror of the official MCP registry that a
daemon thread keeps current and `search`, `find`, `changed_since` read. Not in `state.db` (the
ledger's projection): D46's server deletes this file. Standard library, `catalog`, `clock` only."""

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
from pathlib import Path

from pathfinder import catalog, clock

_log = logging.getLogger("pathfinder.mirror")
RETRY_DELAYS = (10, 60)  # a failing page is retried after 10 s, then 60 s; then the sync stops
MAX_PAGES = 1000  # a last guard per run; the registry is 372 pages of 100 today
MAX_TERMS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, status TEXT,
    updated_at TEXT, seen REAL, item TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS servers_fts USING fts5(name, description, content='servers', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS servers_ai AFTER INSERT ON servers BEGIN
  INSERT INTO servers_fts (rowid, name, description) VALUES (new.id, new.name, new.description); END;
CREATE TRIGGER IF NOT EXISTS servers_ad AFTER DELETE ON servers BEGIN
  INSERT INTO servers_fts (servers_fts, rowid, name, description) VALUES ('delete', old.id, old.name, old.description); END;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_threads: dict = {}  # home -> its sync thread
_lock = threading.RLock()  # also guards creating the db file (see `_open`)


def _open(home) -> sqlite3.Connection:
    path = Path(home) / "registry.db"
    if not path.exists():
        with _lock:  # `PRAGMA journal_mode=WAL` on a fresh file fails at once when openers race: one creator
            if not path.exists():
                tmp = path.with_name("registry.db.new")  # already WAL when it appears under its name
                tmp.touch(mode=0o600)  # born private; the WAL files follow its mode
                db = sqlite3.connect(tmp, isolation_level=None)
                db.execute("PRAGMA journal_mode=WAL")
                db.close()
                tmp.replace(path)
    db = sqlite3.connect(path, isolation_level=None, timeout=30)
    db.executescript(SCHEMA)
    return db


def _meta(db) -> dict:
    return dict(db.execute("SELECT key, value FROM meta"))


def _put(db, **kv) -> None:
    db.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [(k, str(v)) for k, v in kv.items()])


def _upsert(db, item, seen) -> None:
    server, meta = item["server"], (item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
    old = db.execute("SELECT updated_at, seen FROM servers WHERE name = ?", (server["name"],)).fetchone()
    if old and old[0] and old[0] == meta.get("updatedAt"):
        seen = old[1]  # re-fetched in the sync's overlap hour, unchanged: not new
    db.execute("DELETE FROM servers WHERE name = ?", (server["name"],))
    if meta.get("status") != "deleted":  # an incremental sync delivers a deletion as a row
        db.execute("INSERT INTO servers (name, description, status, updated_at, seen, item) VALUES (?,?,?,?,?,?)",
                   (server["name"], server.get("description", ""), meta.get("status") or "active",
                    meta.get("updatedAt", ""), seen, json.dumps(item)))


def _run(db, registry) -> None:
    m = _meta(db)
    cursor = m.get("cursor") or None
    resumed = cursor is not None  # only a cursor stored by an earlier run may be stale
    started = float(m.get("started_at") or time.time()) if cursor else time.time()
    _put(db, started_at=started)
    since = float(m["since"]) if m.get("complete") == "1" else None
    delays = list(RETRY_DELAYS)
    pages = 0
    while pages < MAX_PAGES:
        try:
            items, nxt = catalog.page(registry, cursor, since)
        except Exception as e:
            code = e.code if isinstance(e, urllib.error.HTTPError) else None
            if code:
                e.close()
            if resumed and code in (400, 404, 410):  # the registry refuses the stored cursor: start over, once
                cursor, since, started, resumed = None, None, time.time(), False
                _put(db, cursor="", complete=0, started_at=started)
            elif delays and not (code and 400 <= code < 500):  # a network error or a 5xx is worth a retry
                time.sleep(delays.pop(0))
            else:
                raise
            continue
        if nxt and nxt == cursor:
            raise RuntimeError("the registry repeated a cursor")
        pages, resumed, delays = pages + 1, False, list(RETRY_DELAYS)
        db.execute("BEGIN")  # a page and its cursor are one transaction; a cut-off page rolls back
        for item in items:
            _upsert(db, item, time.time() if since else 0)
        _put(db, cursor=nxt or "")
        if not nxt:
            _put(db, complete=1, since=started - 3600, synced_at=time.time(), failed_at="", error="")
        db.execute("COMMIT")
        if not nxt:
            return
        cursor = nxt
    raise RuntimeError(f"the registry gave more than {MAX_PAGES} pages")


def _sync(home, registry) -> None:
    db = None
    try:
        db = _open(home)
        _run(db, registry)
    except Exception as e:
        _log.warning("registry copy sync failed: %s", e)
        try:
            if db is None:
                db = _open(home)
            db.rollback()
            _put(db, failed_at=time.time(), error=catalog.oneline(e, 200) or type(e).__name__)
        except Exception:
            pass  # nowhere to store it: the warning is all there is
    finally:
        if db is not None:
            db.close()


def refresh(home, registry) -> None:
    """Start the sync on a daemon thread unless one is alive; nothing waits for it (the first sync
    takes minutes)."""
    with _lock:
        thread = _threads.get(str(home))
        if thread is None or not thread.is_alive():
            db = _open(home)
            if _meta(db).get("registry") != registry:  # another registry: the copy starts empty (D45)
                db.executescript("DELETE FROM servers; DELETE FROM meta; INSERT INTO servers_fts (servers_fts) VALUES ('delete-all')")
                _put(db, registry=registry)
            db.close()
            _threads[str(home)] = threading.Thread(target=_sync, args=(home, registry), daemon=True)
            _threads[str(home)].start()


def _q(home, sql, args=()) -> list:
    db = _open(home)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def _match(query) -> str:
    """FTS5 query: distinct words, at most the MAX_TERMS longest, each a prefix term unless shorter than 3
    characters ("my", "to", "i" as prefixes would match half the registry)."""
    words = sorted(dict.fromkeys(w.lower() for w in re.findall(r"\w+", query)), key=len, reverse=True)[:MAX_TERMS]
    return " OR ".join(f'"{w}"' + ("*" if len(w) >= 3 else "") for w in words)


def search(home, query, registry=None, limit=10) -> tuple:
    """`(items, note)`: active servers with a name or description word starting like any query word,
    best first. `note` is "" for a complete, healthy copy, else one sentence on why it may be thin."""
    m = dict(_q(home, "SELECT key, value FROM meta"))
    if registry and (m.get("registry") != registry or m.get("complete") != "1" or m.get("failed_at")):
        refresh(home, registry)
        m = dict(_q(home, "SELECT key, value FROM meta"))
    match = _match(query)
    rows = _q(home, "SELECT s.item FROM servers_fts JOIN servers s ON s.id = servers_fts.rowid "
              "WHERE servers_fts MATCH ? AND s.status = 'active' ORDER BY bm25(servers_fts, 10.0, 1.0) LIMIT ?",
              (match, limit)) if match else []
    n = _q(home, "SELECT COUNT(*) FROM servers")[0][0]
    note = ""
    if m.get("failed_at"):
        note = f"the last registry sync failed at {clock.format_local(float(m['failed_at']))[-5:]}: {m.get('error', '')}"
    elif m.get("complete") != "1":
        note = (f"the registry copy is still syncing ({n} so far, since {clock.format_local(float(m['started_at']))[-5:]})"
                if n else "the registry copy is empty; the first sync takes several minutes")
    return [json.loads(r[0]) for r in rows], note


def find(home, name) -> list:
    """The exact name, else every name ending in `/name`; a deprecated row still answers."""
    rows = (_q(home, "SELECT item FROM servers WHERE name = ?", (name,))
            or _q(home, "SELECT item FROM servers WHERE substr(name, -?) = ?", (len(name) + 1, "/" + name)))
    return [json.loads(r[0]) for r in rows]


def changed_since(home, ts) -> list:
    """Active rows an incremental sync wrote after `ts` (a first sync writes `seen = 0`)."""
    return [json.loads(r[0]) for r in _q(home, "SELECT item FROM servers WHERE seen > ? AND status = 'active'", (ts,))]
