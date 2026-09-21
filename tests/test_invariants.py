"""Invariants that are properties of the source, not of one function (blueprint section 11)."""
import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "pathfinder"


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
        day_maths = r"%Y-%m-%d|strftime|isoformat\(|\.date\(\)|fromtimestamp|timedelta"
        for name, text in sources().items():
            hits = re.findall(day_maths, text)
            allowed = 3 if name == "ledger.py" else 0  # the one line in partition(): fromtimestamp, strftime, its format
            self.assertLessEqual(len(hits), allowed, f"{name} computes dates ({hits}); only ledger.partition() may")

    def test_no_model_ids_or_tcp_in_source(self):
        for name, text in sources().items():
            # \b: the provider key "ollama" is configuration, the model family "llama" would not be
            self.assertNotRegex(text, r"\b(gpt-|claude-|gemma|nemotron|llama|qwen)", f"{name} hardcodes a model name")
            self.assertNotRegex(text, r"start_server\(|AF_INET", f"{name} opens TCP; the daemon is unix-socket only")


if __name__ == "__main__":
    unittest.main()
