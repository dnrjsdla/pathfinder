"""`pathfinder tui`: one attached client for the whole sitting (D35). Connects, sends
`attach {cwd}`, puts the terminal in cbreak mode, and multiplexes stdin and the socket with
`selectors`. The screen is the terminal's own scrollback - no curses, no panes: server text
prints above, one input line is redrawn at the bottom (`\r\033[K` + prompt + buffer) after
anything prints, so a notice never tears what the user is typing. This is the only real
producer of `presence` - the deliver state machine's "typing"/"away" branches have had no live
source since partition 3."""
from __future__ import annotations

import codecs
import contextlib
import io
import json
import os
import re
import selectors
import socket
import sys
import time
import tty
import termios
from pathlib import Path

from . import tui_secret
from .daemon import SOCKET
from .mcp.protocol import sanitize
from .tools import visible

PROMPT = "> "
_UNIT = re.compile(r"\\U[0-9a-f]{8}|\\u[0-9a-f]{4}|\\.|.")  # an escape, JSON's or `visible`'s, is one unit
SELECT_TIMEOUT = 0.05           # how often the main loop wakes up to check the timers below
PRESENCE_ACTIVE_S = 1.0         # at most one presence{idle_s: 0} per second while keys arrive
PRESENCE_IDLE_S = 30.0          # a presence{idle_s: n} every 30s of silence
NOTICE_HOLD_S = 1.0             # a notice waits until keys have been silent this long
NOTICE_DIM = "\033[2m"          # the one colour this screen uses
RESET = "\033[0m"

# Fixed strings, Korean and English side by side (D6) - no language setting.
STRINGS = {
    "no_daemon": "데몬이 실행되지 않음 · the daemon is not running (start it with: pathfinder daemon)",
    "dropped": "연결 끊김 · connection to the daemon was lost",
    "gate": "승인 대기 · waiting for approval on {tool} {args} - /approve {id} or /deny {id}",
    "job_gate": "작업 승인 대기 · job {id} waiting for approval on {tool} {args} "
                "- /approve {approval} or /deny {approval}",
    "job_start": "작업 시작 · job {id} started: {goal}",
    "job_end": "작업 종료 · job {id} {state}",
    "error": "오류 · error: {message}",
    "tool_ok": "도구 완료 · tool {name} done",
    "tool_fail": "도구 실패 · tool {name} failed",
    "notice": "알림 · notice: {text} ({why})",
    "retract": "철회 · withdrawn: the answer above {what} without calling a tool - trying again with the tools",
    "help": "알 수 없는 명령 · unknown command - try /approve, /deny, /approvals, /jobs, /secret, /quit",
    "no_approvals": "대기 중인 승인 없음 · no approvals seen this sitting",
    "no_jobs": "확인된 작업 없음 · no jobs seen this sitting",
}


def _gate_args(args) -> str:
    """5d: the args as shown, cut at the first unit that would pass 300 shown characters - after
    `visible`, so the count is what the owner sees, and never inside an escape such as `\\u200b`."""
    shown = visible(json.dumps(args, ensure_ascii=False))
    cross = next((m for m in _UNIT.finditer(shown) if m.end() > 300), None)
    return shown if cross is None else shown[:cross.start()] + "...(clipped; /approvals)"


