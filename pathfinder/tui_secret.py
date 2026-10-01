"""The masked input of `/secret ID NAME` (D53, D64). The screen (`tui.py`) keeps the key loop,
the escape parser, the input buffer and the switch into and out of the masked mode, and calls in
here; an `Entry` holds what is the masked entry's own: its Keychain target and where a bracketed
paste stands. The value is stored by the screen's own process - it never goes on the socket, so
it never reaches the daemon, the ledger or the model."""
from __future__ import annotations

import os
import select

from . import keychain

SECRET_PROMPT = "비밀 · secret> "  # the masked input line: this, then one `*` per character
SECRET_DRAIN_S = 0.05           # input within this after a masked Enter is a paste that ran past the value

# Fixed strings, Korean and English side by side (D6), as in `tui.STRINGS`.
STRINGS = {
    "secret_usage": "사용법 · /secret ID NAME takes a server id and an env variable name",
    "secret_ask": "비밀 입력 · {name} for {id} (hidden; stored in the Keychain, never shown to the assistant)",
    "secret_stored": "저장됨 · {name} stored for {id}; an installed server picks it up at the next "
                     "`pathfinder service restart`",
    "secret_cancelled": "저장 안 함 · nothing stored",
    "secret_one_line": "저장 안 함 · nothing stored: a secret is one line - enter only the value",
    "secret_failed": "저장 안 함 · nothing stored: {message}",
    "pasted": "토큰을 붙여넣었습니다 · you pasted a token; it is stored as {handle} for 24 h - bind it with "
              "/secret <id> NAME --from {handle}",
}


def entry(rest, home):
    """`/secret`'s arguments: `ID NAME` opens the masked entry - the Entry and the line to show;
    `ID NAME --from pasted-<seq>` binds a pasted item at once (D53) - None and how it went; anything
    else - None and the usage line."""
    args = rest.split()
    if len(args) in (2, 4) and args[2:3] in ([], ["--from"]) and keychain.valid_target(*args[:2]):
        if len(args) == 2:
            return Entry(*args), STRINGS["secret_ask"].format(id=args[0], name=args[1])
        try:
            keychain.bind(home, args[3], f"pathfinder-mcp-{args[0]}", args[1])
            return None, STRINGS["secret_stored"].format(id=args[0], name=args[1])
        except Exception as e:  # bind's messages never carry the value
            return None, STRINGS["secret_failed"].format(message=e)
    return None, STRINGS["secret_usage"]


class Entry:
    """One masked entry for `pathfinder-mcp-<id>`/NAME. `paste` is None outside a bracketed paste;
    inside one it is False, "\\n" after a newline, True with text after one (more than one line)."""

    def __init__(self, id, name):
        self.id, self.name, self.paste = id, name, None

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
        `keychain.store` raises with messages that never carry the value."""
        while select.select([in_fd], [], [], SECRET_DRAIN_S)[0] and os.read(in_fd, 1024):
            spill = True
        if spill:
            return STRINGS["secret_one_line"]
        keychain.store(f"pathfinder-mcp-{self.id}", self.name, value)
        return STRINGS["secret_stored"].format(name=self.name, id=self.id)
