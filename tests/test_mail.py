import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import config
from pathfinder.broker import _clip_for_model
from pathfinder.senses import mail as mail_sense

_KEY = ("imap.example.com", "me@example.com", "INBOX", b"1")  # what FakeIMAP's defaults connect to


def _clear_watermark():
    mail_sense._last_uid.clear()


def _watermark_at_zero():
    """This process has polled the mailbox before and found nothing unread - the next poll announces."""
    mail_sense._last_uid.clear()
    mail_sense._last_uid[_KEY] = 0


class FakeIMAP:
    """Records every command; `uid("search", ...)` and `uid("fetch", ...)` return canned
    replies. Good enough to prove `poll` never issues a full-body fetch. Like imaplib, a search
    sends `literal` after its arguments and clears it; `literals` keeps what each search sent."""

    def __init__(self, *a, uidvalidity=b"1", search_uids=b"1 2", capabilities=("IMAP4REV1",), message=None,
                 messages=None, search_typ="OK", **kw):
        self.commands = []
        self._uidvalidity = uidvalidity
        self._search_uids = search_uids
        self.capabilities = capabilities
        self.literal = None
        self.literals = []
        self._message = message
        self._messages = messages or {}
        self._search_typ = search_typ

    def login(self, user, password):
        self.commands.append(("login", user, password))

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly))

    def response(self, code):
        self.commands.append(("response", code))
        return "OK", [self._uidvalidity] if code == "UIDVALIDITY" else [None]

    def uid(self, cmd, *args):
        self.commands.append(("uid", cmd, args))
        if cmd == "search":
            self.literals.append(self.literal)
            self.literal = None
            if self._search_typ != "OK":
                return self._search_typ, [b"[BADCHARSET] nope"]
            return "OK", [self._search_uids]
        if cmd == "fetch":
            uid, fields = args
            raw = self._messages.get(uid) or self._message or (f"From: a@example.com\r\nSubject: hi\r\nMessage-ID: <{uid}@example.com>\r\n\r\n").encode()
            return "OK", [(b"1 (...)", raw)]
        return "OK", [None]

    def logout(self):
        self.commands.append(("logout",))


class MailHeadersOnlyTest(unittest.TestCase):
    def setUp(self):
        _watermark_at_zero()  # module-level, shared across tests using the same host/user/mailbox

    def _run_poll(self):
        fake = FakeIMAP()
        with mock.patch("pathfinder.senses._imap.imaplib.IMAP4_SSL", return_value=fake), \
             mock.patch("pathfinder.senses._imap.keychain.secret", return_value="secret-pw"):
            signals = mail_sense.poll({"host": "imap.example.com", "user": "me@example.com"}, time.time())
        return fake, signals

    def test_mail_headers_only(self):
        fake, signals = self._run_poll()
        fetches = [c for c in fake.commands if c[0] == "uid" and c[1] == "fetch"]
        self.assertTrue(fetches)
        for _cmd, _kind, args in fetches:
            fields = args[1]
            self.assertIn("PEEK[HEADER", fields)
            self.assertNotIn("BODY.PEEK[]", fields)  # never a full body during poll
            self.assertNotIn("BODY[", fields.replace("BODY.PEEK[", ""))  # no unprotected BODY[ either
        selects = [c for c in fake.commands if c[0] == "select"]
        self.assertTrue(all(readonly for _cmd, _mailbox, readonly in selects))  # SELECT is read-only
        self.assertEqual(len(signals), 2)
        self.assertEqual({s.kind for s in signals}, {"new_mail"})

    def test_mail_password_from_keychain_only(self):
        self.assertNotIn("password", mail_sense.OPTIONS)  # never a config key
        with mock.patch("pathfinder.senses._imap.keychain.secret", return_value="secret-pw") as secret, \
             mock.patch("pathfinder.senses._imap.imaplib.IMAP4_SSL", return_value=FakeIMAP()) as ssl_cls:
            mail_sense.poll({"host": "imap.example.com", "user": "me@example.com"}, time.time())
        secret.assert_called_once_with("pathfinder-mail", "me@example.com")  # the default keychain_service
        fake = ssl_cls.return_value
        logins = [c for c in fake.commands if c[0] == "login"]
        self.assertEqual(logins[0][2], "secret-pw")  # the password came from the mocked Keychain call

    def test_mail_not_configured_raises(self):
        with self.assertRaises(RuntimeError):
            mail_sense.poll({"host": "", "user": ""}, time.time())


