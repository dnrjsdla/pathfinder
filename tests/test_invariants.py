"""Invariants that are properties of the source, not of one function (blueprint section 11)."""
import importlib.util
import re
import unittest
from pathlib import Path

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
        """A leading underscore (`_eventkit.py`) marks an adapter shared by senses, not a sense
        itself - outside the budget, same as `__init__.py`."""
        for p in (SRC / "senses").glob("*.py"):
            if p.name.startswith("_"):
                continue
            n = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(n, 150, f"{p.name} is {n} lines, over the sense budget of 150")

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


if __name__ == "__main__":
    unittest.main()
