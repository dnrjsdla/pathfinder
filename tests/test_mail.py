import time
import unittest
from unittest import mock

from pathfinder.senses import mail as mail_sense


def _clear_watermark():
    mail_sense._last_uid.clear()


class FakeIMAP:
    """Records every command; `uid("search", ...)` and `uid("fetch", ...)` return canned
    replies. Good enough to prove `poll` never issues a full-body fetch."""

    def __init__(self, *a, uidvalidity=b"1", search_uids=b"1 2", **kw):
        self.commands = []
        self._uidvalidity = uidvalidity
        self._search_uids = search_uids

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
            return "OK", [self._search_uids]
        if cmd == "fetch":
            uid, fields = args
            raw = (f"From: a@example.com\r\nSubject: hi\r\nMessage-ID: <{uid}@example.com>\r\n\r\n").encode()
            return "OK", [(b"1 (...)", raw)]
        return "OK", [None]

    def logout(self):
        self.commands.append(("logout",))


class MailHeadersOnlyTest(unittest.TestCase):
    def setUp(self):
        _clear_watermark()  # module-level, shared across tests using the same host/user/mailbox

    def _run_poll(self):
        fake = FakeIMAP()
        with mock.patch("pathfinder.senses.mail.imaplib.IMAP4_SSL", return_value=fake), \
             mock.patch("pathfinder.senses.mail.subprocess.run",
                       return_value=mock.Mock(returncode=0, stdout="secret-pw\n")):
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
        with mock.patch("pathfinder.senses.mail.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="secret-pw\n")) as run, \
             mock.patch("pathfinder.senses.mail.imaplib.IMAP4_SSL", return_value=FakeIMAP()) as ssl_cls:
            mail_sense.poll({"host": "imap.example.com", "user": "me@example.com"}, time.time())
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["security", "find-generic-password"])
        self.assertIn("me@example.com", argv)
        self.assertIn("pathfinder-mail", argv)  # the default keychain_service
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
        with mock.patch("pathfinder.senses.mail.imaplib.IMAP4_SSL", return_value=fake), \
             mock.patch("pathfinder.senses.mail.subprocess.run",
                       return_value=mock.Mock(returncode=0, stdout="pw\n")), \
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
    UIDVALIDITY) starts fresh instead of silencing the sense forever."""

    def setUp(self):
        _clear_watermark()

    def test_uidvalidity_change_resets_watermark(self):
        options = {"host": "imap.example.com", "user": "me@example.com"}
        with mock.patch("pathfinder.senses.mail.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="pw\n")):
            fake1 = FakeIMAP(uidvalidity=b"100", search_uids=b"1 2 3 4")
            with mock.patch("pathfinder.senses.mail.imaplib.IMAP4_SSL", return_value=fake1):
                first = mail_sense.poll(options, time.time())
            self.assertEqual(len(first), 4)  # watermark now 4 for UIDVALIDITY 100

            fake2 = FakeIMAP(uidvalidity=b"100", search_uids=b"1 2 3 4 5")
            with mock.patch("pathfinder.senses.mail.imaplib.IMAP4_SSL", return_value=fake2):
                same_mailbox = mail_sense.poll(options, time.time())
            self.assertEqual(len(same_mailbox), 1)  # only uid 5 is new

            fake3 = FakeIMAP(uidvalidity=b"200", search_uids=b"1 2 3 4")  # rebuilt mailbox
            with mock.patch("pathfinder.senses.mail.imaplib.IMAP4_SSL", return_value=fake3):
                rebuilt = mail_sense.poll(options, time.time())
            self.assertEqual(len(rebuilt), 4)  # uids 1-4 re-served under the new UIDVALIDITY


class FoldedMessageIdTest(unittest.TestCase):
    def setUp(self):
        _clear_watermark()

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
        with mock.patch("pathfinder.senses.mail.imaplib.IMAP4_SSL", return_value=fake), \
             mock.patch("pathfinder.senses.mail.subprocess.run",
                       return_value=mock.Mock(returncode=0, stdout="pw\n")):
            signals = mail_sense.poll({"host": "imap.example.com", "user": "me@example.com"}, time.time())
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].key, "uid:7")  # not the mangled folded value


if __name__ == "__main__":
    unittest.main()
