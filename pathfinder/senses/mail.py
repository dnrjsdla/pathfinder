"""mail sense - IMAP headers only (the plumbing is `_imap.py`). Never fetches a body in `poll`
(`BODY.PEEK` + a read-only `SELECT`). The password never lives in config/env - it comes from the
macOS Keychain per call. What a tool hands the model of someone else's mail passes `oneline`."""
from __future__ import annotations

from .. import config
from ..signals import Signal
from ..tools import ToolSpec, oneline
from . import _imap

OPTIONS = {"host": "", "user": "", "mailbox": "INBOX", "keychain_service": "pathfinder-mail"}
TRUST = "third_party"
KINDS = {"new_mail": ("always", "-", "silence", "no rule configured for this kind")}
CADENCE_S = 180

# `signals.load` binds this to the daemon's own home - the tools read it, never `config.home()`.
HOME = None

# (host, user, mailbox, UIDVALIDITY) -> highest UNSEEN UID this process has looked at. A process's
# first poll of a key only takes it and announces nothing, so a restart re-announces nothing; mail
# that came while the daemon was down is found with list_mail/search_mail.
_last_uid: dict = {}

CONDITIONS = {
    "sender_in": lambda signal, value, today, now: signal.fields.get("from") in (value or []),
    "sender_domain_in": lambda signal, value, today, now: signal.fields.get("sender_domain") in (value or []),
    "subject_has": lambda signal, value, today, now: any(w in signal.fields.get("subject", "") for w in (value or [])),
}

def poll(options, now):
    conn, key = _imap.connect(options)
    try:
        found = _imap.uids(conn, None, "UNSEEN")
        first, watermark = key not in _last_uid, _last_uid.get(key, 0)
        uids = sorted(u for u in (int(x) for x in found) if u > watermark)
        _last_uid[key] = max([watermark, *uids])
        if first or not uids:
            return []
        out = []
        for uid in uids:
            msg = _imap.fetch(conn, uid)
            if msg is None:
                continue
            row = _row(_imap.message_id(msg, key, uid), msg)  # the notice is one clean line, as a tool row
            domain = row["from"].rsplit("@", 1)[-1].rstrip(">") if "@" in row["from"] else ""
            out.append(Signal(
                sense="mail", kind="new_mail", key=row["id"], summary=f"Mail from {row['from']}: {row['subject']}",
                fields={"from": row["from"], "sender_domain": domain, "subject": row["subject"], "received": row["date"]},
                trust=TRUST))
        return out
    finally:
        _imap.logout(conn)


def _options():
    if HOME is None:
        raise RuntimeError("mail sense not loaded (no home bound)")
    return OPTIONS | (config.load(HOME).get("senses") or {}).get("mail", {})


def _row(id, msg):
    head = {name: oneline(_imap.header(msg, name), 300) for name in ("From", "Subject", "Date")}
    return {"id": id, "from": head["From"], "subject": head["Subject"] or "(no subject)", "date": head["Date"]}


def _newest(conn, key, uids, limit):
    """The newest `limit` of `uids`, newest first, headers only."""
    out = []
    for uid in sorted(uids, key=int)[-limit:][::-1]:
        msg = _imap.fetch(conn, uid.decode())
        if msg is not None:
            out.append(_row(_imap.message_id(msg, key, uid), msg))
    return out


def _list_mail(limit=10, control=None):
    if not 1 <= limit <= 20:
        raise ValueError("limit must be 1-20")
    conn, key = _imap.connect(_options())
    try:
        return _newest(conn, key, _imap.uids(conn, None, "ALL"), limit)
    finally:
        _imap.logout(conn)


def _search_mail(query, limit=10, control=None):
    if not 0 < len(query) <= 200 or not query.isprintable() or not 1 <= limit <= 20:
        raise ValueError("query must be 1-200 printable characters, limit 1-20")
    conn, key = _imap.connect(_options())
    try:
        return _newest(conn, key, _imap.search(conn, query), limit)
    finally:
        _imap.logout(conn)


def _read_mail(id, control=None):
    if not _imap.valid_id(id):  # model-supplied, and a Message-ID goes into a SEARCH string
        raise ValueError("not a Message-ID")
    conn, key = _imap.connect(_options())
    try:
        msg = _imap.fetch(conn, _imap.uid_of(conn, key, id), "(BODY.PEEK[])")
        if msg is None:
            raise LookupError(f"could not fetch mail {id!r}")
        # keeps its newlines; each line loses what `oneline` drops (controls, bidi, zero-width)
        text = "\n".join(oneline(line) for line in _imap.plain_text(msg).splitlines())
        clipped = text if len(text) <= 8000 else text[:8000] + "...(truncated)"
        return {**_row(id, msg), "body": clipped}
    finally:
        _imap.logout(conn)


TOOLS = {
    "list_mail": ToolSpec(name="list_mail", description="List recent mail headers.",
                          params={"limit": {"type": "integer", "description": "how many, 1-20, most recent first"}},
                          required=["limit"], handler=_list_mail),
    "search_mail": ToolSpec(name="search_mail", description="Search mail; newest matching headers first. On Gmail the query "
                            "takes Gmail's search syntax: from:, subject:, after:2026/09/01, newer_than:7d, plain words.",
                            params={"query": {"type": "string", "description": "the search, at most 200 characters"},
                                    "limit": {"type": "integer", "description": "how many, 1-20 (default 10)"}},
                            required=["query"], handler=_search_mail),
    "read_mail": ToolSpec(name="read_mail", description="Read one mail's full body by its id.",
                          params={"id": {"type": "string", "description": "the id from list_mail or search_mail"}},
                          required=["id"], handler=_read_mail),
}
