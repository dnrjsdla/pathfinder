"""The masked input of `/secret ID NAME` (D53, D64). The screen (`tui.py`) keeps the key loop,
the escape parser, the input buffer and the switch into and out of the masked mode, and calls in
here; an `Entry` holds what is the masked entry's own: where its value goes and where a bracketed
paste stands. The value goes to the daemon as `secret` - from `/secret` and from a harness ask
(`secret.ask`, D53's main path) alike - which stores it in the Keychain and proposes again an
install waiting for it; it never reaches the ledger, another screen or the model."""
from __future__ import annotations

import os
import select

from . import keychain

SECRET_PROMPT = "비밀 · secret> "  # the masked input line: this, then one `*` per character
SECRET_DRAIN_S = 0.05           # input within this after a masked Enter is a paste that ran past the value

# Fixed strings, Korean and English side by side (D6), as in `tui.STRINGS`.
STRINGS = {
    "secret_usage": "사용법 · /secret ID NAME takes a server id and an env variable name "
                    "(then --from pasted-<seq> binds a pasted token), or /secret mail the mail password",
    "mail_ask": "비밀 입력 · the app password for {user} (hidden; stored in the Keychain, never shown to the assistant)",
    "mail_stored": "저장됨 · mail password stored",
    "secret_ask": "비밀 입력 · {name} for {id} (hidden; stored in the Keychain, never shown to the assistant)",
    "secret_stored": "저장됨 · {name} stored for {id}; an installed server picks it up at the next "
                     "`pathfinder service restart`",
    "secret_cancelled": "저장 안 함 · nothing stored",
    "secret_one_line": "저장 안 함 · nothing stored: a secret is one line - enter only the value",
    "secret_failed": "저장 안 함 · nothing stored: {message}",
    "secret_handles": " - 또는 · or --from {handles}",
    "secret_sent": "보냄 · {what} went to the daemon, not the chat",
    "secret_stored_ask": "저장됨 · {name} stored for {id}; proposing it again",
    "secret_busy": "다른 입력 중 · another secret entry is open; after it - ",
    "secret_needed": "비밀 필요 · {what} is needed - {how} in the screen takes it{again}, never the chat",
    "pasted": "토큰을 붙여넣었습니다 · you pasted a token; it is stored as {handle} for 24 h - bind it with "
              "/secret <id> NAME --from {handle}",
}


def entry(rest, home, send):
    """`/secret`'s arguments: `ID NAME` opens the masked entry, `mail` alone the mail password's (U26),
    `ID NAME --from pasted-<seq>` binds a pasted item at once (D53) - the Entry (or None) and the line
    to show; anything else - the usage line. Every value goes to the daemon as `secret` (`send`),
    which stores it and proposes again an install that was waiting for it."""
    args = rest.split()
    if args == ["mail"]:  # alone, the mail password (U26); an MCP secret always names its variable
        try:
            _, user = keychain.target(home, "mail")
        except LookupError as e:  # the mail sense is not configured
            return None, STRINGS["secret_failed"].format(message=e)
        return (Entry(lambda value: send({"type": "secret", "server_id": "mail", "value": value}),
                      STRINGS["secret_sent"].format(what="the mail password")), _prompt(home, "mail_ask", user=user))
    if len(args) in (2, 4) and args[2:3] in ([], ["--from"]) and keychain.valid_target(*args[:2]):
        try:
            keychain.target(home, *args[:2])
        except ValueError as e:  # a NAME that looks like a token
            return None, STRINGS["secret_failed"].format(message=e)
        answer, sent = {"type": "secret", "server_id": args[0], "name": args[1]}, STRINGS["secret_sent"].format(
            what=f"{args[1]} for {args[0]}")
        if len(args) == 4:
            send({**answer, "value": f"--from {args[3]}"})
            return None, sent
        return Entry(lambda value: send({**answer, "value": value}), sent), _prompt(home, id=args[0], name=args[1])
    return None, STRINGS["secret_usage"]


def asked(msg, home, send):
    """D53's main path, `secret.ask`: the masked entry `/secret` opens, marked as this ask's, so an
    answer from another screen closes it."""
    id, name = msg.get("server_id"), msg.get("name")
    made, text = entry(f"{id} {name}" if id else "mail", home, send)
    if made:
        made.ask = (id, name)
    return made, text


def needed(msg):
    """`secret.ask` as a line: for a client with no masked input (`say`, `listen`), or a screen with
    an entry already open - it names the way in, which proposes the install again."""
    id, name = msg.get("server_id"), msg.get("name")
    return STRINGS["secret_needed"].format(what=f"{name} for {id}" if id else "the mail password",
                                           how=f"/secret {id} {name}" if id else "/secret mail",
                                           again=" and proposes the install again" if id else "")


def stored(msg):
    """The daemon's `secret.stored`, as the screen shows it."""
    if not msg.get("name"):
        return STRINGS["mail_stored"]
    key = "secret_stored_ask" if msg.get("server") else "secret_stored"
    return STRINGS[key].format(name=msg.get("name"), id=msg.get("server_id"))


def _prompt(home, key="secret_ask", **fields):
    """The masked prompt, with the live pasted handles `--from` binds there (D53)."""
    held = keychain.live(home)
    return STRINGS[key].format(**fields) + (STRINGS["secret_handles"].format(handles=", ".join(held)) if held else "")


class Entry:
    """One masked entry: `put` takes the value - it sends it to the daemon - and `done` is the line
    after it. `paste` is None outside a bracketed paste;
    inside one it is False, "\\n" after a newline, True with text after one (more than one line)."""

    def __init__(self, put, done, ask=None):
        self.put, self.done, self.ask, self.paste = put, done, ask, None  # `ask`: (id, NAME) of a harness ask

    def swallow(self, ch, in_esc) -> bool:
        """In a paste no control is a key: True drops `ch`. Text after a pasted newline marks the
        paste as more than one line."""
        if self.paste is not None and ch != "\x1b" and not ch.isprintable():
            self.paste = self.paste or ("\n" if ch in "\r\n" else False)
            return True
        if self.paste == "\n" and not in_esc and ch != "\x1b":
            self.paste = True
        return False

    def mark(self, seq) -> bool:
        """A finished escape: `[200~` opens a paste, `[201~` closes it. True when the paste that
        closed was more than one line - all of it goes, unseen, the value with it."""
        if seq not in ("[200~", "[201~"):
            return False
        void = seq == "[201~" and self.paste is True
        self.paste = False if seq == "[200~" else None
        return void

    def enter(self, value, in_fd, spill) -> str:
        """A secret is one line: Enter stores the value only when silence follows it. Input after it
        in the same read (`spill`), or readable within SECRET_DRAIN_S (a paste spread over reads), is
        read and dropped unseen, the value with it - nothing is stored. Returns the line to show;
        `put` raises with messages that never carry the value."""
        while select.select([in_fd], [], [], SECRET_DRAIN_S)[0] and os.read(in_fd, 1024):
            spill = True
        if spill:
            return STRINGS["secret_one_line"]
        self.put(value)
        return self.done
