"""IMAP plumbing for the mail sense (`mail.py`): connect with the Keychain password onto a
read-only `SELECT`, fetch headers through `BODY.PEEK`, search with the query sent as a literal.
Nothing here marks mail read. The password is read per call and never kept."""
from __future__ import annotations

import contextlib
import email
import imaplib
from email import policy
from email.header import decode_header, make_header

from .. import keychain

_HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
_TIMEOUT = 20


def decode(raw):
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def connect(options):
    """The key includes UIDVALIDITY: a mailbox rebuild restarts the watermark, not silence."""
    if not options.get("host") or not options.get("user"):
        raise RuntimeError("mail sense is not configured (host/user)")
    host, user, mailbox = options["host"], options["user"], options.get("mailbox", "INBOX")
    service = options.get("keychain_service", "pathfinder-mail")
    password = keychain.secret(service, user)
    if password is None:
        raise RuntimeError(f"no Keychain password for service {service!r}, account {user!r}")
    conn = imaplib.IMAP4_SSL(host, timeout=_TIMEOUT)
    conn.login(user, password)
    conn.select(mailbox, readonly=True)  # readonly + PEEK: polling never marks mail seen
    _typ, uidval = conn.response("UIDVALIDITY")
    return conn, (host, user, mailbox, uidval[0] if uidval and uidval[0] else b"0")


def logout(conn):
    with contextlib.suppress(Exception):
        conn.logout()


def fetch(conn, uid):
    typ, chunk = conn.uid("fetch", str(uid), _HEADERS)
    if typ != "OK" or not chunk or not isinstance(chunk[0], tuple):
        return None
    return email.message_from_bytes(chunk[0][1], policy=policy.default)


def message_id(msg, fallback):
    """Missing, or not wrapped in `<...>` (e.g. folded wrong) - falls back like no header."""
    mid = (msg.get("Message-ID") or "").strip()
    return mid if mid.startswith("<") and mid.endswith(">") else fallback


def search(conn, query):
    """UIDs matching `query`: Gmail's own syntax (`X-GM-RAW`) where the server speaks it, `TEXT`
    elsewhere. The query goes as a literal after the command, never into the command string - no
    quoting to get wrong, no CRLF to inject, and a Korean query works."""
    key = "X-GM-RAW" if "X-GM-EXT-1" in conn.capabilities else "TEXT"
    conn.literal = query.encode("utf-8")
    typ, data = conn.uid("search", "CHARSET", "UTF-8", key)
    return data[0].split() if typ == "OK" and data and data[0] else []
