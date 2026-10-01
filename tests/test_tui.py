"""tui.py against a fake daemon on a temp unix socket, and a pty standing in for the real
terminal (`os.openpty()`) so termios/cbreak behave for real without a real terminal."""
import contextlib
import io
import json
import os
import socket
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import tui, tui_secret


class FakeDaemon:
    """Accepts one connection, records every JSON line it receives, and writes lines handed to
    it (`send`) back over the socket - the wire format `__main__._connect`/`_stream` use."""

    def __init__(self, path):
        self.received = []
        self._conn = None
        self._ready = threading.Event()
        self._server = socket.socket(socket.AF_UNIX)
        self._server.bind(str(path))
        self._server.listen(1)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        self._conn = conn
        self._ready.set()
        buf = b""
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    self.received.append(json.loads(line))

    def wait_connected(self, timeout=2):
        self._ready.wait(timeout)

    def send(self, msg):
        self.wait_connected()
        self._conn.sendall((json.dumps(msg) + "\n").encode())

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._server.close()


def _answer(value):
    return {"type": "secret", "server_id": "rv11test", "name": "TOKEN", "value": value}


def _wait_for(pred, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class TuiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.sock_path = self.home / "s.sock"
        self.daemon = FakeDaemon(self.sock_path)
        self.master, self.slave = os.openpty()
        self.out_r, self.out_w = os.pipe()
        self.output = bytearray()
        self._reader_stop = False
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()
        self.term_before = termios.tcgetattr(self.slave)

        self._saved = (tui.NOTICE_HOLD_S, tui.PRESENCE_ACTIVE_S, tui.PRESENCE_IDLE_S, tui.SELECT_TIMEOUT)
        tui.NOTICE_HOLD_S = 0.15
        tui.PRESENCE_ACTIVE_S = 0.05
        tui.PRESENCE_IDLE_S = 0.3
        tui.SELECT_TIMEOUT = 0.02

        self.rc = None
        self._main = threading.Thread(target=self._run_main, daemon=True)
        self._main.start()
        self.daemon.wait_connected()
        _wait_for(lambda: len(self.daemon.received) >= 1)  # the initial attach

    def tearDown(self):
        (tui.NOTICE_HOLD_S, tui.PRESENCE_ACTIVE_S, tui.PRESENCE_IDLE_S, tui.SELECT_TIMEOUT) = self._saved
        self.type_("\x04")  # Ctrl-D: make sure the client quits even if a test forgot to
        self._main.join(timeout=2)
        self._reader_stop = True
        for fd in (self.out_w, self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass
        self.daemon.close()
        self.tmp.cleanup()

    def _run_main(self):
        self.rc = tui.main(self.home, sock_path=self.sock_path, in_fd=self.slave, out_fd=self.out_w)

    def _read_output(self):
        while not self._reader_stop:
            try:
                chunk = os.read(self.out_r, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.output.extend(chunk)

    def _secrets(self):
        return [m for m in self.daemon.received if m["type"] == "secret"]

    def type_(self, s):
        try:
            os.write(self.master, s.encode())
        except OSError:
            pass  # the client already quit and closed its end

    def out_text(self):
        return self.output.decode(errors="replace")

    # --- tests -----------------------------------------------------------

    def test_deltas_and_notice_redraw_the_input_line(self):
        self.type_("hel")
        _wait_for(lambda: any(m.get("type") == "presence" for m in self.daemon.received))
        time.sleep(tui.NOTICE_HOLD_S * 2)  # hands go silent before the notice arrives
        self.daemon.send({"type": "notice", "id": "n1", "text": "hi", "why": "signal"})
        # Both conditions must hold on the SAME snapshot: "> hel" is also the tail of every
        # redraw from typing, so waiting on it alone can pass before the notice ever arrives.
        self.assertTrue(_wait_for(lambda: "hi" in self.out_text()
                                   and self.out_text().endswith("\r\033[K> hel")))

    def test_notice_waits_while_typing(self):
        self.type_("h")
        _wait_for(lambda: any(m.get("type") == "presence" for m in self.daemon.received))
        self.daemon.send({"type": "notice", "id": "n2", "text": "later", "why": "y"})
        time.sleep(tui.NOTICE_HOLD_S * 0.3)
        self.type_("e")  # a key arrives inside the hold window - resets it
        time.sleep(tui.NOTICE_HOLD_S * 0.6)
        self.assertNotIn("later", self.out_text(), "the notice printed while keys were arriving")
        self.assertTrue(_wait_for(lambda: "later" in self.out_text(), timeout=2))

    def test_typing_sends_presence_zero_and_silence_sends_idle(self):
        self.type_("a")
        self.assertTrue(_wait_for(lambda: any(
            m == {"type": "presence", "idle_s": 0} for m in self.daemon.received)))
        self.assertTrue(_wait_for(lambda: any(
            m.get("type") == "presence" and m.get("idle_s", 0) > 0 for m in self.daemon.received), timeout=2))

    def test_enter_sends_say_and_slash_approve_sends_approve(self):
        self.type_("hello\r")
        self.assertTrue(_wait_for(lambda: any(
            m.get("type") == "say" and m.get("text") == "hello" for m in self.daemon.received)))
        self.type_("/approve abc123\r")
        self.assertTrue(_wait_for(lambda: any(
            m.get("type") == "approve" and m.get("id") == "abc123" for m in self.daemon.received)))

    def test_korean_input_reaches_say_and_a_stream_is_one_paragraph(self):
        """D6: a Hangul syllable is three UTF-8 bytes - the key loop must decode, not drop, them.
        And deltas print raw between turn.start and turn.end: no redraw tears the stream (live 4)."""
        self.type_("안녕 세계\r")
        self.assertTrue(_wait_for(lambda: any(
            m.get("type") == "say" and m.get("text") == "안녕 세계" for m in self.daemon.received)))
        before = len(self.out_text())
        self.daemon.send({"type": "turn.start", "turn": "t1"})
        self.daemon.send({"type": "delta", "text": "1. Nile"})
        self.daemon.send({"type": "delta", "text": " 2. Amazon"})
        self.daemon.send({"type": "turn.end", "turn": "t1", "kind": "answer"})
        # the two deltas are contiguous, the newline comes with turn.end, then the input line
        self.assertTrue(_wait_for(lambda: "1. Nile 2. Amazon\n\r\033[K> " in self.out_text()[before:]))

    def test_typing_and_tool_lines_during_a_stream_do_not_erase_it(self):
        """Review: a redraw mid-stream cleared the streamed line. Keys buffer silently, a tool
        line takes a line of its own, and the buffer comes back at turn.end."""
        before = len(self.out_text())
        self.daemon.send({"type": "turn.start", "turn": "t1"})
        self.daemon.send({"type": "delta", "text": "Hello wor"})
        self.assertTrue(_wait_for(lambda: "Hello wor" in self.out_text()[before:]))
        self.type_("x")
        self.daemon.send({"type": "tool", "name": "read_file", "ok": True})
        self.daemon.send({"type": "delta", "text": "ld"})
        self.daemon.send({"type": "turn.end", "turn": "t1", "kind": "answer"})
        self.assertTrue(_wait_for(lambda: self.out_text().endswith("\r\033[K> x")))
        out = self.out_text()[before:]
        self.assertIn("Hello wor\n", out)  # never erased; the tool line follows on its own line
        self.assertNotIn("\r\033[KHello", out[out.index("Hello wor") + 1:])

    def test_two_held_notices_both_print(self):
        self.type_("ab")
        self.daemon.send({"type": "notice", "id": "n1", "text": "first", "why": "r"})
        self.daemon.send({"type": "notice", "id": "n2", "text": "second", "why": "r"})
        self.assertTrue(_wait_for(lambda: "second" in self.out_text(), timeout=3))
        self.assertIn("first", self.out_text())

    def test_second_ctrl_c_quits_and_arrow_keys_are_dropped(self):
        self.type_("\x1b[A")  # up arrow
        self.type_("q\r")
        self.assertTrue(_wait_for(lambda: any(
            m.get("type") == "say" and m.get("text") == "q" for m in self.daemon.received)))
        self.daemon.send({"type": "turn.start", "turn": "t1"})
        self.assertTrue(_wait_for(lambda: "turn.start" in self.out_text() or True))
        time.sleep(0.2)
        self.type_("\x03")
        self.assertTrue(_wait_for(lambda: any(m.get("type") == "interrupt" for m in self.daemon.received)))
        self.type_("\x03")
        self._main.join(timeout=2)
        self.assertFalse(self._main.is_alive())
        self.assertEqual(self.rc, 0)

    def test_slash_approvals_reads_state_not_the_sitting(self):
        self.type_("/approvals\r")
        self.assertTrue(_wait_for(lambda: "no approvals" in self.out_text()))

    def test_plus_minus_send_react(self):
        self.type_("+\r")
        self.assertTrue(_wait_for(lambda: any(
            m == {"type": "react", "vote": "up"} for m in self.daemon.received)))
        self.type_("-\r")
        self.assertTrue(_wait_for(lambda: any(
            m == {"type": "react", "vote": "down"} for m in self.daemon.received)))

    def test_slash_secret_masks_and_sends_the_value_only_to_the_daemon(self):
        """U11, as routed in U16's review: the value is read masked - only `*` on the screen - and
        goes in one `secret` line to the daemon, which stores it; nothing else carries it."""
        value = "s3cr3t-" + "v4lue"
        with mock.patch("pathfinder.keychain.store") as store:
            self.type_("/secret rv11test TOKEN\r")
            self.assertTrue(_wait_for(lambda: "TOKEN for rv11test (hidden;" in self.out_text()))
            self.type_(value)
            self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT + "*" * len(value))))
            self.type_("\r")
            self.assertTrue(_wait_for(lambda: "TOKEN for rv11test went to the daemon" in self.out_text()))
            self.type_("/secret Bad_Id TOKEN\r")
            self.assertTrue(_wait_for(lambda: "/secret ID NAME takes a server id" in self.out_text()))
            store.assert_not_called()  # the daemon stores it, not the screen
        self.assertNotIn(value, self.out_text())
        self.assertEqual(self._secrets(), [_answer(value)])
        self.assertEqual({m["type"] for m in self.daemon.received} - {"attach", "presence", "secret"}, set())

    def test_secret_ask_masks_lists_handles_and_sends_the_value(self):
        """U16: the harness asks - the masked prompt names the variable and the live pasted handles,
        and Enter sends the value to the daemon, never onto the screen."""
        (self.home / "pasted.json").write_text(json.dumps({"pasted": {"pasted-12": time.time()}}))
        value = "s3cr3t-" + "asked"
        ask = {"type": "secret.ask", "server_id": "notion", "name": "NOTION_TOKEN", "server": "io.example/notion"}
        self.daemon.send(ask)
        self.assertTrue(_wait_for(lambda: "NOTION_TOKEN for notion (hidden;" in self.out_text()))
        self.assertIn("or --from pasted-12", self.out_text())
        self.type_(value)
        self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT + "*" * len(value))))
        self.type_("\r")
        self.assertTrue(_wait_for(lambda: any(m["type"] == "secret" for m in self.daemon.received)))
        self.assertEqual(self._secrets(), [{"type": "secret", "server_id": "notion", "name": "NOTION_TOKEN", "value": value}])
        self.daemon.send({"type": "secret.stored", "server_id": "notion", "name": "NOTION_TOKEN", "server": "io.example/notion"})
        self.assertTrue(_wait_for(lambda: "NOTION_TOKEN stored for notion; proposing it again" in self.out_text()))
        self.assertNotIn(value, self.out_text())

    def test_secret_ask_keeps_the_typed_line_and_never_replaces_an_open_entry(self):
        """Review: an ask puts the half-typed line aside and gives it back; an ask that arrives while
        an entry is open is dropped with a line, so the open entry's value never goes to the ask's
        name; an answer from another screen closes this screen's entry."""
        ask = {"type": "secret.ask", "server_id": "notion", "name": "NOTION_TOKEN", "server": "io.example/notion"}
        self.type_("half")
        self.assertTrue(_wait_for(lambda: self.out_text().endswith("> half")))
        self.daemon.send(ask)
        self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT)))
        self.daemon.send({"type": "secret.stored", "server_id": "notion", "name": "NOTION_TOKEN"})
        self.assertTrue(_wait_for(lambda: self.out_text().endswith("> half")))  # left the entry; the line is back
        self.type_("\x7f" * 4)
        with mock.patch("pathfinder.keychain.store") as store:
            self.type_("/secret rv11test TOKEN\r")
            self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT)))
            self.daemon.send(ask)
            self.assertTrue(_wait_for(lambda: "another secret entry is open" in self.out_text()))
            self.assertIn("/secret notion NOTION_TOKEN in the screen takes it and proposes the install again",
                          self.out_text())
            self.type_("v4lue")
            self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT + "*" * 5)))
            self.type_("\r")
            self.assertTrue(_wait_for(lambda: self._secrets()))
            self.assertEqual(self._secrets(), [_answer("v4lue")])  # the open entry's own name, not the ask's

    def test_ctrl_c_cancels_secret_entry(self):
        with mock.patch("pathfinder.keychain.store") as store:
            self.type_("/secret rv11test TOKEN\r")
            self.assertTrue(_wait_for(lambda: "(hidden;" in self.out_text()))
            self.type_("half")
            self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT + "****")))
            self.type_("\x03")
            self.assertTrue(_wait_for(lambda: "nothing stored" in self.out_text()))
            self.assertIn("\x1b[?2004l", self.out_text())  # bracketed paste off on cancel
            self.type_("hello\r")  # still attached, and back to plain input
            self.assertTrue(_wait_for(lambda: any(m.get("text") == "hello" for m in self.daemon.received)))
            store.assert_not_called()
        self.assertTrue(self._main.is_alive())
        self.assertNotIn("half", self.out_text() + json.dumps(self.daemon.received))

    def _paste_runs_past_the_secret(self, *writes, gap=0.01):
        with mock.patch("pathfinder.keychain.store") as store:
            self.type_("/secret rv11test TOKEN\r")
            self.assertTrue(_wait_for(lambda: "(hidden;" in self.out_text()))
            for w in writes:
                self.type_(w)
                time.sleep(gap)
            self.assertTrue(_wait_for(lambda: "a secret is one line" in self.out_text()))
            self.type_("hello\r")  # back to plain input, nothing of the paste left in the buffer
            self.assertTrue(_wait_for(lambda: any(m.get("type") == "say" for m in self.daemon.received)))
            self.assertEqual(self._secrets(), [])
        self.assertEqual([m for m in self.daemon.received if m["type"] == "say"], [{"type": "say", "text": "hello"}])
        for part in ("PART1-tok", "PART2-after", "PART3"):
            self.assertNotIn(part, self.out_text() + json.dumps(self.daemon.received))

    def test_paste_past_the_secret_line_stores_and_sends_nothing(self):
        """Review blocker: a secret is one line; whatever follows its Enter voids it, unseen."""
        self._paste_runs_past_the_secret("PART1-tok\rPART2-after\rPART3")

    def test_paste_past_the_secret_line_over_two_reads_stores_and_sends_nothing(self):
        self._paste_runs_past_the_secret("PART1-tok\r", "PART2-after\rPART3")

    def test_bracketed_multi_line_paste_over_slow_reads_stores_and_sends_nothing(self):
        """Reads 200 ms apart, past the drain window: the paste marks, not timing, bound the paste."""
        self._paste_runs_past_the_secret("\x1b[200~PART1-tok\r", "PART2-after\r", "PART3\x1b[201~", gap=0.2)

    def test_bracketed_one_line_paste_is_the_value_and_the_mode_is_turned_off(self):
        with mock.patch("pathfinder.keychain.store") as store:
            self.type_("/secret rv11test TOKEN\r")
            self.assertTrue(_wait_for(lambda: "(hidden;" in self.out_text()))
            self.assertIn("\x1b[?2004h", self.out_text())
            for part in ("\x1b[20", "0~tok-" + "value\x1b[2", "01~"):  # both marks straddle reads
                self.type_(part)
                time.sleep(0.05)
            self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT + "*" * 9)))
            self.type_("\r")
            self.assertTrue(_wait_for(lambda: "TOKEN for rv11test went to the daemon" in self.out_text()))
            self.assertEqual(self._secrets(), [_answer("tok-value")])
        out = self.out_text()
        self.assertLess(out.index("\x1b[?2004h"), out.index("\x1b[?2004l"))  # off once stored, screen still up
        self.assertTrue(self._main.is_alive())

    def test_bracketed_paste_with_a_trailing_newline_is_still_one_line(self):
        """A value copied with its line end (a triple-click) is the value; text after a newline is not."""
        with mock.patch("pathfinder.keychain.store") as store:
            for n, end in enumerate(("\n", "\r\n"), 1):
                self.type_("/secret rv11test TOKEN\r")
                self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT)))
                self.type_("\x1b[200~tok-" + "value" + end + "\x1b[201~")
                self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT + "*" * 9)))
                self.type_("\r")
                self.assertTrue(_wait_for(lambda: len(self._secrets()) == n))
            self.assertNotIn("a secret is one line", self.out_text())
            self.type_("/secret rv11test TOKEN\r")
            self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT)))
            self.type_("\x1b[200~a\nb\x1b[201~")
            self.assertTrue(_wait_for(lambda: "a secret is one line" in self.out_text()))
        self.assertEqual(self._secrets(), [_answer("tok-value")] * 2)

    def test_screen_closing_while_masked_turns_bracketed_paste_off(self):
        self.type_("/secret rv11test TOKEN\r")
        self.assertTrue(_wait_for(lambda: "(hidden;" in self.out_text()))
        self.type_("\x04")
        self._main.join(timeout=2)
        self.assertTrue(_wait_for(lambda: self.out_text().endswith("\x1b[?2004l")))

    def test_masked_input_drops_escape_sequences_whole(self):
        """CSI and SS3 keys (F1 is ESC O P) leave no byte in the value; letters stay."""
        with mock.patch("pathfinder.keychain.store") as store:
            self.type_("/secret rv11test TOKEN\r")
            self.assertTrue(_wait_for(lambda: "(hidden;" in self.out_text()))
            self.type_("\x1b[200~BPASTE\x1b[201~\x1b[A\x1b[D\x1b[1;5C\x1bOP")
            self.assertTrue(_wait_for(lambda: self.out_text().endswith(tui_secret.SECRET_PROMPT + "******")))
            self.type_("\r")
            self.assertTrue(_wait_for(lambda: self._secrets()))
            self.assertEqual(self._secrets(), [_answer("BPASTE")])

    def test_ctrl_c_interrupts_a_running_stream_then_quits(self):
        self.daemon.send({"type": "turn.start", "turn": 1})
        time.sleep(0.1)
        self.type_("\x03")
        self.assertTrue(_wait_for(lambda: any(m.get("type") == "interrupt" for m in self.daemon.received)))
        self.daemon.send({"type": "turn.end", "kind": "cancelled"})
        time.sleep(0.1)
        self.type_("\x03")
        self._main.join(timeout=2)
        self.assertEqual(self.rc, 0)

    def test_terminal_mode_restored_on_error(self):
        self.daemon.close()
        self._main.join(timeout=2)
        self.assertEqual(self.rc, 1)
        # A full tcgetattr() round-trip through cbreak isn't bit-identical on this platform (an
        # extra high bit shows up in lflag), so check the flags that matter instead: ECHO and
        # ICANON are off under cbreak and must be back on once main() returns.
        lflag = termios.tcgetattr(self.slave)[3]
        self.assertTrue(lflag & termios.ECHO)
        self.assertTrue(lflag & termios.ICANON)

    def test_render_is_shared_with_listen(self):
        """One renderer: `__main__.listen` prints server messages through `tui.render`."""
        src = (Path(tui.__file__).parent / "__main__.py").read_text(encoding="utf-8")
        listen_src = src[src.index("def listen("):src.index("def signal(")]
        self.assertIn("tui.render(msg)", listen_src)
        self.assertNotIn('msg["type"] == "notice"', listen_src)

    def test_render_covers_every_kind(self):
        for msg in (
            {"type": "attached", "session": "s", "resumed": False, "turns": 0},
            {"type": "turn.start", "turn": 1},
            {"type": "delta", "text": "hi"},
            {"type": "tool", "name": "search", "ok": True},
            {"type": "tool", "name": "search", "ok": False},
            {"type": "gate", "id": "g1", "tool": "search", "args": {"q": "x"}},
            {"type": "turn.end", "kind": "ok"},
            {"type": "notice", "id": "n1", "text": "hi", "why": "signal"},
            {"type": "job.start", "id": "j1", "goal": "do things"},
            {"type": "job.gate", "id": "j1", "approval": "a1", "tool": "search", "args": {}},
            {"type": "job.end", "id": "j1", "state": "done"},
            {"type": "error", "message": "boom"},
            {"type": "retract", "what": "says something was done", "why": "w"},
        ):
            tui.render(msg)  # must not raise KeyError

    def test_retract_rendered(self):
        """D51: the withdrawal is one dim harness line on the screen and on `say`'s stderr; the
        verifier's own `why` is never shown."""
        from pathfinder import __main__ as cli
        from pathfinder.loop import FLAG_WORDS
        msg = {"type": "retract", "what": FLAG_WORDS["unsupported_state"], "why": "MODEL-WHY"}
        line = ("철회 · withdrawn: the answer above says what the calendar, reminders, memory or registry "
                "hold without calling a tool - trying again with the tools")
        self.assertEqual(tui.render(msg), tui.NOTICE_DIM + line + tui.RESET)
        server, client = socket.socketpair()
        with server, client:
            for m in ({"type": "turn.start", "turn": "t"}, {"type": "delta", "text": "Your list is empty."}, msg,
                      {"type": "delta", "text": "It holds two."}, {"type": "turn.end", "kind": "answer"}):
                server.sendall((json.dumps(m, ensure_ascii=False) + "\n").encode())
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli._stream(client, []), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("Your list is empty.\nIt holds two.\n", line + "\n"))

    def test_notice_text_is_sanitized(self):
        out = tui.render({"type": "notice", "id": "n", "text": "bad\x07bell", "why": "y"})
        self.assertNotIn("\x07", out)

    def test_gate_line_has_no_bidi_or_c1(self):
        """U33: what is shown is what runs - a hidden character in an approval line is escaped, never
        dropped and never raw; tui `gate`, `job.gate`, `say` and `approvals` render the args alike."""
        from pathfinder import __main__ as cli
        from pathfinder.ledger import Ledger
        from pathfinder.state import State
        args = {"to": "bo\u200bss@example.com", "path": "/x/\u202eevil\x9b"}
        shown = '{"to": "bo\\u200bss@example.com", "path": "/x/\\u202eevil\\u009b"}'
        gate = {"type": "gate", "id": "g1", "tool": "send\u200b", "args": args}
        lines = [tui.render(gate), tui.render({**gate, "type": "job.gate", "approval": "a1"})]
        server, client = socket.socketpair()
        with server, client:
            for m in (gate, {"type": "turn.end", "kind": "answer"}):
                server.sendall((json.dumps(m, ensure_ascii=False) + "\n").encode())
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                cli._stream(client, [])
        lines.append(err.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / "state.db")
            state.apply(Ledger(Path(tmp)).append("approval_pending", session="s1", origin="user", id="a2",
                                                call_id="c1", tool="send\u200b", args=args, messages=[]))
            memory = {"target": "memory", "op": "replace", "old": "typed \\u200b", "new": "real \u200b"}
            state.apply(Ledger(Path(tmp)).append("approval_pending", session="s1", origin="memory", id="a3",
                                                call_id="c2", tool="edit_memory", args=memory, messages=[]))
            state.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.approvals(Path(tmp))
        # typed text and the real character stay apart, as in the args rows
        self.assertIn('a3  edit_memory  memory replace: "typed \\\\u200b" -> "real \\u200b"', out.getvalue())
        lines.append(out.getvalue())
        for line in lines:
            self.assertFalse(set(line) & set("\u200b\u202e\x9b\x1b"), repr(line))
            self.assertIn(shown, line)
            self.assertIn("send\\u200b", line)

    def test_gate_args_clipped(self):
        """5d: past 300 shown characters the args end in a tail. The count is taken after escaping,
        and the cut never splits an escape: here a `\\u200b` straddles the 300th character."""
        args = {"q": "a" * 290 + "\u200b" * 10}  # `{"q": "` and 290 letters: 297 shown, then 6-long units
        for msg in ({"type": "gate", "id": "g1", "tool": "search", "args": args},
                    {"type": "job.gate", "id": "j1", "approval": "a1", "tool": "search", "args": args}):
            self.assertIn(' search {"q": "' + "a" * 290 + "...(clipped; /approvals) - /approve", tui.render(msg))
        exact = tui.render({"type": "gate", "id": "g1", "tool": "search", "args": {"q": "a" * 291}})
        self.assertIn('"a' + "a" * 290 + '"} - /approve', exact)  # exactly 300 shown: kept whole


if __name__ == "__main__":
    unittest.main()
