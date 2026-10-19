"""`memory.Manager` end to end - no daemon, no socket, no network. `record` is the test's own,
backed by a real Ledger+State on a temp home."""
import contextlib
import io
import tempfile
import threading
import types
import unittest
from unittest import mock
from pathlib import Path

from pathfinder import __main__ as cli
from pathfinder import config, memory, signals, skills
from pathfinder import rules as rules_mod
from pathfinder import tools as tools_mod
from pathfinder.ledger import Ledger
from pathfinder.state import State

USER_MD = "---\nwaiting_for: []\nsubject_alerts: []\nnoise_domains: []\n---\n"
RULES_MD = "---\nconsolidate_min_events: 1\n---\n\n## job_done\n\n| always | - | silence | |\n"


def _fake_sense(kinds):
    return types.SimpleNamespace(conditions={}, kinds=kinds)


class MemoryHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "USER.md").write_text(USER_MD, encoding="utf-8")
        (self.home / "RULES.md").write_text(RULES_MD, encoding="utf-8")
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self._managers = []

    def tearDown(self):
        for mgr in self._managers:
            mgr.close()
        self.state.close()
        self.tmp.cleanup()

    def record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        return event

    def manager(self, senses=None):
        senses = senses if senses is not None else [_fake_sense({"job_done": None})]
        mgr = memory.Manager(self.home, self.record, senses)
        self._managers.append(mgr)
        memory.bind(mgr)  # the tool handlers (edit_memory, read_memory, ...) call the bound Manager
        return mgr

    def events(self, type=None):
        evs = list(self.ledger.read())
        return [e for e in evs if type is None or e["type"] == type]


class VaultPathTest(unittest.TestCase):
    def test_vault_path_refuses_outside(self):
        for bad in ("../x.md", "/etc/passwd", "wiki/a/b.md", "notes/x.md", "wiki/x.txt"):
            with self.assertRaises(ValueError):
                memory.vault_path("/tmp/home", bad)


