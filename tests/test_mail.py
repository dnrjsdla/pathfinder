import time
import unittest
from unittest import mock

from pathfinder.senses import mail as mail_sense


class FakeIMAP:
    """Records every command; `uid("search", ...)` and `uid("fetch", ...)` return canned
    replies. Good enough to prove `poll` never issues a full-body fetch."""

    def __init__(self, *a, **kw):
        self.commands = []

    def login(self, user, password):
        self.commands.append(("login", user, password))

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly))

    def uid(self, cmd, *args):
        self.commands.append(("uid", cmd, args))
        if cmd == "search":
            return "OK", [b"1 2"]
        if cmd == "fetch":
            uid, fields = args
            raw = (f"From: a@example.com\r\nSubject: hi\r\nMessage-ID: <{uid}@example.com>\r\n\r\n").encode()
            return "OK", [(b"1 (...)", raw)]
        return "OK", [None]

    def logout(self):
        self.commands.append(("logout",))


class MailHeadersOnlyTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