def render(msg: dict) -> str | None:
    """The one renderer for every server message kind (shared with `__main__.listen`)."""
    kind = msg.get("type")
    if kind in ("attached", "turn.start"):
        return None
    if kind == "delta":
        return msg.get("text", "")
    if kind == "turn.end":
        return "\n"
    if kind == "tool":
        key = "tool_ok" if msg.get("ok") else "tool_fail"
        return STRINGS[key].format(name=msg.get("name"))
    if kind == "gate":  # what is shown is what runs: a hidden character is escaped, never dropped
        return visible(STRINGS["gate"].format(id=msg.get("id"), tool=msg.get("tool"),
                                               args=_gate_args(msg.get("args"))))
    if kind == "retract":  # D51: harness words only - the verifier's `why` never reaches the screen
        return NOTICE_DIM + STRINGS["retract"].format(what=msg.get("what")) + RESET
    if kind == "notice":
        text = sanitize(msg.get("text") or "")
        return NOTICE_DIM + STRINGS["notice"].format(text=text, why=msg.get("why")) + RESET
    if kind == "job.start":
        return STRINGS["job_start"].format(id=msg.get("id"), goal=sanitize(msg.get("goal") or ""))
    if kind == "job.gate":
        return visible(STRINGS["job_gate"].format(id=msg.get("id"), approval=msg.get("approval"),
                                                   tool=msg.get("tool"), args=_gate_args(msg.get("args"))))
    if kind == "job.end":
        return STRINGS["job_end"].format(id=msg.get("id"), state=msg.get("state"))
    if kind == "error":
        return STRINGS["error"].format(message=msg.get("message"))
    if kind == "secret.ask":  # D53, for a client with no masked input (`say`, `listen`)
        return tui_secret.needed(msg)
    if kind == "secret.stored":  # D53: the daemon stored an asked-for value; the proposal follows
        return tui_secret.STRINGS["secret_stored_ask"].format(name=msg.get("name"), id=msg.get("server_id"))
    if kind == "pasted":  # D53: a token in the owner's own line went to the Keychain under a handle
        return "\n".join(tui_secret.STRINGS["pasted"].format(handle=h) for h in msg.get("handles") or [])
    return None


