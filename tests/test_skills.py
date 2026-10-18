"""Skills (D58, D61): the seed copy, the index and `read_skill`, on a temp home - no daemon."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import skills
from pathfinder import tools as tools_mod
from pathfinder.broker import Broker, Gate
from pathfinder.mcp import attach as mcp_attach


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
        (folder / "b.md").unlink()
        (folder / "b.md").symlink_to(self.home / "elsewhere.md")  # dangling: O_EXCL never writes through it
        with mock.patch.object(skills, "SEEDS", self.seeds):
            skills.bind(self.home)
        self.assertFalse((self.home / "elsewhere.md").exists())

    def test_index_is_sorted_and_bounded(self):
        self.write("b-x", _skill("b-x", "Use for b-x."))
        self.write("a-b", _skill("a-b", "Use for a-b."))
        self.write("a", _skill("a", "Use for a."))
        self.write("long", _skill("long", "x" * 161))
        self.write("other", _skill("not-other", "Use for other."))
        self.write("Upper", _skill("Upper", "Use for Upper."))
        (self.home / "linked.md").write_text(_skill("linked", "Use for linked."), encoding="utf-8")
        (self.home / "skills" / "linked.md").symlink_to(self.home / "linked.md")
        with self.assertLogs("pathfinder.skills", "WARNING") as logs:
            text = skills.index(self.home)
        self.assertEqual(text, "- a: Use for a.\n- a-b: Use for a-b.\n- b-x: Use for b-x.")
        self.assertEqual(len(logs.output), 4)
        with self.assertNoLogs("pathfinder.skills", "WARNING"):  # the same problems: warned once, not per request
            self.assertEqual(skills.index(self.home), text)
        self.write("a", _skill("a", "Edited in place."))  # the directory's mtime does not move
        self.assertTrue(skills.index(self.home).startswith("- a: Edited in place.\n"))
        for i in range(25):  # 25 lines of ~167 characters pass 4,000: the whole index is refused
            self.write(f"s{i:02d}", _skill(f"s{i:02d}", "y" * 160))
        with self.assertLogs("pathfinder.skills", "WARNING") as logs:
            self.assertEqual(skills.index(self.home),
                             "(index too large: 28 skills not listed - shorten their descriptions)")
        self.assertEqual(len(logs.output), 1)  # only the new problem; the four old ones are not repeated
        self.assertIn("over 4000", logs.output[0])

    def test_a_refused_file_reaches_the_ledger_as_an_error_event(self):
        events = []  # the daemon's own registration: mcp/attach.py's handler, no handler of the test's
        attach = mcp_attach.Attach(self.home, None, lambda type, **f: events.append((type, f["message"])),
                                   None, lambda fn: fn())
        try:
            self.write("bad", _skill("not-bad", "Use for bad."))
            skills.index(self.home)  # not under assertLogs: it swaps the logger's handlers out
        finally:
            attach.close()
        self.assertEqual(events, [("error", "skill bad.md is not loaded: its name must equal the file's "
                                            "stem (lowercase, digits, hyphens)")])

    def test_schema_builds_before_bind(self):
        with mock.patch.object(skills, "_home", None):  # bench/m10 copies the specs with no daemon
            description = tools_mod.TOOLS["read_skill"].schema()["function"]["description"]
        self.assertTrue(description.endswith("\n(none yet)"))

    def test_write_file_cannot_write_a_skill(self):
        spec = tools_mod.TOOLS["write_file"]
        for origin, grants in (("user", {"write_file": "allow"}), ("signal", {}), ("job", {})):
            for path in (self.home / "skills" / "x.md", self.home / "skills" / "new" / ".." / "y.md"):
                args = {"path": str(path), "content": _skill("x", "Use for x.")}
                broker = Broker({"write_file": spec}, origin, grants, control=None)
                if origin != "user":
                    with self.assertRaises(Gate) as cm:  # policy asks first; the owner says yes
                        broker.call("c1", "write_file", args)
                    broker = Broker({"write_file": spec}, origin, grants, control=None,
                                    approved={"c1": ("write_file", cm.exception.arguments)})
                out = broker.call("c1", "write_file", args)
                self.assertFalse(out["ok"], (origin, path))
                self.assertIn("a skill is written only by the owner", out["error"])
        self.assertEqual(sorted(p.name for p in (self.home / "skills").glob("*.md")), [])
        seed = _skill("a", "Use for a.")
        self.write("a", seed)
        link = self.home / "workspace" / "l.md"  # what a user-turn shell can do in the workspace
        link.parent.mkdir()
        link.symlink_to(self.home / "skills" / "a.md")
        out = Broker({"write_file": spec}, "user", {"write_file": "allow"}, control=None
                     ).call("c3", "write_file", {"path": str(link), "content": "overwritten"})
        self.assertIn("a skill is written only by the owner", out["error"])
        for path in (link, self.home / "skills" / "new" / ".." / "b.md"):  # the handler resolves too, no gate in front
            with self.assertRaises(ValueError):
                spec.handler(path=str(path), content="overwritten")
        self.assertFalse((self.home / "skills" / "new").exists())  # a refused write makes no directory
        self.assertEqual((self.home / "skills" / "a.md").read_text(encoding="utf-8"), seed)
        with self.assertRaises(Gate) as cm:  # no grant: the approval line names the target, not the link
            Broker({"write_file": spec}, "user", {}, control=None
                   ).call("c4", "write_file", {"path": str(link), "content": "overwritten"})
        self.assertEqual(cm.exception.arguments["path"], str((self.home / "skills" / "a.md").resolve()))
        out = Broker({"write_file": spec}, "user", {"write_file": "allow"}, control=None
                     ).call("c2", "write_file", {"path": str(self.home / "notes.md"), "content": "ok"})
        self.assertTrue(out["ok"])  # anywhere else it writes as before

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

    def test_connect_gmail_is_shipped_and_parses(self):
        seed = skills.SEEDS / "connect-gmail.md"
        description, body = skills._load(seed)  # frontmatter valid, no token-shaped string
        self.assertEqual(description, "Use when the owner asks to connect Gmail or read their mail.")
        self.assertNotRegex(seed.read_text(encoding="utf-8"), r"[\w.+-]+@[\w-]+\.\w")  # no address
        skills.bind(self.home)  # the real seeds
        self.assertEqual((self.home / "skills" / "connect-gmail.md").read_text(encoding="utf-8"),
                         seed.read_text(encoding="utf-8"))
        self.assertIn(f"- connect-gmail: {description}", skills.index(self.home).splitlines())
        out = skills._read_skill("connect-gmail")
        self.assertTrue(out.startswith("[Skill 'connect-gmail' - a procedure the owner approved"))
        self.assertIn("/secret mail", out)
        self.assertIn("myaccount.google.com/apppasswords", out)


if __name__ == "__main__":
    unittest.main()
