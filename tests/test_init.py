import contextlib
import http.server
import io
import json
import os
import plistlib
import shlex
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import __main__ as cli
from pathfinder import config, init

# "bge" is excluded on purpose: init.py's embed_model heuristic (`"bge" in id.lower()`) has to
# name that substring to detect it in ids the machine reports - that is pattern matching against
# runtime data, not a hardcoded model. \b avoids "ollama" tripping the "llama" check.
_MODEL_FAMILIES = ("gpt-oss", "gemma", "qwen", "llama", "nemotron", "nomic")


def _handler(body):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


def _serve(body):
    server = http.server.HTTPServer(("127.0.0.1", 0), _handler(body))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/models"


class InitTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "pathfinder-home"
        fake_home = Path(self._tmp.name) / "fake-home"
        fake_home.mkdir()
        self._old_home = os.environ.get("HOME")
        self._old_pf_home = os.environ.get("PATHFINDER_HOME")
        os.environ["HOME"] = str(fake_home)
        os.environ["PATHFINDER_HOME"] = str(self.home)
        self.fake_home = fake_home
        self._servers = []

    def tearDown(self):
        for server in self._servers:
            server.shutdown()
            server.server_close()
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        if self._old_pf_home is None:
            os.environ.pop("PATHFINDER_HOME", None)
        else:
            os.environ["PATHFINDER_HOME"] = self._old_pf_home
        self._tmp.cleanup()

    def serve(self, body: bytes) -> str:
        server, url = _serve(body)
        self._servers.append(server)
        return url

    @staticmethod
    def answers(*answers):
        it = iter(answers)
        return lambda prompt: next(it, "")

    def run_init(self, answers=(), launchctl=None, models_url=None, say=None):
        return init.run(self.home, ask=self.answers(*answers), say=say or (lambda *_: None),
                        launchctl=launchctl or (lambda argv: (0, "")), models_url=models_url)


