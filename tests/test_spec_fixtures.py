"""docs/spec/fixtures/*.json against the connection spec v0 (docs/spec/connection-v0.md):
every fixture is a candidate shaped exactly like `catalog.SPEC_FIELDS`."""
import json
import unittest
from pathlib import Path

from pathfinder import catalog

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "docs" / "spec" / "fixtures"

OUTCOMES = {"proposed": "proposed", "installed": "installed", "install_failed": "install_failed"}


class SpecFixtures(unittest.TestCase):
    def test_fixtures_match_spec_fields(self):
        for stem in OUTCOMES:
            with self.subTest(fixture=stem):
                data = json.loads((FIXTURES_DIR / f"{stem}.json").read_text(encoding="utf-8"))
                self.assertEqual(set(data.keys()), set(catalog.SPEC_FIELDS),
                                 f"{stem}.json has keys outside catalog.SPEC_FIELDS")

    def test_fixtures_have_the_named_outcome(self):
        for stem, outcome in OUTCOMES.items():
            with self.subTest(fixture=stem):
                data = json.loads((FIXTURES_DIR / f"{stem}.json").read_text(encoding="utf-8"))
                self.assertIn(data["outcome"], (
                    "proposed", "approved", "rejected", "installed", "install_failed", "uninstalled"))
                self.assertEqual(data["outcome"], outcome)

    def test_malicious_scan_never_checked(self):
        for stem in OUTCOMES:
            with self.subTest(fixture=stem):
                data = json.loads((FIXTURES_DIR / f"{stem}.json").read_text(encoding="utf-8"))
                self.assertEqual(data["verification"]["malicious_scan"], "not_checked")


if __name__ == "__main__":
    unittest.main()
