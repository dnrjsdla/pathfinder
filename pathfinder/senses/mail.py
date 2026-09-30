"""mail sense - IMAP headers only (the plumbing is `_imap.py`). Never fetches a body in `poll`
(`BODY.PEEK` + a read-only `SELECT`). The password never lives in config/env - it comes from the
macOS Keychain per call. What a tool hands the model of someone else's mail passes `oneline`."""
from __future__ import annotations

import email
import re
from email import policy

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

_MSGID_RE = re.compile(r"[!-~]{1,250}")
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
        typ, data = conn.uid("search", None, "UNSEEN")
        if typ != "OK":
            return []
        first, watermark = key not in _last_uid, _last_uid.get(key, 0)
        uids = sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > watermark)
        _last_uid[key] = max([watermark, *uids])
        if first or not uids:
            return []
        out = []
        for uid in uids:
            msg = _imap.fetch(conn, uid)
            if msg is None:
                continue
            sender = _imap.decode(msg.get("From"))
            subject = _imap.decode(msg.get("Subject")) or "(no subject)"
            message_id = _imap.message_id(msg, f"uid:{uid}")
            domain = sender.rsplit("@", 1)[-1].rstrip(">") if "@" in sender else ""
            out.append(Signal(
                sense="mail", kind="new_mail", key=message_id, summary=f"Mail from {sender}: {subject}",
                fields={"from": sender, "sender_domain": domain, "subject": subject,
                        "received": (msg.get("Date") or "").strip()},
                trust=TRUST))
        return out
    finally:
        _imap.logout(conn)


def _options():
    if HOME is None:
        raise RuntimeError("mail sense not loaded (no home bound)")
    return OPTIONS | (config.load(HOME).get("senses") or {}).get("mail", {})


def _row(id, msg):
    head = {name: oneline(_imap.decode(msg.get(name)), 300) for name in ("From", "Subject", "Date")}
    return {"id": id, "from": head["From"], "subject": head["Subject"] or "(no subject)", "date": head["Date"]}


def _newest(conn, uids, limit):
    """The newest `limit` of `uids`, newest first, headers only."""
    out = []
    for uid in sorted(uids, key=int)[-limit:][::-1]:
        msg = _imap.fetch(conn, uid.decode())
        if msg is not None:
            out.append(_row(_imap.message_id(msg, f"uid:{uid.decode()}"), msg))
    return out


def _list_mail(limit=10, control=None):
    conn, _key = _imap.connect(_options())
    try:
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        return _newest(conn, data[0].split(), max(1, int(limit)))
    finally:
        _imap.logout(conn)


def _search_mail(query, limit=10, control=None):
    if not 0 < len(query) <= 200 or not 1 <= limit <= 20:
        raise ValueError("query must be 1-200 characters, limit 1-20")
    conn, _key = _imap.connect(_options())
    try:
        return _newest(conn, _imap.search(conn, query), limit)
    finally:
        _imap.logout(conn)


def _read_mail(id, control=None):
    # model-supplied `id` feeds a SEARCH string raw - unguarded, `"` widens it, CRLF injects a command.
    if not _MSGID_RE.fullmatch(id) or '"' in id or "\\" in id:
        raise ValueError("not a Message-ID")
    conn, _key = _imap.connect(_options())
    try:
        typ, data = conn.uid("search", None, f'(HEADER MESSAGE-ID "{id}")')
        if typ != "OK" or not data or not data[0]:
            raise LookupError(f"no mail with Message-ID {id!r}")
        typ, chunk = conn.uid("fetch", data[0].split()[0].decode(), "(BODY.PEEK[])")
        if typ != "OK" or not chunk or not isinstance(chunk[0], tuple):
            raise LookupError(f"could not fetch mail {id!r}")
        msg = email.message_from_bytes(chunk[0][1], policy=policy.default)
        body = msg.get_body(preferencelist=("plain",))
        # keeps its newlines; each line loses what `oneline` drops (controls, bidi, zero-width)
        text = "\n".join(oneline(line) for line in (body.get_content() if body else "").splitlines())
        clipped = text if len(text) <= 8000 else text[:8000] + "...(truncated)"
        return {**_row(id, msg), "body": clipped}
    finally:
        _imap.logout(conn)


TOOLS = {
    "list_mail": ToolSpec(name="list_mail", description="List recent mail headers.",
                          params={"limit": {"type": "integer", "description": "how many, most recent first"}},
                          required=["limit"], handler=_list_mail),
    "search_mail": ToolSpec(name="search_mail", description="Search mail; newest matching headers first. On Gmail the query "
                            "takes Gmail's search syntax: from:, subject:, after:2026/09/01, newer_than:7d, plain words.",
                            params={"query": {"type": "string", "description": "the search, at most 200 characters"},
                                    "limit": {"type": "integer", "description": "how many, 1-20 (default 10)"}},
                            required=["query"], handler=_search_mail),
    "read_mail": ToolSpec(name="read_mail", description="Read one mail's full body by Message-ID.",
                          params={"id": {"type": "string", "description": "the Message-ID, from list_mail or search_mail"}},
                          required=["id"], handler=_read_mail),
}
