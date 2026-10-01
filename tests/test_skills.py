"""Skills (D58, D61): the seed copy, the index and `read_skill`, on a temp home - no daemon."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import skills
from pathfinder import tools as tools_mod


def _skill(name, description, body="Do the thing.\n"):
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.seeds = Path(self.tmp.name) / "seeds"
        self.seeds.mkdir()
        with mock.patch.object(skills, "SEEDS", self.seeds):
            skills.bind(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        (self.home / "skills" / f"{name}.md").write_text(text, encoding="utf-8")

    def test_bind_copies_missing_seeds_and_leaves_edited_ones(self):
        (self.seeds / "a.md").write_text(_skill("a", "Use for a."), encoding="utf-8")
        (self.seeds / "b.md").write_text(_skill("b", "Use for b."), encoding="utf-8")
        self.write("b", _skill("b", "The owner's own words."))
        with mock.patch.object(skills, "SEEDS", self.seeds):
            skills.bind(self.home)
        folder = self.home / "skills"
        self.assertEqual((folder / "a.md").read_text(encoding="utf-8"), _skill("a", "Use for a."))
        self.assertEqual((folder / "b.md").read_text(encoding="utf-8"), _skill("b", "The owner's own words."))
        self.assertEqual((folder / "a.md").stat().st_mode & 0o777, 0o600)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        (folder / "a.md").unlink()  # a deleted seed comes back at the next start, as RULES.md does
        with mock.patch.object(skills, "SEEDS", self.seeds):
            skills.bind(self.home)
        self.assertTrue((folder / "a.md").exists())

    def test_index_is_sorted_and_bounded(self):
        self.write("b-x", _skill("b-x", "Use for b-x."))
        self.write("a-b", _skill("a-b", "Use for a-b."))
        self.write("a", _skill("a", "Use for a."))
        self.write("long", _skill("long", "x" * 161))
        self.write("other", _skill("not-other", "Use for other."))
        self.write("Upper", _skill("Upper", "Use for Upper."))
        with self.assertLogs("pathfinder.skills", "WARNING") as logs:
            text = skills.index(self.home)
        self.assertEqual(text, "- a: Use for a.\n- a-b: Use for a-b.\n- b-x: Use for b-x.")
        self.assertEqual(len(logs.output), 3)
        self.assertEqual(skills.index(self.home), text)  # cached: the same bytes, no new warning
        for i in range(25):  # 25 lines of ~167 characters pass 4,000: the whole index is refused
            self.write(f"s{i:02d}", _skill(f"s{i:02d}", "y" * 160))
        with self.assertLogs("pathfinder.skills", "WARNING") as logs:
            self.assertEqual(skills.index(self.home), "")
        self.assertIn("over 4000", logs.output[-1])

    def test_a_skill_with_a_token_is_not_loaded(self):
        token = "gh" + "p_" + "A1b2C3d4E5" * 4
        self.write("leaky", _skill("leaky", "Use for leaks.", f"Paste {token} here.\n"))
        self.write("fine", _skill("fine", "Use for fine."))
        with self.assertLogs("pathfinder.skills", "WARNING") as logs:
            self.assertEqual(skills.index(self.home), "- fine: Use for fine.")
        self.assertIn("token-shaped", logs.output[0])
        self.assertNotIn(token, "".join(logs.output))
        with self.assertRaises(ValueError) as cm:
            skills._read_skill("leaky")
        self.assertNotIn(token, str(cm.exception))

    def test_read_skill_returns_header_and_clipped_body(self):
        self.write("big", _skill("big", "Use for big.", "z" * (tools_mod.READ_CLIP * 2)))
        out = skills._read_skill("big")
        header, body = out.split("\n", 1)
        self.assertEqual(header, "[Skill 'big' - a procedure the owner approved; follow it. "
                                 "Secrets go through /secret, never the chat.]")
        self.assertIn("characters omitted", body)
        self.assertLessEqual(len(body), tools_mod.READ_CLIP + 60)
        for name in ("missing", "../big", "Big"):
            with self.assertRaises(ValueError) as cm:
                skills._read_skill(name)
            self.assertIn("no skill named", str(cm.exception))

    def test_read_skill_description_carries_the_index(self):
        spec = tools_mod.TOOLS["read_skill"]
        self.assertIn("read_skill", tools_mod.for_origin("user"))
        self.assertIn("read_skill", tools_mod.for_origin("job"))
        self.assertNotIn("read_skill", tools_mod.for_origin("signal"))
        self.assertNotIn("read_skill", tools_mod.for_origin("memory"))
        self.assertFalse(spec.write or spec.network or spec.sandboxed)  # read-only, no gate
        self.write("a", _skill("a", "Use for a."))
        first = spec.schema()["function"]["description"]
        self.assertTrue(first.startswith(spec.description))
        self.assertTrue(first.endswith("\n- a: Use for a."))
        self.assertEqual(spec.schema()["function"]["description"], first)  # byte-stable
        self.write("b", _skill("b", "Use for b."))
        self.assertTrue(spec.schema()["function"]["description"].endswith("- a: Use for a.\n- b: Use for b."))


if __name__ == "__main__":
    unittest.main()
