"""The masked input of `/secret ID NAME` (D53, D64). The screen (`tui.py`) keeps the key loop,
the escape parser, the input buffer and the switch into and out of the masked mode, and calls in
here; an `Entry` holds what is the masked entry's own: where its value goes and where a bracketed
paste stands. `/secret` stores the value in the screen's own process; a harness ask (`secret.ask`,
D53's main path) sends it to the daemon, which stores it. Neither reaches the ledger or the model."""
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
    "mail_stored": "저장됨 · the app password for {user} is in the Keychain",
    "secret_ask": "비밀 입력 · {name} for {id} (hidden; stored in the Keychain, never shown to the assistant)",
    "secret_stored": "저장됨 · {name} stored for {id}; an installed server picks it up at the next "
                     "`pathfinder service restart`",
    "secret_cancelled": "저장 안 함 · nothing stored",
    "secret_one_line": "저장 안 함 · nothing stored: a secret is one line - enter only the value",
    "secret_failed": "저장 안 함 · nothing stored: {message}",
    "secret_handles": " - 또는 · or --from {handles}",
    "secret_sent": "보냄 · {name} for {id} went to the daemon, not the chat",
    "secret_stored_ask": "저장됨 · {name} stored for {id}; proposing it again",
    "secret_busy": "건너뜀 · another secret entry is open, so this ask is dropped - answer it later with /secret",
    "secret_needed": "비밀 필요 · {what} is needed - enter it with {how} in the screen, never in the chat",
    "pasted": "토큰을 붙여넣었습니다 · you pasted a token; it is stored as {handle} for 24 h - bind it with "
              "/secret <id> NAME --from {handle}",
}


def entry(rest, home):
    """`/secret`'s arguments: `ID NAME` opens the masked entry - the Entry and the line to show;
    `ID NAME --from pasted-<seq>` binds a pasted item at once (D53) - None and how it went; anything
    else - None and the usage line."""
    args = rest.split()
    if args == ["mail"]:  # alone, the mail password (U26); an MCP secret always names its variable
        try:
            service, user = keychain.target(home, "mail")
        except LookupError as e:  # the mail sense is not configured
            return None, STRINGS["secret_failed"].format(message=e)
        return (Entry(lambda value: keychain.take(home, service, user, value), STRINGS["mail_stored"].format(user=user)),
                _prompt(home, "mail_ask", user=user))
    if len(args) in (2, 4) and args[2:3] in ([], ["--from"]) and keychain.valid_target(*args[:2]):
        try:
            service, account = keychain.target(home, *args[:2])
        except ValueError as e:  # a NAME that looks like a token
            return None, STRINGS["secret_failed"].format(message=e)
        stored = STRINGS["secret_stored"].format(id=args[0], name=args[1])
        if len(args) == 2:
            return (Entry(lambda value: keychain.take(home, service, account, value), stored),
                    _prompt(home, id=args[0], name=args[1]))
        try:
            keychain.bind(home, args[3], service, account)
            return None, stored
        except Exception as e:  # bind's messages never carry the value
            return None, STRINGS["secret_failed"].format(message=e)
    return None, STRINGS["secret_usage"]


def asked(msg, home, send):
    """D53's main path, `secret.ask`: the harness asks for an MCP server's variable. The same masked
    input, but Enter sends the value to the daemon, which stores it and proposes the server again."""
    id, name = msg.get("server_id"), msg.get("name")
    if not id:  # the mail password, asked as the screen attaches (U26): `/secret mail`, stored here
        return entry("mail", home)
    answer = {"type": "secret", "server_id": id, "name": name, "server": msg.get("server")}
    return (Entry(lambda value: send({**answer, "value": value}), STRINGS["secret_sent"].format(id=id, name=name),
                  ask=(id, name)), _prompt(home, id=id, name=name))


def needed(msg):
    """`secret.ask` for a client with no masked input (`say`, `listen`): the line that says how."""
    id, name = msg.get("server_id"), msg.get("name")
    return STRINGS["secret_needed"].format(what=f"{name} for {id}" if id else "the mail password",
                                           how=f"/secret {id} {name}" if id else "/secret mail")


def _prompt(home, key="secret_ask", **fields):
    """The masked prompt, with the live pasted handles `--from` binds there (D53)."""
    held = keychain.live(home)
    return STRINGS[key].format(**fields) + (STRINGS["secret_handles"].format(handles=", ".join(held)) if held else "")


class Entry:
    """One masked entry: `put` takes the value - stored in this process, or sent to the daemon for
    a harness ask - and `done` is the line after it. `paste` is None outside a bracketed paste;
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
