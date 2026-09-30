import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
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

    def test_hex_decoded_value_is_stripped_like_the_plain_one(self):
        hexed = " 비밀abc \t".encode().hex()  # `-w` hexes the edge whitespace along with the rest
        self.assertEqual(self._find(hexed, f"password: 0x{hexed.upper()}\n")[0], "비밀abc")

    def test_hex_looking_ascii_token_is_kept(self):
        value, calls = self._find("deadbeef", 'password: "deadbeef"\n')
        self.assertEqual(value, "deadbeef")
        self.assertEqual(calls, ["-w", "-g"])

    # -- store --------------------------------------------------------------------------------
    def _store(self, value, returncode=0, read_back=None):
        """One fake `security` answers the write and the read-back - never the real Keychain."""
        back = (value.strip() if read_back is None else read_back) + "\n"
        run = mock.Mock(side_effect=lambda argv, **kw: mock.Mock(
            returncode=returncode if argv == ["security", "-i"] else 0, stdout=back if "-w" in argv else "", stderr=""))
        keychain.store("pathfinder-mcp-x", "TOKEN", value, run=run)
        return run

    def test_store_sends_the_value_on_stdin_not_argv(self):
        value = "ntn_" + "abc123"
        run = self._store(value)
        argv, kw = run.call_args_list[0]
        self.assertEqual(argv[0], ["security", "-i"])
        self.assertEqual(kw["input"], f'add-generic-password -U -s "pathfinder-mcp-x" -a "TOKEN" -w "{value}"\n')

    def test_store_escapes_backslash_then_quote(self):
        run = self._store('a\\b"c')
        self.assertIn(r'-w "a\\b\"c"', run.call_args_list[0][1]["input"])  # quote-first would give a\\b\\"c

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
        run = self._store("s3cr3t")  # exit 0 and the same value back: no error
        self.assertEqual(run.call_args_list[1][0][0][:2], ["security", "find-generic-password"])  # the injected run

    def test_store_strips_edge_whitespace_and_refuses_empty(self):
        run = self._store("  s3cr3t \t")  # `secret()` strips on read: kept, it could never read back equal
        self.assertIn('-w "s3cr3t"\n', run.call_args_list[0][1]["input"])
        for empty in ("", "   "):
            run = mock.Mock()
            with self.assertRaises(ValueError):
                keychain.store("svc", "acct", empty, run=run)
            run.assert_not_called()

    def test_store_refuses_a_lone_surrogate_without_the_value(self):
        value = "s3cr3t" + "\ud800" + "tail"
        run = mock.Mock()
        with self.assertRaises(ValueError) as err:
            keychain.store("svc", "acct", value, run=run)
        run.assert_not_called()
        e = err.exception
        for text in (str(e), repr(e), repr(e.args), repr(e.__cause__), repr(e.__context__)):
            self.assertNotIn("s3cr3t", text)


class SecretCliTest(unittest.TestCase):
    def _main(self, *args, answer="v4lue"):
        from pathfinder.__main__ import main
        ask = mock.patch("pathfinder.__main__.getpass.getpass",
                         **({"side_effect": answer} if isinstance(answer, BaseException) else {"return_value": answer}))
        with tempfile.TemporaryDirectory() as home, mock.patch.dict("os.environ", {"PATHFINDER_HOME": home}), \
                ask as self.ask, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as self.err:
            return main(["secret", *args])

    def test_cli_secret_uses_getpass_and_store(self):
        with mock.patch("pathfinder.__main__.keychain.store") as store:
            self.assertEqual(self._main("easy-notion", "NOTION_TOKEN"), 0)
            store.assert_called_once_with("pathfinder-mcp-easy-notion", "NOTION_TOKEN", "v4lue")
            self.assertIn("NOTION_TOKEN", self.ask.call_args[0][0])
            store.reset_mock()
            self.assertEqual(self._main("Bad_Id", "NOTION_TOKEN"), 2)
            self.assertEqual(self._main("easy-notion", "NOT-A-NAME"), 2)
            store.assert_not_called()

    def test_cli_secret_ctrl_c_eof_and_empty_store_nothing(self):
        with mock.patch("pathfinder.keychain.subprocess.run") as run:
            for answer in (KeyboardInterrupt(), EOFError(), "", "   "):
                self.assertEqual(self._main("easy-notion", "NOTION_TOKEN", answer=answer), 1)
                self.assertNotIn("Traceback", self.err.getvalue())
            run.assert_not_called()