class ReadMailInjectionGuardTest(unittest.TestCase):
    """Blocker: a model-supplied Message-ID goes straight into a SEARCH string."""

    def _try(self, bad_id):
        fake = FakeIMAP()
        with mock.patch("pathfinder.senses._imap.imaplib.IMAP4_SSL", return_value=fake), \
             mock.patch("pathfinder.senses._imap.keychain.secret", return_value="pw"), \
             mock.patch("pathfinder.senses.mail.config.load", return_value={"senses": {"mail": {
                 "host": "imap.example.com", "user": "me@example.com"}}}):
            with self.assertRaises(ValueError):
                mail_sense._read_mail(bad_id)
        searches = [c for c in fake.commands if c[0] == "uid" and c[1] == "search"]
        self.assertEqual(searches, [])  # no SEARCH was ever sent

    def test_quote_widens_search_refused(self):
        self._try('x" HEADER SUBJECT "secret')

    def test_crlf_command_injection_refused(self):
        self._try("a\r\nA001 LOGOUT")


class UidvalidityWatermarkTest(unittest.TestCase):
    """Should-fix 2: the watermark is keyed by UIDVALIDITY too, so a mailbox rebuild (a new
    UIDVALIDITY) starts a fresh watermark instead of silencing the sense forever."""

    def setUp(self):
        _clear_watermark()

    def _poll(self, uidvalidity, uids):
        with mock.patch("pathfinder.senses._imap.imaplib.IMAP4_SSL", return_value=FakeIMAP(uidvalidity=uidvalidity, search_uids=uids)):
            return mail_sense.poll({"host": "imap.example.com", "user": "me@example.com"}, time.time())

    def test_uidvalidity_change_resets_watermark(self):
        with mock.patch("pathfinder.senses._imap.keychain.secret", return_value="pw"):
            self.assertEqual(self._poll(b"100", b"1 2 3 4"), [])  # first look: watermark 4, silently
            self.assertEqual(len(self._poll(b"100", b"1 2 3 4 5")), 1)  # only uid 5 is new
            self.assertEqual(self._poll(b"200", b"1 2 3 4"), [])  # rebuilt mailbox: a new first look
            self.assertEqual(len(self._poll(b"200", b"1 2 3 4 5")), 1)  # and it still announces


class FoldedMessageIdTest(unittest.TestCase):
    def setUp(self):
        _watermark_at_zero()

    def test_folded_message_id_falls_back_like_missing(self):
        fake = FakeIMAP()

        def uid(cmd, *args):
            fake.commands.append(("uid", cmd, args))
            if cmd == "search":
                return "OK", [b"7"]
            if cmd == "fetch":
                raw = b"From: a@example.com\r\nSubject: hi\r\nMessage-ID: <abc\r\n @example.com>\r\n\r\n"
                return "OK", [(b"1 (...)", raw)]
            return "OK", [None]

        fake.uid = uid
        with mock.patch("pathfinder.senses._imap.imaplib.IMAP4_SSL", return_value=fake), \
             mock.patch("pathfinder.senses._imap.keychain.secret", return_value="pw"):
            signals = mail_sense.poll({"host": "imap.example.com", "user": "me@example.com"}, time.time())
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].key, "uid:1.7")  # not the mangled folded value; UIDVALIDITY 1