class InitChecksTest(InitTestBase):
    def test_checks_run_in_order_and_stop_at_the_first_fix(self):
        lines = []
        rc = self.run_init(models_url="http://127.0.0.1:1/models", say=lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("brew install ollama", lines)
        self.assertFalse(self.home.exists())

    def test_model_pick_goes_through_config_set(self):
        url = self.serve(b'{"data": [{"id": "x-cloud"}, {"id": "bge-x"}]}')
        # model 1, embed n, attach/catalog/schedule/reminders/shell/claude_code n, launchd n
        answers = ["1", "n", "n", "n", "n", "n", "n", "n", "n"]
        self.assertEqual(self.run_init(answers=answers, models_url=url), 0)
        self.assertEqual(config.load(self.home)["models"],
                         {"root": ["ollama:x-cloud"], "summarize": ["ollama:x-cloud"]})
        types = []
        for f in sorted((self.home / "events").glob("*.jsonl")):
            for line in f.read_text().splitlines():
                types.append(json.loads(line)["type"])
        self.assertIn("config_set", types)

    def test_empty_model_list_stops(self):
        url = self.serve(b'{"data": []}')
        lines = []
        rc = self.run_init(models_url=url, say=lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("ollama signin", lines)
        self.assertFalse((self.home / "config.json").exists())

    def test_embed_model_offered_and_set(self):
        url = self.serve(b'{"data": [{"id": "x-cloud"}, {"id": "bge-x"}]}')
        answers = ["1", "y", "n", "n", "n", "n", "n", "n", "n"]
        self.assertEqual(self.run_init(answers=answers, models_url=url), 0)
        self.assertEqual(config.load(self.home)["embed_model"], "ollama:bge-x")

    def test_senses_merge_not_replace(self):
        url = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        self.home.mkdir(parents=True)  # config.set needs the directory to already exist
        config.set(self.home, "senses", {"mail": {}})
        # no embed candidate here (no "embed"/"bge" id) - straight to senses: attach y, rest n
        answers = ["1", "y", "n", "n", "n", "n", "n", "n"]
        lines = []
        self.assertEqual(self.run_init(answers=answers, models_url=url, say=lines.append), 0)
        senses = config.load(self.home)["senses"]
        self.assertIn("mail", senses)
        self.assertIn("attach", senses)
        # the announced lines, pasted into a shell, change one sense each and keep the rest
        announced = [line for line in lines if line.startswith("pathfinder config set senses.")]
        self.assertEqual(len(announced), 2)
        for line in announced:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(shlex.split(line)[1:]), 0)
        self.assertEqual(set(config.load(self.home)["senses"]), {"mail", "attach", "calendar"})


class ConfigCommandTest(InitTestBase):
    """`pathfinder config get|set`: a sense is set or switched off on its own (A1: a whole-senses
    `config set` switched seven senses off), and a refusal is one line, not a traceback."""

    def setUp(self):
        super().setUp()
        self.home.mkdir(parents=True)
        config.set(self.home, "senses", {"calendar": {}, "shell": {}})

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_config_set_sense_merges_not_replaces(self):
        mail = {"host": "imap.example.com", "user": "you@example.com"}
        self.assertEqual(self.run_cli("config", "set", "senses.mail", json.dumps(mail))[0], 0)
        self.assertEqual(config.load(self.home)["senses"], {"calendar": {}, "shell": {}, "mail": mail})

    def test_config_set_sense_off_removes_one(self):
        self.assertEqual(self.run_cli("config", "set", "senses.shell", "off")[0], 0)
        self.assertEqual(config.load(self.home)["senses"], {"calendar": {}})

    def test_config_get_prints_current_value(self):
        rc, out, _ = self.run_cli("config", "get", "senses")
        self.assertEqual((rc, json.loads(out)), (0, {"calendar": {}, "shell": {}}))
        rc, out, _ = self.run_cli("config", "get")
        self.assertEqual(json.loads(out)["senses"], {"calendar": {}, "shell": {}})

    def test_config_error_is_one_line_not_a_traceback(self):
        for argv in (("config", "set", "senses.nosuch", "{}"), ("config", "set", "senses.mail", "[]"),
                     ("config", "set", "nosuch", "1"), ("config", "get", "nosuch")):
            rc, out, err = self.run_cli(*argv)
            self.assertEqual((rc, out), (1, ""), argv)
            self.assertEqual(len(err.splitlines()), 1, argv)
            self.assertTrue(err.startswith("pathfinder: "), argv)
        self.assertEqual(config.load(self.home)["senses"], {"calendar": {}, "shell": {}})


class InitShellHookTest(InitTestBase):
    def test_shell_hook_is_appended_once(self):
        url = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        zshrc = self.fake_home / ".zshrc"
        zshrc.write_text("existing line\n")
        # model, attach n, catalog n, schedule n, reminders n, shell y, claude_code n,
        # shell-hook-ask y, launchd n
        answers = ["1", "n", "n", "n", "n", "y", "n", "y", "n"]
        self.assertEqual(self.run_init(answers=answers, models_url=url), 0)
        text = zshrc.read_text()
        # the one guarded line legitimately names the path twice (`-f` test and `source`) -
        # count lines that reference it, not raw substring occurrences.
        marker_lines = [l for l in text.splitlines() if "hooks/pathfinder.zsh" in l]
        self.assertEqual(len(marker_lines), 1)
        self.assertIn("existing line", text)
        self.assertTrue((self.home / "hooks" / "pathfinder.zsh").exists())
        backups = list(self.fake_home.glob(".zshrc.pathfinder-*"))
        self.assertEqual(len(backups), 1)

        # run again with the same answers - must not add a second line or a second backup
        url2 = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        self.assertEqual(self.run_init(answers=answers, models_url=url2), 0)
        text2 = zshrc.read_text()
        marker_lines2 = [l for l in text2.splitlines() if "hooks/pathfinder.zsh" in l]
        self.assertEqual(len(marker_lines2), 1)
        self.assertEqual(len(list(self.fake_home.glob(".zshrc.pathfinder-*"))), 1)


class InitClaudeHooksTest(InitTestBase):
    def test_zshrc_without_trailing_newline_keeps_its_last_line(self):
        """`export FOO=1` with no final newline (VS Code's default) must not become
        `export FOO=1[ -f ...` - the review reproduced zsh rejecting that line."""
        url = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        zshrc = self.fake_home / ".zshrc"
        zshrc.write_bytes(b"export FOO=1")
        answers = ["1", "n", "n", "n", "n", "y", "n", "y", "n"]
        self.assertEqual(self.run_init(answers=answers, models_url=url), 0)
        lines = zshrc.read_text().splitlines()
        self.assertEqual(lines[0], "export FOO=1")
        self.assertTrue(lines[1].startswith("[ -f "))

    def test_malformed_settings_json_is_a_fix_line_not_a_traceback(self):
        url = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        settings = self.fake_home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"hooks": {"Stop": {}}}')
        said = []
        answers = ["1", "n", "n", "n", "n", "n", "y", "y", "n"]  # claude_code y, hooks y, launchd n
        self.assertEqual(self.run_init(answers=answers, models_url=url, say=said.append), 0)
        self.assertTrue(any("fix it and run init again" in line for line in said), said)
        self.assertEqual(settings.read_text(), '{"hooks": {"Stop": {}}}')  # untouched
        self.assertEqual(list(settings.parent.glob("settings.json.pathfinder-*")), [])

    def test_zsh_snippet_matches_docs(self):
        """init installs the block docs/senses.md shows; the two must not drift."""
        doc = (Path(init.__file__).parent.parent / "docs" / "senses.md").read_text(encoding="utf-8")
        self.assertIn(init._ZSHRC_SNIPPET.strip(), doc)
        self.assertIn("zmodload zsh/datetime", init._ZSHRC_SNIPPET)  # $EPOCHSECONDS needs it
        self.assertIn("add-zsh-hook", init._ZSHRC_SNIPPET)  # never overrides the user's precmd

    def test_claude_hooks_merge_idempotently(self):
        url = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        settings_path = self.fake_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({
            "otherKey": "kept",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo unrelated"}]}]},
        }))
        # model, attach n, catalog n, schedule n, reminders n, shell n, claude_code y,
        # claude-hook-ask y, launchd n
        answers = ["1", "n", "n", "n", "n", "n", "y", "y", "n"]
        self.assertEqual(self.run_init(answers=answers, models_url=url), 0)
        data = json.loads(settings_path.read_text())
        self.assertEqual(data["otherKey"], "kept")
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertEqual(len(data["hooks"]["Notification"]), 1)

        url2 = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        self.assertEqual(self.run_init(answers=answers, models_url=url2), 0)
        data2 = json.loads(settings_path.read_text())
        self.assertEqual(len(data2["hooks"]["Stop"]), 2)
        self.assertEqual(len(data2["hooks"]["Notification"]), 1)