class TokenPatternTest(unittest.TestCase):
    # Every token-shaped string is built by concatenation, so the scanner never flags this file.
    MUST_MATCH = {
        "GitHub classic": "gh" + "p_" + "A1b2" * 9,
        "GitHub fine-grained": "github" + "_pat_" + "11AB" * 6 + "_" + "xY9z" * 14,
        "Slack bot": "xo" + "xb-" + "1234567890-1234567890123-" + "AbCd" * 6,
        "Slack app-level": "xa" + "pp-1-A0" + "B1C2D3E4-1234567890123-" + "ab" * 32,
        "AWS access key id": "AK" + "IA" + "IOSFODNN7EXAMPLE",
        "OpenAI project": "sk-" + "proj-" + "Ab1Cd2" * 8,
        "Anthropic": "sk-" + "ant-api03-" + "Zz9" * 20,
        "Google API": "AI" + "za" + "SyD" + "x" * 32,
        "Notion": "nt" + "n_" + "1234567890" + "aBcD" * 9,
        "Notion legacy": "sec" + "ret_" + "AbC1" * 10 + "xyz",
        "Stripe live": "sk" + "_live_" + "51H" + "a" * 30,
        "Stripe test": "sk" + "_test_" + "51H" + "b" * 30,
        "Stripe restricted live": "rk" + "_live_" + "51H" + "c" * 30,
        "Stripe restricted test": "rk" + "_test_" + "51H" + "d" * 30,
        "JWT": "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "ey" + "JzdWIiOiIxMjM0In0" + "." + "dBjftJeZ4CVP" + "mB92K27uhbUJU1p1r_wW1g",
        "Telegram bot": "123456789" + ":" + "AAH" + "d" * 32,
        "private key block": "-----BEGIN " + "RSA PRIVATE KEY-----\n" + "MIIEow" + "IBAAKCAQEA" * 3 + "\n-----END RSA PRIVATE KEY-----",
        "private key header alone": "-----BEGIN " + "PRIVATE KEY-----",
    }
    MUST_NOT_MATCH = {
        "git SHA": "6d9b265f0e1d2c3b4a5968778695a4b3c2d1e0f1",
        "UUID": "3f2b9c1e-8a4d-4f6b-9e2a-1c5d7b8e9f0a",
        "base64 image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
        "Message-ID": "<CAF=" + "Yq8x" * 8 + "@mail.example.com>",
        "long URL": "https://github.com/modelcontextprotocol/servers/tree/main/src/everything/README.md",
        "Korean": "내일 오후 세 시에 회의 일정을 잡아줘, 그리고 메일도 확인해줘.",
        "sk-learn": "pip install scikit-learn; import sk-learn",
        "MCP packages": "@modelcontextprotocol/server-filesystem mcp-server-sqlite @notionhq/notion-mcp-server",
        "task-management": "npx -y @hyperbolic/task-management-mcp-server",
        "disk-usage": "disk-usage-analyzer-server",
        "ask-me": "ask-me-anything-about-the-weather-today",
        "risk-": "risk-assessment-framework-v2",
        "risk_test_": "risk_test_" + "a" * 24,
        "ledger ids": "turn=0a1b2c3d4e5f id=9f8e7d6c5b4a seq=48213",
        "handle": "[secret:pasted-1]",
        "snake_case secret_": "secret_value_never_reaches_ledger test_secret_missing_records_one_error",
        "camelCase secret_": "secret_ConfigurationManagerFactory",
        "public key header": "-----BEGIN PUBLIC KEY-----",
        "AWS ARN": "arn:aws:iam::123456789012:role/service-role",
        "npm integrity": "sha512-" + "Qm9vdHN0cmFwIGludGVncml0eSBoYXNoIGZvciBhIHRlc3QgcGFja2FnZQ==",
        "xoxo": "hugs xoxo-from-the-team-at-the-office",
        "a timestamp and a longer tail": "at 1727712000:" + "b" * 40,
        "a JWT header alone": "ey" + "JhbGciOiJIUzI1NiJ9",
    }

    def test_token_re_catches_the_known_shapes(self):
        for kind, token in self.MUST_MATCH.items():
            with self.subTest(kind):
                self.assertEqual(keychain.TOKEN_RE.findall(f"see {token}. then"), [token])
        env = "OPENAI_API_KEY=" + self.MUST_MATCH["OpenAI project"]
        self.assertEqual(keychain.TOKEN_RE.findall(env), [self.MUST_MATCH["OpenAI project"]])

    def test_token_re_spares_ordinary_text(self):
        for kind, text in self.MUST_NOT_MATCH.items():
            with self.subTest(kind):
                self.assertEqual(keychain.TOKEN_RE.findall(text), [])

    def test_scanner_uses_the_package_pattern(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "scan_personal.py"
        spec = importlib.util.spec_from_file_location("scan_personal", path)
        scan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scan)
        self.assertIn(("token", keychain.TOKEN_RE), scan.PATTERNS)
        for token in self.MUST_MATCH.values():
            self.assertIn("token", scan.hits_in(f"x {token} y", set()))
        for text in self.MUST_NOT_MATCH.values():
            self.assertNotIn("token", scan.hits_in(text, set()))


if __name__ == "__main__":
    unittest.main()