class FirstPollTest(unittest.TestCase):
    """U25: the watermark lives in the process, so a restart used to re-announce every UNSEEN
    mail older than the 24-hour dedupe - a thousand `new_mail` signals from a busy inbox."""

    def setUp(self):
        _clear_watermark()

    def _poll(self, uids):
        with mock.patch("pathfinder.senses._imap.imaplib.IMAP4_SSL", return_value=FakeIMAP(search_uids=uids)), \
             mock.patch("pathfinder.senses._imap.keychain.secret", return_value="pw"):
            return mail_sense.poll({"host": "imap.example.com", "user": "me@example.com"}, time.time())

    def test_first_poll_takes_the_watermark_silently(self):
        self.assertEqual(self._poll(b"1 2"), [])  # a restart announces nothing
        self.assertEqual([s.key for s in self._poll(b"1 2 3")], ["<3@example.com>"])
        _clear_watermark()
        self.assertEqual(self._poll(b""), [])  # nothing unread is a watermark too...
        self.assertEqual(len(self._poll(b"4")), 1)  # ...so the next mail is announced, not swallowed


class MailToolsTest(unittest.TestCase):
    """U25: `search_mail`, the filter over someone else's text, and the bound `HOME`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        config.set(self.home, "senses", {"mail": {"host": "imap.example.com", "user": "me@example.com"}})
        mail_sense.HOME = self.home
        self.addCleanup(setattr, mail_sense, "HOME", None)

    def _call(self, fake, fn, *args):
        with mock.patch("pathfinder.senses._imap.imaplib.IMAP4_SSL", return_value=fake) as ssl, \
             mock.patch("pathfinder.senses._imap.keychain.secret", return_value="pw"):
            result = fn(*args)
        self.ssl = ssl
        return result

    def _searches(self, fake):
        return [c[2] for c in fake.commands if c[:2] == ("uid", "search")]

    def test_search_mail_uses_x_gm_raw_on_gmail_and_text_elsewhere(self):
        gmail = FakeIMAP(capabilities=("IMAP4REV1", "X-GM-EXT-1"), search_uids=b"3 1 2")
        rows = self._call(gmail, mail_sense._search_mail, "from:a@example.com newer_than:7d", 2)
        self.assertEqual(self._searches(gmail), [("CHARSET", "UTF-8", "X-GM-RAW")])
        self.assertEqual([r["id"] for r in rows], ["<3@example.com>", "<2@example.com>"])  # newest 2, newest first
        self.assertEqual(set(rows[0]), {"id", "from", "subject", "date"})
        other = FakeIMAP()
        self._call(other, mail_sense._search_mail, "invoice")
        self.assertEqual(self._searches(other), [("CHARSET", "UTF-8", "TEXT")])

    def test_query_goes_as_a_literal_utf8(self):
        query = '회의록 "x" HEADER SUBJECT "y" A001 LOGOUT'
        fake = FakeIMAP(capabilities=("X-GM-EXT-1",))
        self._call(fake, mail_sense._search_mail, query)
        self.assertEqual(fake.literals, [query.encode("utf-8")])  # the literal, byte for byte
        self.assertEqual(self._searches(fake), [("CHARSET", "UTF-8", "X-GM-RAW")])  # nothing of it in the command
        for query, limit in (("", 5), ("x" * 201, 5), ("ok", 0), ("ok", 21), ("a\r\nb", 5), ("nul\x00", 5), ("sur\ud800", 5)):
            fake = FakeIMAP()
            with self.assertRaises(ValueError):
                self._call(fake, mail_sense._search_mail, query, limit)
            self.assertEqual(fake.commands, [])  # refused before connecting
        with self.assertRaises(ValueError):
            self._call(FakeIMAP(), mail_sense._list_mail, 21)  # the same 1-20 as search_mail

    def test_search_never_marks_read(self):
        fake = FakeIMAP(capabilities=("X-GM-EXT-1",))
        self._call(fake, mail_sense._search_mail, "hi")
        self.assertEqual([c[2] for c in fake.commands if c[0] == "select"], [True])  # read-only SELECT
        fetches = [c[2][1] for c in fake.commands if c[0] == "uid" and c[1] == "fetch"]
        self.assertTrue(fetches)
        self.assertTrue(all(f.startswith("(BODY.PEEK[HEADER.FIELDS") for f in fetches))
        self.assertEqual([c for c in fake.commands if c[0] == "uid" and c[1] not in ("search", "fetch")], [])

    def test_headers_pass_the_filter(self):
        import base64
        word = lambda text: "=?utf-8?b?" + base64.b64encode(text.encode()).decode() + "?="
        message = ("From: \x1b[31mEvil\x1b[0m <a@example.com>\r\n"
                   f"Subject: {word(chr(0x202E) + 'gnp.exe' + chr(0x200B))} {'x' * 400}\r\n"
                   "Date: Tue, 29 Sep 2026 10:00:00 +0900\x07\r\n"
                   "Message-ID: <m1@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                   "line one‮\r\n\r\n​line\x07 three\r\n").encode("utf-8")
        rows = [self._call(FakeIMAP(message=message), mail_sense._list_mail, 1)[0],
                self._call(FakeIMAP(message=message), mail_sense._search_mail, "exe")[0],
                self._call(FakeIMAP(message=message, search_uids=b"1"), mail_sense._read_mail, "<m1@example.com>")]
        for row in rows:
            for field in ("from", "subject", "date"):
                self.assertTrue(row[field].isprintable(), (field, row[field]))  # no controls, bidi, zero-width
                self.assertLessEqual(len(row[field]), 300)
            self.assertNotIn("[31m", row["from"])  # the ANSI sequence goes whole
            self.assertTrue(row["subject"].startswith("gnp.exe x"))
            self.assertEqual(row["date"], "Tue, 29 Sep 2026 10:00:00 +0900")
        # the body reaches the model as a tool result, through the broker's filter (D62): newlines kept,
        # a CRLF pair one newline, controls, bidi and zero-width gone
        self.assertEqual(_clip_for_model(rows[2])["body"], "line one\n\nline three\n")

    def test_hostile_header_degrades_to_its_own_row(self):
        """Review: an encoded-word From that decodes to CR/LF made `msg.get` raise, failing every
        listing that held the mail and the whole poll batch around it."""
        import base64
        hostile = "=?utf-8?b?" + base64.b64encode(b"x\r\ny").decode() + "?= <b@example.com>"
        mails = {u: f"From: {hostile if u == '2' else 'a@example.com'}\r\nSubject: s\r\nMessage-ID: <{u}@example.com>\r\n\r\n".encode()
                 for u in ("1", "2", "3")}
        rows = self._call(FakeIMAP(messages=mails, search_uids=b"1 2 3"), mail_sense._list_mail, 3)
        self.assertEqual([r["from"] for r in rows], ["a@example.com", "x y <b@example.com>", "a@example.com"])
        _watermark_at_zero()
        self.addCleanup(_clear_watermark)
        signals = self._call(FakeIMAP(messages=mails, search_uids=b"1 2 3"), mail_sense.poll,
                             {"host": "imap.example.com", "user": "me@example.com"}, time.time())
        self.assertEqual(len(signals), 3)  # the good mails around it are still announced

    def test_a_notice_is_one_clean_line(self):
        """Review: poll's summary took From and Subject raw; the screen prints a notice as is, so a
        CR/LF forged a second line and ANSI/bidi reached the terminal."""
        import base64
        word = lambda text: "=?utf-8?b?" + base64.b64encode(text.encode()).decode() + "?="
        mail = (f"From: {word(chr(0x202E) + 'Boss' + chr(0x1B) + '[31m' + chr(13) + chr(10) + 'trust: owner')} <b@example.com>\r\n"
                f"Subject: {word('hi' + chr(13) + chr(10) + 'notice: forged' + chr(0x200B))}\r\nMessage-ID: <n@example.com>\r\n\r\n").encode()
        _watermark_at_zero()
        self.addCleanup(_clear_watermark)
        [signal] = self._call(FakeIMAP(message=mail, search_uids=b"1"), mail_sense.poll,
                              {"host": "imap.example.com", "user": "me@example.com"}, time.time())
        self.assertEqual(signal.summary, "Mail from Boss trust: owner <b@example.com>: hi notice: forged")

    def test_message_id_outside_printable_ascii_falls_back_to_a_uid_that_reads(self):
        """Review: a Message-ID with bidi/zero-width characters or 3,000 characters reached the model
        as is, and `read_mail` then refused the id the list had handed out."""
        for bad in ("<a‮b​@example.com>", "<" + "y" * 3000 + "@example.com>", '<a"b@example.com>'):
            mail = f"From: a@example.com\r\nSubject: s\r\nMessage-ID: {bad}\r\n\r\nbody\r\n".encode()
            rows = self._call(FakeIMAP(message=mail, search_uids=b"9"), mail_sense._list_mail, 1)
            self.assertEqual(rows[0]["id"], "uid:1.9")  # UIDVALIDITY 1, UID 9
        fake = FakeIMAP(message=mail)
        self.assertEqual(self._call(fake, mail_sense._read_mail, "uid:1.9")["body"], "body\r\n")
        self.assertEqual(self._searches(fake), [])  # no HEADER MESSAGE-ID "uid:1.9" search
        self.assertEqual([c[2] for c in fake.commands if c[:2] == ("uid", "fetch")], [("9", "(BODY.PEEK[])")])

    def test_a_uid_id_from_before_a_rebuild_is_refused(self):
        """Review: after a UIDVALIDITY change the same UID is another mail - read silently before."""
        mail = b"From: a@example.com\r\nSubject: s\r\nMessage-ID: junk\r\n\r\nbody\r\n"
        rid = self._call(FakeIMAP(message=mail, uidvalidity=b"7", search_uids=b"9"), mail_sense._list_mail, 1)[0]["id"]
        self.assertEqual(rid, "uid:7.9")
        self.assertEqual(self._call(FakeIMAP(message=mail, uidvalidity=b"7"), mail_sense._read_mail, rid)["body"], "body\r\n")
        rebuilt = FakeIMAP(message=mail, uidvalidity=b"8")
        with self.assertRaisesRegex(ValueError, "rebuilt"):
            self._call(rebuilt, mail_sense._read_mail, rid)
        self.assertEqual([c for c in rebuilt.commands if c[:2] == ("uid", "fetch")], [])  # nothing read

    def test_a_refused_search_raises(self):
        for fn, args in ((mail_sense._search_mail, ("hi",)), (mail_sense._list_mail, (5,))):
            with self.assertRaisesRegex(RuntimeError, "BADCHARSET"):  # not an empty "no mail" answer
                self._call(FakeIMAP(search_typ="NO"), fn, *args)

    def test_read_mail_cuts_and_degrades(self):
        head = "From: a@example.com\r\nSubject: s\r\nMessage-ID: <m@example.com>\r\n"
        read = lambda raw: self._call(FakeIMAP(message=raw), mail_sense._read_mail, "<m@example.com>")["body"]
        body = read((head + "Content-Type: text/plain\r\n\r\n" + "z" * 9000).encode())
        self.assertEqual(body, "z" * 8000 + "...(truncated)")
        self.assertEqual(read((head + "Content-Type: text/html\r\n\r\n<p>hi</p>").encode()), "(no plain-text part)")
        self.assertEqual(read((head + "Content-Type: text/plain; charset=x-bogus\r\n\r\n").encode() + b"caf\xc3\xa9"), "café")

    def test_mail_tools_use_the_bound_home(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        config.set(Path(other.name), "senses", {"mail": {"host": "imap.other.example.com", "user": "b@example.com"}})
        with mock.patch("pathfinder.config.home", return_value=Path(other.name)):
            self._call(FakeIMAP(), mail_sense._list_mail, 5)
            self.assertEqual(self.ssl.call_args.args[0], "imap.example.com")
            self._call(FakeIMAP(), mail_sense._search_mail, "hi")
            self.assertEqual(self.ssl.call_args.args[0], "imap.example.com")
        mail_sense.HOME = None
        with self.assertRaises(RuntimeError):  # not loaded: no second answer from config.home()
            self._call(FakeIMAP(), mail_sense._list_mail, 5)

    def test_mail_under_150(self):
        n = len(Path(mail_sense.__file__).read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 150, f"mail.py is {n} lines, over the sense budget of 150")


if __name__ == "__main__":
    unittest.main()