class InitLaunchdTest(InitTestBase):
    def test_plist_content(self):
        import sys
        url = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        calls = []

        def fake_launchctl(argv):
            calls.append(argv)
            return 0, ""

        # model, senses all n (6), launchd y
        answers = ["1", "n", "n", "n", "n", "n", "n", "y"]
        with mock.patch("pathfinder.init.is_serving", return_value=True):
            rc = self.run_init(answers=answers, launchctl=fake_launchctl, models_url=url)
        self.assertEqual(rc, 0)
        plist_path = self.fake_home / "Library" / "LaunchAgents" / "pathfinder.daemon.plist"
        with plist_path.open("rb") as f:
            plist = plistlib.load(f)
        self.assertEqual(plist["ProgramArguments"][0], sys.executable)
        path_value = plist["EnvironmentVariables"]["PATH"]
        self.assertTrue(path_value.startswith(str(Path(sys.executable).parent)))
        self.assertIn("/opt/homebrew/bin", path_value)
        self.assertEqual(plist["StandardOutPath"], str(self.home / "daemon.log"))
        self.assertIs(plist["KeepAlive"], True)
        self.assertEqual(calls[0][0], "bootstrap")
        self.assertEqual(calls[0][1], f"gui/{os.getuid()}")
        self.assertEqual(calls[0][2], str(plist_path))

    def test_second_run_changes_nothing(self):
        def hashes():
            # the ledger (events/) is an append-only audit trail - it grows by design on
            # every config.set call, even when the value does not change; idempotency here
            # is about the derived state (config.json and the foreign files), not the log.
            out = {}
            for root in (self.fake_home, self.home):
                for p in sorted(root.rglob("*")):
                    if p.is_file() and "events" not in p.relative_to(root).parts:
                        out[str(p)] = p.read_bytes()
            return out

        url = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        answers = ["1", "n", "n", "n", "n", "n", "n", "y"]
        with mock.patch("pathfinder.init.is_serving", return_value=True):
            self.assertEqual(self.run_init(answers=answers, models_url=url), 0)
        first = hashes()

        url2 = self.serve(b'{"data": [{"id": "x-cloud"}]}')
        with mock.patch("pathfinder.init.is_serving", return_value=True):
            self.assertEqual(self.run_init(answers=answers, models_url=url2), 0)
        second = hashes()
        self.assertEqual(first, second)


class ServiceTest(InitTestBase):
    def test_failed_bootstrap_is_reported(self):
        """A first run whose `launchctl bootstrap` fails must say so and exit 1, not print the
        next step as if the daemon were up (live: the old code swallowed the return code)."""
        server, url = _serve(b'{"data": [{"id": "x-cloud"}]}')
        self._servers.append(server)
        said = []
        answers = ["1", "n", "n", "n", "n", "n", "n", "y"]  # model, six senses no, launchd yes
        with mock.patch("pathfinder.init.is_serving", return_value=False), \
             mock.patch("pathfinder.init.time.sleep"):
            rc = self.run_init(answers=answers, say=said.append, models_url=url,
                               launchctl=lambda argv: (78, "Bootstrap failed: 5: Input/output error"))
        self.assertEqual(rc, 1)
        self.assertTrue(any(line.startswith("launchd:") and "Input/output error" in line for line in said), said)
        self.assertFalse(any(line == "pathfinder tui" for line in said))

    def test_service_status_without_launchd(self):
        rc = init.service(self.home, "status", launchctl=lambda argv: (1, "could not find service"))
        self.assertEqual(rc, 1)

    def test_service_actions_call_launchctl(self):
        for action in ("start", "stop", "restart"):
            calls = []

            def record(argv):
                calls.append(argv)
                return 0, ""

            init.service(self.home, action, launchctl=record)
            self.assertEqual(len(calls), 1)
            if action == "start":
                self.assertEqual(calls[0][0], "bootstrap")
            elif action == "stop":
                self.assertEqual(calls[0], ["bootout", f"gui/{os.getuid()}/pathfinder.daemon"])
            else:
                self.assertEqual(calls[0], ["kickstart", "-k", f"gui/{os.getuid()}/pathfinder.daemon"])


class NoModelNameTest(unittest.TestCase):
    def test_no_model_name_in_init(self):
        text = (Path(__file__).resolve().parent.parent / "pathfinder" / "init.py").read_text().lower()
        for family in _MODEL_FAMILIES:
            self.assertNotRegex(text, r"\b" + family, f"init.py names the model family {family!r}")


if __name__ == "__main__":
    unittest.main()
