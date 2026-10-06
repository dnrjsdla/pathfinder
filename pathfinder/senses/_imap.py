"""IMAP plumbing for the mail sense (`mail.py`): connect with the Keychain password onto a
read-only `SELECT`, fetch through `BODY.PEEK`, search with the query sent as a literal. Nothing
here marks mail read. The password is read per call and never kept. Headers are read from their
raw values, so one hostile mail degrades to its own row instead of failing a whole listing."""
from __future__ import annotations

import contextlib
import email
import imaplib
import re
from email import policy
from email.header import decode_header, make_header

from .. import keychain

_HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
_TIMEOUT = 20
_MSGID_RE = re.compile(r"[!-~]{1,250}")


def _decode(raw):
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


def fetch(conn, uid, parts=_HEADERS):
    typ, chunk = conn.uid("fetch", str(uid), parts)
    if typ != "OK" or not chunk or not isinstance(chunk[0], tuple):
        return None
    return email.message_from_bytes(chunk[0][1], policy=policy.default)


def _raw(msg, name):
    raw = next((v for k, v in msg.raw_items() if k.lower() == name.lower()), "")
    return raw.encode("utf-8", "surrogateescape").decode("utf-8", "replace")  # raw 8-bit UTF-8 too


def header(msg, name):
    """A header's text from its raw value, never `msg.get`'s structured parse: that raises on a
    hostile encoded word (an address decoding to CR/LF), and one such mail would fail every tool."""
    return _decode(_raw(msg, name))


def valid_id(id):
    """Safe inside a quoted SEARCH string - unguarded, `"` widens it and CRLF injects a command."""
    return bool(_MSGID_RE.fullmatch(id)) and '"' not in id and "\\" not in id


def message_id(msg, key, uid):
    """A Message-ID `read_mail` can take back, else `uid:<UIDVALIDITY>.<UID>` (`key` from `connect`)
    when it is missing, folded wrong, too long, or holds anything outside printable ASCII."""
    mid = _raw(msg, "Message-ID").strip()
    return mid if mid.startswith("<") and mid.endswith(">") and valid_id(mid) else f"uid:{key[3].decode()}.{int(uid)}"


def uids(conn, *criteria):
    """The matching UIDs. A refusal raises with the server's text - it must not read as no mail."""
    typ, data = conn.uid("search", *criteria)
    if typ != "OK":
        raise RuntimeError(f"the mail server refused the search: {data[-1]!r}")
    return data[0].split() if data and data[0] else []


def search(conn, query):
    """UIDs matching `query`: Gmail's own syntax (`X-GM-RAW`) where the server speaks it, `TEXT`
    elsewhere. The query goes as a literal after the command, never into the command string - no
    quoting to get wrong, no CRLF to inject, and a Korean query works."""
    key = "X-GM-RAW" if "X-GM-EXT-1" in conn.capabilities else "TEXT"
    conn.literal = query.encode("utf-8")
    return uids(conn, "CHARSET", "UTF-8", key)


def uid_of(conn, key, id):
    """The UID behind an id `message_id` handed out (`id` has passed `valid_id`). A `uid:` id names
    its mailbox generation: after a rebuild the same UID is another mail, so it is refused."""
    m = re.fullmatch(r"uid:([0-9]{1,10})\.([0-9]{1,10})", id)
    if m and m[1].encode() != key[3]:
        raise ValueError("that id is from before the mailbox was rebuilt; list the mail again")
    if m:
        return m[2]
    found = uids(conn, None, f'(HEADER MESSAGE-ID "{id}")')
    if not found:
        raise LookupError(f"no mail with Message-ID {id!r}")
    return found[0].decode()


def plain_text(msg):
    """The plain-text part. HTML only gives a marker; a wrong charset label decodes as UTF-8 with
    replacement instead of failing the read."""
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        return "(no plain-text part)"
    try:
        return part.get_content()
    except LookupError:
        return part.get_payload(decode=True).decode("utf-8", "replace")