class _Session:
    """One attached sitting: owns the input buffer, the presence clock and the notice hold."""

    def __init__(self, sock, in_fd, out, home):
        self.sock, self.in_fd, self.out, self.home = sock, in_fd, out, home
        self.buf = ""
        self._typed = ""  # the line being typed, put aside while a masked entry is open
        self.secret = None  # a `tui_secret.Entry` while the input line is masked for `/secret`
        self.turn_running = False
        self.interrupt_sent = False  # one Ctrl-C interrupts, the second quits
        self._esc = 0  # 1 after ESC, 2 inside ESC [ (CSI), 3 after ESC O (SS3): such keys are dropped
        self._seq = ""  # the escape's bytes
        self._recv_buf = b""
        self._keys = codecs.getincrementaldecoder("utf-8")("replace")  # a Hangul syllable is 3 bytes (D6)
        now = time.monotonic()
        self.last_key = now
        self.last_active_presence = 0.0
        self.last_idle_presence = now
        self.pending_notices = []
        self.sel = selectors.DefaultSelector()

    def send(self, msg):
        self.sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode())

    def _masked(self, target):
        """Into (a `tui_secret.Entry`) or out of (None) the masked input. Bracketed paste is on only
        while masked, so a paste comes between marks however slow the link; main() turns it off on exit too."""
        self._typed, self.buf = (self.buf, "") if target else ("", self._typed)  # the typed line comes back
        self.secret = target
        self.out.write("\033[?2004h" if target else "\033[?2004l")

    def redraw(self):
        self.out.write("\r\033[K" + (tui_secret.SECRET_PROMPT + "*" * len(self.buf) if self.secret else PROMPT + self.buf))
        self.out.flush()

    def show(self, text):
        """Print server text above the input line, then redraw the input line below it. During a
        stream there is no input line on screen: the text takes a line of its own and the stream
        goes on below it (a `\\r\\033[K` here would erase the streamed line)."""
        self.out.write("\n" if self.turn_running else "\r\033[K")
        self.out.write(text)
        if not text.endswith("\n"):
            self.out.write("\n")
        if not self.turn_running:
            self.redraw()
        self.out.flush()

    def run(self):
        self.send({"type": "attach", "cwd": os.getcwd()})
        self.sel.register(self.in_fd, selectors.EVENT_READ, "keys")
        self.sel.register(self.sock, selectors.EVENT_READ, "sock")
        self.redraw()
        try:
            while True:
                rc = self._tick()
                if rc is not None:
                    return rc
        finally:
            self.sel.close()

    def _tick(self):
        try:
            events = self.sel.select(timeout=SELECT_TIMEOUT)
        except KeyboardInterrupt:
            # ISIG is off (see main()), so Ctrl-C at the keyboard arrives as the byte 0x03 below;
            # this only catches SIGINT sent some other way (e.g. `kill`).
            return self._ctrl_c()
        for key, _mask in events:
            if key.data == "keys":
                rc = self._read_keys()
            else:
                rc = self._read_sock()
            if rc is not None:
                return rc
        self._check_timers()
        return None

    def _read_keys(self):
        try:
            data = os.read(self.in_fd, 1024)
        except KeyboardInterrupt:
            return self._ctrl_c()
        if not data:  # stdin closed
            return 0
        chars = self._keys.decode(data)
        for i, ch in enumerate(chars):
            if self.secret and self.secret.swallow(ch, self._esc):  # in a paste no control is a key
                continue
            if self.secret and ch in "\r\n":
                return self._secret_enter(spill=i + 1 < len(chars))
            rc = self._key(ord(ch))
            if rc is not None:
                return rc
        return None

    def _secret_enter(self, spill):
        try:  # stored in this process, or sent to the daemon for a harness ask (`tui_secret.Entry.enter`)
            text = self.secret.enter(self.buf, self.in_fd, spill)
        except Exception as e:  # store's messages never carry the value
            text = STRINGS["error"].format(message=e)
        self._masked(None)
        self._esc = 0
        self._keys.reset()
        self.show(text)
        return None

    def _ctrl_c(self):
        if self.secret:  # cancels the masked entry and nothing else
            self._masked(None)
            self.show(tui_secret.STRINGS["secret_cancelled"])
            return None
        if self.turn_running and not self.interrupt_sent:
            self.send({"type": "interrupt"})
            self.interrupt_sent = True
            return None
        return 0  # no turn, or the second Ctrl-C while a tool refuses to be cut: quit

    def _key(self, byte):
        if byte == 0x03:  # Ctrl-C - ISIG is off, so this is how it always arrives
            return self._ctrl_c()
        if byte == 0x04:  # Ctrl-D
            return 0
        if byte == 0x1b or self._esc:  # ESC [ ... final, ESC O x: arrow and function keys, dropped whole
            self._seq = "" if byte == 0x1b else self._seq + chr(byte)
            self._esc = (1 if byte == 0x1b else {0x5b: 2, 0x4f: 3}.get(byte, 0) if self._esc == 1 else
                         2 if self._esc == 2 and not 0x40 <= byte <= 0x7e else 0)
            if self.secret and not self._esc and self.secret.mark(self._seq):  # a paste of more than one line
                self._masked(None)
                self.show(tui_secret.STRINGS["secret_one_line"])
            return None
        now = time.monotonic()
        self.last_key = now
        if now - self.last_active_presence >= PRESENCE_ACTIVE_S:
            self.send({"type": "presence", "idle_s": 0})
            self.last_active_presence = now
            self.last_idle_presence = now
        rc = None
        if byte in (0x7f, 0x08):  # Backspace
            self.buf = self.buf[:-1]
        elif byte in (0x0d, 0x0a):  # Enter
            line, self.buf = self.buf, ""
            rc = self._line(line)
        elif chr(byte).isprintable():  # any script, not just ASCII (D6)
            self.buf += chr(byte)
        if rc is None and not self.turn_running:  # the buffer comes back on screen at turn.end
            self.redraw()
        return rc

    def _line(self, line):
        if line == "+":
            self.send({"type": "react", "vote": "up"})
        elif line == "-":
            self.send({"type": "react", "vote": "down"})
        elif line.startswith("/"):
            return self._slash(line)
        elif line.strip():
            self.send({"type": "say", "text": line})
        return None

    def _slash(self, line):
        parts = line[1:].split(None, 1)
        cmd = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "approve" and rest:
            self.send({"type": "approve", "id": rest})
        elif cmd == "deny" and rest:
            self.send({"type": "deny", "id": rest})
        elif cmd in ("approvals", "jobs"):
            self._cli_list(cmd)
        elif cmd == "secret":
            entry, text = tui_secret.entry(rest, self.home)
            if entry:
                self._masked(entry)
            self.show(text)
        elif cmd == "quit":
            return 0
        else:
            self.show(STRINGS["help"])
        return None

    def _cli_list(self, cmd):
        from . import __main__ as cli  # lazy: __main__ imports this module
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            (cli.approvals if cmd == "approvals" else cli.jobs_list)(self.home)
        lines = buf.getvalue().splitlines()
        for line in lines or [STRINGS["no_approvals" if cmd == "approvals" else "no_jobs"]]:
            self.show(line)

    def _read_sock(self):
        try:
            chunk = self.sock.recv(4096)
        except OSError:  # reset by a daemon that died: the same one line as EOF
            chunk = b""
        if not chunk:
            self.show(STRINGS["dropped"])
            return 1
        self._recv_buf += chunk
        while b"\n" in self._recv_buf:
            line, self._recv_buf = self._recv_buf.split(b"\n", 1)
            if line.strip():
                self._deliver(json.loads(line))
        return None

    def _deliver(self, msg):
        kind = msg.get("type")
        if kind == "turn.start":
            self.turn_running, self.interrupt_sent = True, False
            self.out.write("\r\033[K")  # the stream takes the line; the buffer comes back at turn.end
            self.out.flush()
            return
        if kind == "delta":  # raw, no newline, no redraw - a stream is one paragraph
            self.out.write(msg.get("text", ""))
            self.out.flush()
            return
        if kind == "turn.end":
            self.turn_running = False
            self.out.write("\n")
            self.redraw()
            return
        if kind == "secret.ask":  # D53: the harness asks; the value is typed masked, never into the chat
            if self.secret:  # never in place of an open entry, whose value would go to the wrong name
                return self.show(tui_secret.STRINGS["secret_busy"])
            entry, text = tui_secret.asked(msg, self.home, self.send)
            if entry:
                self._masked(entry)
            self.show(text)
            return
        if kind == "secret.stored" and self.secret and self.secret.ask == (msg.get("server_id"), msg.get("name")):
            self._masked(None)  # another screen answered this ask
        text = render(msg)
        if text is None:
            return
        if kind == "notice" and time.monotonic() - self.last_key < NOTICE_HOLD_S:
            self.pending_notices.append(text)  # hands are still on the keyboard - hold it
            return
        self.show(text)

    def _check_timers(self):
        now = time.monotonic()
        if self.pending_notices and now - self.last_key >= NOTICE_HOLD_S:
            held, self.pending_notices = self.pending_notices, []
            for text in held:
                self.show(text)
        idle = now - self.last_key
        if idle >= PRESENCE_IDLE_S and now - self.last_idle_presence >= PRESENCE_IDLE_S:
            self.send({"type": "presence", "idle_s": min(idle, 86400)})  # the daemon's ceiling
            self.last_idle_presence = now


