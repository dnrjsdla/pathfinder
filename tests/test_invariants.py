"""Invariants that are properties of the source, not of one function (blueprint section 11)."""
import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pathfinder.ledger import Ledger
from pathfinder.state import State

SRC = Path(__file__).resolve().parent.parent / "pathfinder"
REPO_ROOT = SRC.parent


def sources():
    return {p.relative_to(SRC).as_posix(): p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")}


class SourceInvariants(unittest.TestCase):
    def test_single_writer(self):
        """One ledger writer, one partition function, one config writer. Mark.5 died at the
        boundary between two modules that each defined "the day" - so count the places."""
        for name, text in sources().items():
            if name != "ledger.py":
                self.assertNotIn('"events"', text, f"{name} names the events directory; only ledger.py may")
                self.assertNotRegex(text, r"\.jsonl", f"{name} touches ledger files; only ledger.py may")
            if name != "config.py":
                self.assertNotIn("config.json", text, f"{name} names config.json; only config.py may")
        ledger = sources()["ledger.py"]
        self.assertEqual(len(re.findall(r"^def partition\(", ledger, re.M)), 1)
        # A date computed anywhere else is a second definition of "the day" - the exact
        # boundary bug that killed the predecessor. Scan the WHOLE package, not just the ledger.
        # Partition 3: clock.py becomes the one place a *local* calendar is computed, so it is
        # unlimited; ledger.py keeps its allowance of 3 (partition()'s one line); every other
        # module stays at 0.
        day_maths = r"%Y-%m-%d|strftime|isoformat\(|\.date\(\)|fromtimestamp|timedelta"
        for name, text in sources().items():
            if name == "clock.py":
                continue
            hits = re.findall(day_maths, text)
            allowed = 3 if name == "ledger.py" else 0
            self.assertLessEqual(len(hits), allowed,
                                 f"{name} computes dates ({hits}); only ledger.partition() and clock.py may")

    def test_no_model_ids_or_tcp_in_source(self):
        for name, text in sources().items():
            # \b: the provider key "ollama" is configuration, the model family "llama" would not be
            self.assertNotRegex(text, r"\b(gpt-|claude-|gemma|nemotron|llama|qwen)", f"{name} hardcodes a model name")
            self.assertNotRegex(text, r"start_server\(|AF_INET", f"{name} opens TCP; the daemon is unix-socket only")

    def test_sense_files_under_150_lines(self):
        """A leading underscore (`_eventkit.py`, `_imap.py`) marks an adapter shared by senses,
        not a sense itself: its ceiling is 250 (partition 8, U17)."""
        for p in (SRC / "senses").glob("*.py"):
            ceiling = 250 if p.name.startswith("_") else 150
            n = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(n, ceiling, f"{p.name} is {n} lines, over its ceiling of {ceiling}")

    def test_jobs_module_under_350_lines(self):
        n = len((SRC / "jobs.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 350, f"jobs.py is {n} lines, over the partition-4 budget of 350")

    def test_memory_module_under_780_lines(self):
        # 780 = the blueprint's 1.3x line over the partition-5 budget of 600 - the overage is
        # reported in HISTORY.md.
        n = len((SRC / "memory.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 780, f"memory.py is {n} lines, over the 1.3x line of 780")

    def test_daemon_module_under_780_lines(self):
        n = len((SRC / "daemon.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 780, f"daemon.py is {n} lines, over the partition-5 hard line of 780")

    def test_rules_imports_no_llm_at_top_level(self):
        """rules.judge() is where the model call lands (D13); nothing else in the module may
        import an llm module - a live model call must not hide behind decide()'s cheap path."""
        for line in sources()["rules.py"].splitlines():
            if line.startswith(("import ", "from ")):
                self.assertNotIn("llm", line, f"rules.py imports llm at module level: {line!r}")

    def test_catalog_module_under_300_lines(self):
        n = len((SRC / "catalog.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 300, f"catalog.py is {n} lines, over the partition-6 budget of 300")

    def test_installer_module_under_700_lines(self):
        """installer.py is being written by a concurrent unit of this same partition; skip until
        it lands rather than fail on a file that is not this test's to write."""
        installer = SRC / "installer.py"
        if not installer.exists():
            self.skipTest("installer.py not landed yet")
        n = len(installer.read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 700, f"installer.py is {n} lines, over the partition-6 budget of 700")

    def test_init_module_under_300_lines(self):
        n = len((SRC / "init.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 300, f"init.py is {n} lines, over the partition-7 ceiling of 300")

    def test_mcp_attach_module_under_120_lines(self):
        n = len((SRC / "mcp" / "attach.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 120, f"mcp/attach.py is {n} lines, over the partition-6 budget of 120")

    def test_tui_module_under_400_lines(self):
        n = len((SRC / "tui.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 400, f"tui.py is {n} lines, over the partition-7 budget of 400")

    def test_tui_secret_module_under_150_lines(self):
        n = len((SRC / "tui_secret.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 150, f"tui_secret.py is {n} lines, over the D64 ceiling of 150")

    def test_skills_module_under_120_lines(self):
        n = len((SRC / "skills.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 120, f"skills.py is {n} lines, over the D61 ceiling of 120")

    def test_module_ceilings(self):
        """D49's gate for the rows that had no test (partition 8, U17): 1.3x the blueprint §6
        allocation for the core rows, D56's 200 for mirror.py, §6's allocation for llm/ and mcp/.
        A `+` row is one ceiling over the files together."""
        ceilings = {"state.py": 780, "loop.py": 520, "ledger.py": 520, "tools.py": 390, "exec.py": 390,
                    "deliver.py": 325, "broker.py+policy.py": 455, "signals.py+rules.py": 585,
                    "mirror.py": 200, "llm/**/*.py": 600, "mcp/**/*.py": 1858}
        for row, ceiling in ceilings.items():
            with self.subTest(row):
                files = [list(SRC.glob(part)) for part in row.split("+")]
                self.assertTrue(all(files), f"{row} matches no file; a renamed module keeps its ceiling")
                n = sum(len(p.read_text(encoding="utf-8").splitlines()) for ps in files for p in ps)
                self.assertLessEqual(n, ceiling, f"{row} is {n} lines, over its ceiling of {ceiling}")

    def test_core_under_5500_lines(self):
        """D49: the core is the blueprint §6 rows, capped at 5,500 together."""
        core = ["ledger", "state", "daemon", "loop", "broker", "policy", "exec", "tools", "signals", "rules",
                "deliver", "jobs", "memory"]
        n = sum(len((SRC / f"{m}.py").read_text(encoding="utf-8").splitlines()) for m in core)
        self.assertLessEqual(n, 5500, f"the core is {n} lines, over the §6 cap of 5,500")

    def test_no_new_ledger_type(self):
        """Partition 8 adds two fields (`assistant.withdrawn`, `turn_end.check`) and no type."""
        from pathfinder import ledger
        self.assertEqual(ledger.TYPES, frozenset({
            "session_start", "user", "assistant", "tool_call", "tool_result",
            "turn_start", "turn_end", "error", "config_set",
            "approval_pending", "approval", "interrupted",
            "signal", "decision", "judgment", "delivery", "reaction", "presence",
            "job_start", "job_done", "job_failed",
            "memory_pass", "memory_apply", "session_summary",
            "pin_accept", "pin_revoke",
            "install_step", "install_done", "install_failed", "uninstalled"}), "a new ledger type needs a decision")

    def test_config_json_written_only_by_config_set(self):
        """test_single_writer keeps config.json inside config.py; inside it, only set() writes it."""
        import ast
        src = sources()["config.py"]
        for fn in ast.parse(src).body:
            if isinstance(fn, ast.FunctionDef) and fn.name != "set":
                body = ast.get_source_segment(src, fn)
                if "config.json" in body:
                    self.assertNotRegex(body, r"os\.replace|write_text|write_bytes|_atomic_write_json|merge_json_file"
                                              r"|open\(.*['\"][wax]b?\+?['\"]", f"config.{fn.name} writes config.json")

    def test_readme_pair_has_equal_heading_counts(self):
        """D6: README.md and README.ko.md are one document in two languages (same headings)."""
        root = SRC.parent
        en = [l for l in (root / "README.md").read_text(encoding="utf-8").splitlines() if l.startswith("#")]
        ko = [l for l in (root / "README.ko.md").read_text(encoding="utf-8").splitlines() if l.startswith("#")]
        self.assertEqual(len(en), len(ko), f"README headings: en {len(en)} vs ko {len(ko)}")

    def test_presence_has_a_producer(self):
        """The deliver state machine's typing/away branches had no live source from partition 3
        to 6; `tui.py` is that source and must keep sending `presence`."""
        src = (SRC / "tui.py").read_text(encoding="utf-8")
        self.assertIn('{"type": "presence", "idle_s": 0}', src)
        self.assertIn('"type": "presence", "idle_s": min(idle', src)  # capped at the daemon's ceiling

    def test_only_catalog_names_the_registry_url(self):
        """`catalog.REGISTRY` is the one place the registry URL is written (docs/spec/connection-v0.md)."""
        from pathfinder import catalog
        for name, text in sources().items():
            if name == "catalog.py":
                continue
            self.assertNotIn(catalog.REGISTRY, text,
                             f"{name} hardcodes the registry URL; only catalog.py may")


class HistoryInvariants(unittest.TestCase):
    def test_no_tool_record_without_tool_call_events(self):
        """A ledger with no tool_call/tool_result/approval_pending/approval - even an assistant
        message copying a record line - gives a history with no record message."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger, state = Ledger(Path(tmp)), State(Path(tmp) / "state.db")
            try:
                for type, kw in [("session_start", {}),
                                 ("user", {"origin": "user", "trust": "user", "text": "did you send it?"}),
                                 ("assistant", {"final": True, "text": 'send_mail {"to":"a"} ok'})]:
                    state.apply(ledger.append(type, session="s", turn="t", **kw))
                self.assertEqual([m["role"] for m in state.history("s")], ["user", "assistant"])
            finally:
                state.close()


class SignalEnvelopeInvariants(unittest.TestCase):
    """m3 (HISTORY.md) measured that this exact wording keeps the injection defense at 0/20;
    the package's constants must stay byte-identical to the bench that measured them."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("m3_injection", REPO_ROOT / "bench" / "m3_injection.py")
        cls.bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bench)

    def test_envelope_and_clause_match_the_bench(self):
        from pathfinder import signals
        self.assertEqual(signals.SIGNAL_ENVELOPE, self.bench.SIGNAL_ENVELOPE)
        self.assertEqual(signals.SIGNAL_TURNS_CLAUSE, self.bench.SIGNAL_TURNS_CLAUSE)


class SecondLookInvariants(unittest.TestCase):
    """m10 (HISTORY.md) measured the second look and the prompt; both stay what it measured."""

    # The four lines U8 added to bench/m10/system.txt, exactly as they sit in the prompt.
    ADDITIONS = (
        "This is about the world outside this conversation: a question about the conversation itself - a\n"
        "recap, a summary, what you said or did earlier, how you work - is answered from the conversation and\n"
        "the record of your tool calls, without a new tool call.\n",
        "Before your answers, the system notes the tools you called in earlier turns - names and arguments\n"
        "from the ledger, never their results. Use it to say what you did; it is not the current state.\n\n",
        "Never ask for a token, key or password in chat. A secret goes in with /secret <server> <NAME>, and\n"
        "you never see it.\n",
        "USER.md holds the user's standing instructions; follow them in every turn.\n")

    def test_second_look_matches_the_bench(self):
        from pathfinder import loop
        spec = importlib.util.spec_from_file_location("m10_unsupported", REPO_ROOT / "bench" / "m10_unsupported.py")
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        for name in ("VERIFIER_PROMPT", "RERUN_NOTE", "FLAG_WORDS"):
            self.assertEqual(getattr(loop, name), getattr(bench, name), f"loop.{name} drifted from the bench")
        agenda = loop.AGENDA_HEADER + "\n- 09:00 dentist"
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."},
                   {"role": "user", "content": "what is on today?"}]
        messages = ([{"role": "system", "content": "prompt"}, {"role": "system", "content": "Now: x\n\n" + agenda}]
                    + history + [{"role": "assistant", "content": "Nothing today."}])
        tools = ["list_events", "list_reminders"]
        self.assertEqual(loop.verifier_input(tools, messages), bench.verifier_input(tools, messages, history, agenda))
        prompt = (SRC / "prompts" / "system.txt").read_text(encoding="utf-8")
        for added in self.ADDITIONS:
            self.assertIn(added, prompt)
            prompt = prompt.replace(added, "", 1)
        self.assertEqual(prompt, (REPO_ROOT / "bench" / "m10" / "system.txt").read_text(encoding="utf-8"),
                         "system.txt has a line that is neither bench/m10/system.txt nor in ADDITIONS: m10 did "
                         "not measure it; add it to ADDITIONS if it is meant to be there")


class HomeInvariants(unittest.TestCase):
    def test_home_stays_private(self):
        """A pre-partition home (0644 files, 0755 dirs) through the daemon start path: nothing under
        it, `workspace/` aside, keeps a group or other bit."""
        from pathfinder import __main__ as cli
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"PATHFINDER_HOME": tmp}), \
                mock.patch.object(cli, "Daemon", **{"return_value.serve": mock.AsyncMock()}):
            home = Path(tmp)
            for rel in ("events/0.jsonl", "events/.lock", "hooks/pathfinder.zsh", "state.db", "daemon.log",
                        "workspace/mine.txt"):
                (home / rel).parent.mkdir(exist_ok=True)
                (home / rel).write_text("x")
            for p in [home, *home.rglob("*")]:
                os.chmod(p, 0o755 if p.is_dir() else 0o644)
            self.assertEqual(cli.main(["daemon"]), 0)
            loose = [p for p in [home, *home.rglob("*")]
                     if p.stat().st_mode & 0o077 and "workspace" not in p.relative_to(home).parts]
            self.assertEqual(loose, [])
            self.assertEqual((home / "workspace" / "mine.txt").stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