class GateTest(MemoryHarness):
    def test_vet_refuses_unknown_rule_word(self):
        mgr = self.manager()
        proposal, error = mgr.vet("RULES.md", "replace", "| always | - | silence | |",
                                  "| frobnicate | 1 | speak | note |", [], "user", {}, "s1")
        self.assertIsNone(proposal)
        self.assertIn("frobnicate", error)
        self.assertEqual((self.home / "RULES.md").read_text(encoding="utf-8"), RULES_MD)

    def test_vet_replace_needs_exactly_one_line(self):
        mgr = self.manager()
        (self.home / "wiki").mkdir()
        (self.home / "wiki" / "Coffee.md").write_text("# Coffee\nline one\nline one\n", encoding="utf-8")
        proposal, error = mgr.vet("wiki/Coffee.md", "replace", "not here", "new", [], "memory", {}, "")
        self.assertIsNone(proposal)
        self.assertIn("does not match", error)
        proposal, error = mgr.vet("wiki/Coffee.md", "replace", "line one", "new", [], "memory", {}, "")
        self.assertIsNone(proposal)
        self.assertIn("more than one", error)

    def test_user_facts_need_a_user_or_reaction_line(self):
        mgr = self.manager()
        allowed = {
            10: {"seq": 10, "type": "assistant", "origin": "user"},  # origin "user" but not the user's words
            11: {"seq": 11, "type": "reaction", "origin": "user"},
            12: {"seq": 12, "type": "user", "origin": "user"},
        }
        proposal, error = mgr.vet("USER.md", "add", "", "- likes coffee", [10], "memory", allowed, "")
        self.assertIsNone(proposal)
        self.assertIn("user's own words", error)
        proposal, error = mgr.vet("USER.md", "add", "", "- likes coffee", [11], "memory", allowed, "")
        self.assertIsNone(error)
        self.assertEqual(proposal["evidence"], [11])
        proposal, error = mgr.vet("USER.md", "add", "", "- likes coffee", [12], "memory", allowed, "")
        self.assertIsNone(error)
        self.assertEqual(proposal["evidence"], [12])

    def test_vet_checks_every_section_with_the_full_vocabulary(self):
        """RULES.md's vocabulary comes from every module under `pathfinder/senses/`, not from
        which senses this home has turned on - a Manager built with none loaded still knows the
        `mail` sense's `sender_in` word."""
        mgr = self.manager(senses=[])
        allowed = {1: {"seq": 1, "type": "signal", "origin": "signal"}}
        proposal, error = mgr.vet("RULES.md", "add", "", "| sender_in | USER.waiting_for | speak | |",
                                  [1], "memory", allowed, "")
        self.assertIsNone(error)
        proposal, error = mgr.vet("RULES.md", "add", "", "| frobnicate | 1 | speak | |",
                                  [1], "memory", allowed, "")
        self.assertIsNone(proposal)
        self.assertIn("frobnicate", error)

    def test_rules_frontmatter_is_not_editable(self):
        mgr = self.manager()
        allowed = {1: {"seq": 1, "type": "signal", "origin": "signal"}}
        proposal, error = mgr.vet("RULES.md", "replace", "consolidate_min_events: 1",
                                  "consolidate_min_events: 2", [1], "memory", allowed, "")
        self.assertIsNone(proposal)
        self.assertIn("frontmatter", error)
        self.assertEqual((self.home / "RULES.md").read_text(encoding="utf-8"), RULES_MD)

    def test_replace_tolerates_a_retyped_line(self):
        mgr = self.manager()
        (self.home / "wiki").mkdir()
        (self.home / "wiki" / "Coffee.md").write_text(
            "# Coffee\nDon't forget to stop the streaming.\n", encoding="utf-8")
        allowed = {1: {"seq": 1, "type": "signal", "origin": "signal"}}
        proposal, error = mgr.vet(
            "wiki/Coffee.md", "replace", "Don't forget to stop the streming.",
            "Don't forget to stop the recording.", [1], "memory", allowed, "")
        self.assertIsNone(error)
        reason = mgr.apply(proposal)
        self.assertEqual(reason, "")
        text = (self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8")
        self.assertIn("Don't forget to stop the recording.", text)
        self.assertNotIn("streaming", text)


class ApplyTest(MemoryHarness):
    def test_edit_memory_refusal_is_a_failed_call(self):
        """U23: a vet refusal raises, so the model gets `ok: false` for an edit that did not happen."""
        from pathfinder.broker import Broker
        self.manager()
        broker = Broker(memory.TOOLS, "user", {"grants": {"edit_memory": "allow"}}, session_grants=set, control=None)
        out = broker.call("c1", "edit_memory", {"target": "USER.md", "op": "replace", "old": "- no such line", "new": "x"})
        self.assertEqual(out, {"ok": False, "error": "old does not match any line in the file"})

    def test_wiki_applies_at_once(self):
        mgr = self.manager()
        control = types.SimpleNamespace()
        p = {"since": 0, "upto": 5, "lines": [], "allowed": {5: {"seq": 5, "origin": "user"}},
            "read": 0, "applied": 0, "proposed": 0, "pages": [], "session": "s1"}
        control.memory_pass, control.session = p, "s1"
        result = mgr.edit("wiki/Coffee.md", "add", "", "# Coffee\nCoffee is good.", [5], control)
        self.assertEqual(result, "applied")
        self.assertEqual((self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8"),
                         "# Coffee\nCoffee is good.")
        self.assertEqual(len(self.events("memory_apply")), 1)
        self.assertEqual((p["applied"], p["pages"]), (1, ["wiki/Coffee.md"]))
        self.assertEqual([pg["path"] for pg in mgr.state.pages()], ["wiki/Coffee.md"])

    def test_user_and_rules_go_to_the_box(self):
        mgr = self.manager()
        self.record("session_start", session="s1")
        self.record("user", session="s1", turn="t1", origin="user", trust="user", text="I like coffee")
        control = types.SimpleNamespace()
        control.session = "s1"

        result = mgr.edit("USER.md", "add", "", "- likes coffee", [], control)
        self.assertTrue(result.startswith("queued for your approval as "))
        approval_id = result.rsplit(" ", 1)[-1]
        self.assertNotIn("likes coffee", (self.home / "USER.md").read_text(encoding="utf-8"))
        pending = self.state.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tool"], "edit_memory")

        mgr.decide(approval_id, "approved")
        self.assertIn("likes coffee", (self.home / "USER.md").read_text(encoding="utf-8"))
        applies = self.events("memory_apply")
        self.assertEqual(len(applies), 1)
        self.assertEqual(applies[0]["approval"], approval_id)

        result2 = mgr.edit("USER.md", "add", "", "- likes tea", [], control)
        approval_id2 = result2.rsplit(" ", 1)[-1]
        mgr.decide(approval_id2, "denied")
        self.assertNotIn("likes tea", (self.home / "USER.md").read_text(encoding="utf-8"))

    def test_grants_allow_applies_user_turn_at_once(self):
        config.set(self.home, "grants", {"edit_memory": "allow"})
        mgr = self.manager()
        self.record("session_start", session="s1")
        self.record("user", session="s1", turn="t1", origin="user", trust="user", text="hi")
        control = types.SimpleNamespace()
        control.session = "s1"
        result = mgr.edit("USER.md", "add", "", "- likes coffee", [], control)
        self.assertEqual(result, "applied")
        self.assertIn("likes coffee", (self.home / "USER.md").read_text(encoding="utf-8"))

        p = {"since": 0, "upto": 1, "lines": [], "allowed": {}, "read": 0,
            "applied": 0, "proposed": 0, "pages": [], "session": "s1"}
        control2 = types.SimpleNamespace()
        control2.memory_pass, control2.session = p, "s1"
        last = self.record("user", session="s1", turn="t2", origin="user", trust="user", text="tea too")
        p["allowed"][last["seq"]] = last
        result2 = mgr.edit("USER.md", "add", "", "- likes tea", [last["seq"]], control2)
        self.assertTrue(result2.startswith("queued"))

    SKILL = "---\nname: foo\ndescription: Weekly report steps\n---\nCollect the notes.\nSend them.\n"

    def _skill_control(self):
        self.record("session_start", session="s1")
        self.record("user", session="s1", turn="t1", origin="user", trust="user", text="keep this procedure")
        control = types.SimpleNamespace()
        control.session = "s1"
        return control

    def test_skill_edit_from_user_turn_always_waits_for_approval(self):
        config.set(self.home, "grants", {"edit_memory": "allow"})
        mgr = self.manager()
        result = mgr.edit("skills/foo.md", "add", "", self.SKILL, [], self._skill_control())
        self.assertTrue(result.startswith("queued for your approval as "))
        path = self.home / "skills" / "foo.md"
        self.assertFalse(path.exists())
        pending = self.state.pending()
        self.assertEqual([r["tool"] for r in pending], ["edit_memory"])
        mgr.decide(pending[0]["id"], "approved")
        self.assertEqual(path.read_text(encoding="utf-8"), self.SKILL.rstrip("\n"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("- foo: Weekly report steps", skills.index(self.home))

    def test_skill_edit_from_pass_signal_or_job_is_refused(self):
        mgr = self.manager()
        p = {"since": 0, "upto": 5, "lines": [], "allowed": {}, "read": 0, "applied": 0, "proposed": 0,
             "pages": [], "session": "s1"}
        control = types.SimpleNamespace()
        control.memory_pass, control.session = p, "s1"
        with self.assertRaises(ValueError):
            mgr.edit("skills/foo.md", "add", "", self.SKILL, [], control)
        self.assertFalse((self.home / "skills").exists())
        self.assertEqual(self.state.pending(), [])
        self.assertEqual(tools_mod.TOOLS["edit_memory"].origins, {"user", "memory"})  # D76: the rest refused

    def test_third_party_text_cannot_become_a_skill(self):
        mgr = self.manager()
        token = "ghp_" + "A" * 36
        for text in ("no frontmatter", self.SKILL.replace("name: foo", "name: bar"),
                     self.SKILL + f"use {token}\n"):
            proposal, error = mgr.vet("skills/foo.md", "add", "", text, [], "user", {}, "s1")
            self.assertIsNone(proposal)
            self.assertTrue(error)
            with self.assertRaises(ValueError):
                mgr.edit("skills/foo.md", "add", "", text, [], self._skill_control())
        self.assertEqual(self.state.pending(), [])
        with self.assertRaisesRegex(ValueError, "frontmatter"):
            mgr.edit("skills/x.md", "add", "", "1. step", [], self._skill_control())
        target = memory.TOOLS["edit_memory"].params["target"]["description"]
        self.assertIn("name:", target)
        self.assertIn("description:", target)
        for bad in ("skills/a/b.md", "skills/../x.md", "skills/x.txt"):
            with self.assertRaises(ValueError):
                memory.vault_path(self.home, bad)

    def test_approvals_prints_a_skill_proposal_whole(self):
        lines = ["---", "name: foo", "description: Weekly report steps", "---", "Send\u200b them."]
        self.record("approval_pending", session="s1", origin="user", id="a1", call_id="a1", tool="edit_memory",
                    args={"target": "skills/foo.md", "op": "add", "old": "", "new": "\n".join(lines),
                          "evidence": []}, messages=[])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.approvals(self.home)
        printed = out.getvalue().split("\n")
        for line in lines[:4]:
            self.assertIn("    " + line, printed)
        self.assertIn("    Send\\u200b them.", printed)
        self.assertNotIn("\u200b", out.getvalue())

    def test_approvals_shows_removed_skill_lines_and_survives_a_gate_row(self):
        for id, args in (("r1", {"target": "skills/foo.md", "op": "replace", "old": "Send them.\nThen rest.",
                                 "new": "Mail them."}),
                         ("g1", {"target": "USER.md", "op": "add", "new": "- likes tea"})):
            self.record("approval_pending", session="s1", origin="user", id=id, call_id=id, tool="edit_memory",
                        args=args, messages=[])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.approvals(self.home)
        printed = out.getvalue().split("\n")
        for line in ("  old text:", "    Send them.", "    Then rest.", "  new text:", "    Mail them."):
            self.assertIn(line, printed)
        self.assertIn("likes tea", out.getvalue())

    def test_skill_edge_cases(self):
        mgr = self.manager()
        control = self._skill_control()
        (self.home / "skills").mkdir()
        outside = self.home / "outside.md"
        outside.write_text(self.SKILL.replace("foo", "y"), encoding="utf-8")
        (self.home / "skills" / "y.md").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            mgr.edit("skills/y.md", "add", "", "more", [], control)
        mgr.apply({"target": "skills/foo.md", "op": "add", "old": "", "new": self.SKILL.rstrip("\n"), "evidence": []})
        mgr.undo(1)
        self.assertFalse((self.home / "skills" / "foo.md").exists())
        with self.assertLogs("pathfinder.skills", level="WARNING") as logs:
            skills._log.warning("sentinel")
            skills.index(self.home)
        self.assertEqual(len(logs.records), 2)  # the sentinel and the symlinked y.md, nothing for foo
        mgr.apply({"target": "skills/foo.md", "op": "add", "old": "", "new": self.SKILL.rstrip("\n"), "evidence": []})
        with self.assertRaisesRegex(ValueError, "cannot be emptied"):
            mgr.edit("skills/foo.md", "remove", self.SKILL.rstrip("\n"), "", [], control)
        p = {"since": 0, "upto": 1, "lines": [], "allowed": {}, "read": 0, "applied": 0, "proposed": 0,
             "pages": [], "session": "s1"}
        passed = types.SimpleNamespace()
        passed.memory_pass, passed.session = p, "s1"
        for ctl in (control, passed):
            with self.assertRaisesRegex(ValueError, "read_skill"):
                memory._read_memory("skills/foo.md", ctl)

    def test_rules_apply_calls_on_rules_changed(self):
        mgr = self.manager()
        called = []
        mgr.on_rules_changed = lambda: called.append(True)
        proposal = {"target": "RULES.md", "op": "replace", "old": "| always | - | silence | |",
                   "new": "| count_over | 3 | speak | |\n| always | - | silence | |", "evidence": []}
        reason = mgr.apply(proposal)
        self.assertEqual(reason, "")
        self.assertEqual(called, [True])
        self.assertIn("count_over", (self.home / "RULES.md").read_text(encoding="utf-8"))

    def test_read_memory_matches_space_and_underscore_names(self):
        mgr = self.manager()
        mgr.apply({"target": "wiki/Weekly_Plan.md", "op": "add", "old": "", "new": "# Weekly Plan\nGym.", "evidence": []})
        for asked in ("wiki/Weekly Plan.md", "wiki/weekly plan.md", "wiki/Weekly_Plan.md"):
            self.assertIn("Gym.", memory._read_memory(asked))
        self.assertTrue(memory._read_memory("wiki/Monthly Plan.md").startswith("no such page"))

    def test_rule_vocab_skips_a_sense_that_fails_to_import(self):
        mgr = self.manager()
        real = signals.importlib.import_module

        def broken_calendar(name):
            if name == "pathfinder.senses.calendar":
                raise ImportError("broken")
            return real(name)
        with mock.patch.object(signals.importlib, "import_module", broken_calendar):
            conditions, kinds = mgr._rule_vocab()
        self.assertNotIn("upcoming_event", kinds)
        self.assertIn("new_mail", kinds)  # the other senses still count
        self.assertTrue(set(rules_mod.CORE_CONDITIONS) <= set(conditions))

    def test_undo_is_the_inverse_op(self):
        mgr = self.manager()
        (self.home / "wiki").mkdir()
        (self.home / "wiki" / "Coffee.md").write_text("# Coffee\n", encoding="utf-8")
        mgr.apply({"target": "wiki/Coffee.md", "op": "add", "old": "", "new": "Coffee is good.", "evidence": []})
        self.assertIn("Coffee is good.", (self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8"))
        mgr.undo(1)
        self.assertNotIn("Coffee is good.", (self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8"))

        mgr.apply({"target": "wiki/Coffee.md", "op": "replace", "old": "# Coffee",
                  "new": "# Coffee\nSubtitle", "evidence": []})
        self.assertIn("Subtitle", (self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8"))
        mgr.undo(1)
        self.assertNotIn("Subtitle", (self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8"))

    def test_undo_twice_walks_back_two_edits(self):
        mgr = self.manager()
        (self.home / "wiki").mkdir()
        (self.home / "wiki" / "Coffee.md").write_text("# Coffee\n", encoding="utf-8")
        mgr.apply({"target": "wiki/Coffee.md", "op": "add", "old": "", "new": "line one", "evidence": []})
        mgr.apply({"target": "wiki/Coffee.md", "op": "add", "old": "", "new": "line two", "evidence": []})
        text = (self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8")
        self.assertIn("line one", text)
        self.assertIn("line two", text)

        results1 = mgr.undo(1)
        text = (self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8")
        self.assertTrue(all(r.endswith("applied") for r in results1), results1)
        self.assertIn("line one", text)
        self.assertNotIn("line two", text)  # the most recent edit went first

        results2 = mgr.undo(1)  # a second call must reach the OLDER edit, not re-undo the same one
        text = (self.home / "wiki" / "Coffee.md").read_text(encoding="utf-8")
        self.assertTrue(all(r.endswith("applied") for r in results2), results2)
        self.assertNotIn("line one", text)
        self.assertNotIn("line two", text)

        applies = self.events("memory_apply")
        self.assertEqual([a["approval"] for a in applies], [None, None, "undo", "undo"])

    def test_decide_revets_before_apply(self):
        mgr = self.manager()
        proposal = {"target": "RULES.md", "op": "replace", "old": "| always | - | silence | |",
                   "new": "| frobnicate | 1 | speak | note |", "evidence": []}
        self.record("approval_pending", session="s1", origin="memory", id="ap1", call_id="ap1",
                    tool="edit_memory", args=proposal, messages=[])
        mgr.decide("ap1", "approved")
        self.assertEqual((self.home / "RULES.md").read_text(encoding="utf-8"), RULES_MD)
        errors = self.events("error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["origin"], "memory")
        self.assertIn("frobnicate", errors[0]["message"])


class ConcurrencyTest(MemoryHarness):
    def test_concurrent_wiki_edits_do_not_corrupt(self):
        mgr = self.manager()
        (self.home / "wiki").mkdir()
        (self.home / "wiki" / "A.md").write_text("# A\n", encoding="utf-8")
        (self.home / "wiki" / "B.md").write_text("# B\n", encoding="utf-8")
        self.record("session_start", session="sa")
        self.record("user", session="sa", turn="t1", origin="user", trust="user", text="hi")
        self.record("session_start", session="sb")
        self.record("user", session="sb", turn="t1", origin="user", trust="user", text="hi")
        errors = []

        def worker(target, prefix, session):
            control = types.SimpleNamespace()
            control.session = session
            for i in range(50):
                result = mgr.edit(target, "add", "", f"{prefix}{i}", [], control)
                if result != "applied":
                    errors.append(result)

        t1 = threading.Thread(target=worker, args=("wiki/A.md", "a", "sa"))
        t2 = threading.Thread(target=worker, args=("wiki/B.md", "b", "sb"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(errors, [])
        text_a = (self.home / "wiki" / "A.md").read_text(encoding="utf-8")
        text_b = (self.home / "wiki" / "B.md").read_text(encoding="utf-8")
        for i in range(50):
            self.assertIn(f"a{i}", text_a)
            self.assertIn(f"b{i}", text_b)
        self.assertEqual(sorted(p["path"] for p in mgr.state.pages()), ["wiki/A.md", "wiki/B.md"])


class EvidenceTest(MemoryHarness):
    def _recording(self, per_call):
        """A step factory that logs (tools, messages) of every request; per_call[i] answers request i."""
        log = []

        def factory(cfg, control, tools):
            def step(messages):
                log.append((tools, list(messages)))
                r = per_call[len(log) - 1]
                if isinstance(r, Exception):
                    raise r
                return iter(r)
            return step
        return factory, log

    def _session_of_six(self):
        self._seed_pass_events()
        return rules_mod.Rules(consolidate_min_events=1, consolidate_steps=3)

    def test_wiki_dir_0700(self):
        mgr = self.manager()
        mgr.apply({"target": "wiki/Coffee.md", "op": "add", "old": "", "new": "# Coffee\nGood.",
                   "evidence": []}, origin="user")
        self.assertEqual((self.home / "wiki").stat().st_mode & 0o777, 0o700)

    def test_user_turn_evidence_is_the_last_user_message(self):
        mgr = self.manager()
        self.record("session_start", session="s1")
        self.record("user", session="s1", turn="t1", origin="user", trust="user", text="hi")
        last = self.record("user", session="s1", turn="t2", origin="user", trust="user", text="I like tea")
        control = types.SimpleNamespace()
        control.session = "s1"
        result = mgr.edit("USER.md", "add", "", "- likes tea", [], control)
        self.assertTrue(result.startswith("queued"))
        pending = self.state.pending()
        self.assertEqual(pending[0]["args"]["evidence"], [last["seq"]])


class ContextTest(MemoryHarness):
    def test_context_has_user_body_and_map(self):
        (self.home / "USER.md").write_text(USER_MD + "The user likes coffee.\n", encoding="utf-8")
        mgr = self.manager()
        for i in range(60):
            mgr.state.index_page(f"wiki/Page{i}.md", f"Page Number {i:03d} With A Longer Title",
                                 "text", 1000 + i, None, "")
        ctx = mgr.context()
        self.assertTrue(ctx.startswith("The user likes coffee."))
        self.assertIn("Wiki pages:", ctx)
        wiki_part = ctx.split("Wiki pages:\n", 1)[1]
        self.assertLessEqual(len(wiki_part) + len("Wiki pages:\n"), 1000)
        self.assertNotIn("Page Number 000", wiki_part)  # oldest page cut off; newest first


class ToolRegistryTest(unittest.TestCase):
    def test_memory_origin_tools_are_opt_in(self):
        pass_tools = {name for name, spec in tools_mod.TOOLS.items() if "memory" in spec.origins}
        self.assertEqual(pass_tools, {"search_memory", "read_memory", "edit_memory"})
        user_tools = {name for name, spec in tools_mod.TOOLS.items() if "user" in spec.origins}
        self.assertTrue({"search_memory", "read_memory", "edit_memory"} <= user_tools)
        self.assertNotIn("read_ledger", user_tools)


if __name__ == "__main__":
    unittest.main()