def main(home, *, sock_path=None, in_fd=None, out_fd=None) -> int:
    home = Path(home)
    sock_path = Path(sock_path) if sock_path is not None else home / SOCKET
    in_fd = sys.stdin.fileno() if in_fd is None else in_fd
    out_fd = sys.stdout.fileno() if out_fd is None else out_fd
    out = os.fdopen(out_fd, "w", encoding="utf-8", closefd=False)

    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.connect(str(sock_path))
    except OSError:
        out.write(STRINGS["no_daemon"] + "\n")
        out.flush()
        return 1

    old_attrs = termios.tcgetattr(in_fd)
    tty.setcbreak(in_fd)
    # cbreak leaves ISIG on, which makes the tty driver swallow Ctrl-C (and Ctrl-\) before it
    # ever reaches read() - the line discipline consumes the byte to raise a signal even when
    # there is no controlling-terminal relationship to deliver it to. We handle Ctrl-C ourselves
    # (interrupt the running turn, or quit) so turn ISIG off and let the byte through instead.
    # The cost: Ctrl-Z and Ctrl-\\ do nothing while the screen is attached.
    attrs = termios.tcgetattr(in_fd)
    attrs[3] &= ~termios.ISIG
    termios.tcsetattr(in_fd, termios.TCSANOW, attrs)
    try:
        with sock:
            return _Session(sock, in_fd, out, home).run()
    finally:
        termios.tcsetattr(in_fd, termios.TCSADRAIN, old_attrs)
        out.write("\033[?2004l")  # the terminal is never left in bracketed paste, however the screen ends
        out.flush()
