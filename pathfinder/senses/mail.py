"""mail sense - IMAP headers only, transplanted from Mark.5's `brain/bridges/mail_bridge.py`.
Never fetches a body in `poll`: `BODY.PEEK` plus a read-only `SELECT` both guard against a
`\\Seen` flag, so polling never marks the user's mail read. The password never lives in config
or the environment - it comes from the macOS Keychain, by service name, once per call."""
from __future__ import annotations

import email
import imaplib
import subprocess
from email import policy
from email.header import decode_header, make_header

from .. import config
from ..signals import Signal
from ..tools import ToolSpec

OPTIONS = {"host": "", "user": "", "mailbox": "INBOX", "keychain_service": "pathfinder-mail"}
TRUST = "third_party"
KINDS = {"new_mail": ("always", "-", "silence", "no rule configured for this kind")}
CADENCE_S = 180

_HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
_TIMEOUT = 20
_last_uid: dict = {}  # (host, user, mailbox) -> highest UID already turned into a Signal (this process only)

CONDITIONS = {
    "sender_in": lambda signal, value, today, now: signal.fields.get("from") in (value or []),
    "sender_domain_in": lambda signal, value, today, now: signal.fields.get("sender_domain") in (value or []),
    "subject_has": lambda signal, value, today, now: any(w in signal.fields.get("subject", "") for w in (value or [])),
}


def _decode(raw):
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def _connect(options):
    """`options` is either what SenseLoop passes to `poll`, or (for a tool call, which gets no
    options argument) `OPTIONS | cfg["senses"]["mail"]` read fresh at call time."""
    if not options.get("host") or not options.get("user"):
        raise RuntimeError("mail sense is not configured (host/user)")
    host, user, mailbox = options["host"], options["user"], options.get("mailbox", "INBOX")
    service = options.get("keychain_service", "pathfinder-mail")
    password = subprocess.run(["security", "find-generic-password", "-s", service, "-a", user, "-w"],
                              capture_output=True, text=True, timeout=5)
    if password.returncode != 0:
        raise RuntimeError(f"no Keychain password for service {service!r}, account {user!r}")
    conn = imaplib.IMAP4_SSL(host, timeout=_TIMEOUT)
    conn.login(user, password.stdout.strip())
    conn.select(mailbox, readonly=True)  # readonly + PEEK: polling never marks mail seen
    return conn, (host, user, mailbox)


def _logout(conn):
    try:
        conn.logout()
    except Exception:
        pass


def _fetch(conn, uid):
    typ, chunk = conn.uid("fetch", str(uid), _HEADERS)
    if typ != "OK" or not chunk or not isinstance(chunk[0], tuple):
        return None
    return email.message_from_bytes(chunk[0][1], policy=policy.default)


def poll(options, now):
    conn, key = _connect(options)
    try:
        watermark = _last_uid.get(key, 0)
        typ, data = conn.uid("search", None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []
        uids = sorted(u for u in (int(x) for x in data[0].split()) if u > watermark)
        if not uids:
            return []
        _last_uid[key] = uids[-1]
        out = []
        for uid in uids:
            msg = _fetch(conn, uid)
            if msg is None:
                continue
            sender = _decode(msg.get("From"))
            subject = _decode(msg.get("Subject")) or "(no subject)"
            message_id = (msg.get("Message-ID") or "").strip() or f"uid:{uid}"
            domain = sender.rsplit("@", 1)[-1].rstrip(">") if "@" in sender else ""
            out.append(Signal(
                sense="mail", kind="new_mail", key=message_id, summary=f"Mail from {sender}: {subject}",
                fields={"from": sender, "sender_domain": domain, "subject": subject,
                        "received": (msg.get("Date") or "").strip()},
                trust=TRUST))
        return out
    finally:
        _logout(conn)


def _list_mail(limit=10, control=None):
    conn, _key = _connect(OPTIONS | (config.load(config.home()).get("senses") or {}).get("mail", {}))
    try:
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        out = []
        for uid in reversed(data[0].split()[-max(1, int(limit)):]):
            msg = _fetch(conn, uid.decode())
            if msg is None:
                continue
            out.append({"id": (msg.get("Message-ID") or "").strip(), "from": _decode(msg.get("From")),
                        "subject": _decode(msg.get("Subject")) or "(no subject)"})
        return out
    finally:
        _logout(conn)


def _read_mail(id, control=None):
    conn, _key = _connect(OPTIONS | (config.load(config.home()).get("senses") or {}).get("mail", {}))
    try:
        typ, data = conn.uid("search", None, f'(HEADER MESSAGE-ID "{id}")')
        if typ != "OK" or not data or not data[0]:
            raise LookupError(f"no mail with Message-ID {id!r}")
        typ, chunk = conn.uid("fetch", data[0].split()[0].decode(), "(BODY.PEEK[])")
        if typ != "OK" or not chunk or not isinstance(chunk[0], tuple):
            raise LookupError(f"could not fetch mail {id!r}")
        msg = email.message_from_bytes(chunk[0][1], policy=policy.default)
        body = msg.get_body(preferencelist=("plain",))
        text = body.get_content() if body else ""
        clipped = text if len(text) <= 8000 else text[:8000] + "...(truncated)"
        return {"id": id, "from": _decode(msg.get("From")), "subject": _decode(msg.get("Subject")), "body": clipped}
    finally:
        _logout(conn)


TOOLS = {
    "list_mail": ToolSpec(
        name="list_mail", description="List recent mail headers.",
        params={"limit": {"type": "integer", "description": "how many, most recent first"}},
        required=["limit"], handler=_list_mail,
    ),
    "read_mail": ToolSpec(
        name="read_mail", description="Read one mail's full body by Message-ID.",
        params={"id": {"type": "string", "description": "the Message-ID, from list_mail"}},
        required=["id"], handler=_read_mail,
    ),
}
