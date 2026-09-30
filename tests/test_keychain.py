import contextlib
import io
import tempfile
import unittest
from unittest import mock

from pathfinder import keychain


class KeychainTest(unittest.TestCase):
    def test_secret_found(self):
        with mock.patch("pathfinder.keychain.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="s3cr3t\n")) as run:
            self.assertEqual(keychain.secret("pathfinder-mcp-gh", "bot"), "s3cr3t")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["security", "find-generic-password", "-s", "pathfinder-mcp-gh", "-a", "bot", "-w"])

    def test_secret_missing_returns_none(self):
        with mock.patch("pathfinder.keychain.subprocess.run",
                        return_value=mock.Mock(returncode=44, stdout="")):
            self.assertIsNone(keychain.secret("svc", "acct"))

    def test_no_security_binary_returns_none(self):
        with mock.patch("pathfinder.keychain.subprocess.run", side_effect=FileNotFoundError()):
            self.assertIsNone(keychain.secret("svc", "acct"))

    # -- 5f: `-w` prints a non-ASCII value as bare hex; `-g` alone says so ---------------------
    def _find(self, w_out, g_err):
        def run(argv, **kw):
            if argv[-1] == "-w":
                return mock.Mock(returncode=0, stdout=w_out + "\n", stderr="")
            return mock.Mock(returncode=0, stdout="attributes:\n", stderr=g_err)
        with mock.patch("pathfinder.keychain.subprocess.run", side_effect=run) as fake:
            return keychain.secret("svc", "acct"), [c[0][0][-1] for c in fake.call_args_list]

    def test_secret_decodes_hex_only_when_g_says_0x(self):
        hexed = "비밀abc".encode().hex()
        value, calls = self._find(hexed, f'password: 0x{hexed.upper()}  "\\353\\271\\204\\353\\260\\200abc"\n')
        self.assertEqual(value, "비밀abc")
        self.assertEqual(calls, ["-w", "-g"])
        self.assertEqual(self._find("plain-token", "")[1], ["-w"])  # no second call for a non-hex value

    def test_hex_looking_ascii_token_is_kept(self):
        value, calls = self._find("deadbeef", 'password: "deadbeef"\n')
        self.assertEqual(value, "deadbeef")
        self.assertEqual(calls, ["-w", "-g"])

    # -- store --------------------------------------------------------------------------------
    def _store(self, value, returncode=0, read_back=None):
        run = mock.Mock(return_value=mock.Mock(returncode=returncode, stdout="", stderr=""))
        with mock.patch("pathfinder.keychain.secret", return_value=value if read_back is None else read_back):
            keychain.store("pathfinder-mcp-x", "TOKEN", value, run=run)
        return run

    def test_store_sends_the_value_on_stdin_not_argv(self):
        value = "ntn_" + "abc123"
        run = self._store(value)
        argv, kw = run.call_args
        self.assertEqual(argv[0], ["security", "-i"])
        self.assertEqual(kw["input"], f'add-generic-password -U -s "pathfinder-mcp-x" -a "TOKEN" -w "{value}"\n')

    def test_store_escapes_backslash_then_quote(self):
        run = self._store('a\\b"c')
        self.assertIn(r'-w "a\\b\"c"', run.call_args[1]["input"])  # quote-first would give a\\b\\"c

    def test_store_refuses_newline_and_nul(self):
        for bad in ("a\nb", "a\0b"):
            run = mock.Mock()
            with self.assertRaises(ValueError):
                keychain.store("svc", "acct", bad, run=run)
            run.assert_not_called()

    def test_store_checks_returncode_and_reads_back(self):
        with self.assertRaises(RuntimeError) as err:
            self._store("s3cr3t", returncode=45)
        self.assertNotIn("s3cr3t", str(err.exception))
        with self.assertRaises(RuntimeError) as err:
            self._store('s3"cr3t', read_back="s3")  # an unescaped quote truncates and still exits 0
        self.assertNotIn('s3"cr3t', str(err.exception))
        self._store("s3cr3t")  # exit 0 and the same value back: no error


class SecretCliTest(unittest.TestCase):
    def test_cli_secret_uses_getpass_and_store(self):
        from pathfinder.__main__ import main
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict("os.environ", {"PATHFINDER_HOME": home}), \
                mock.patch("pathfinder.__main__.getpass.getpass", return_value="v4lue") as ask, \
                mock.patch("pathfinder.__main__.keychain.store") as store, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["secret", "easy-notion", "NOTION_TOKEN"]), 0)
            store.assert_called_once_with("pathfinder-mcp-easy-notion", "NOTION_TOKEN", "v4lue")
            self.assertIn("NOTION_TOKEN", ask.call_args[0][0])
            store.reset_mock()
            self.assertEqual(main(["secret", "Bad_Id", "NOTION_TOKEN"]), 2)
            self.assertEqual(main(["secret", "easy-notion", "NOT-A-NAME"]), 2)
            store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
