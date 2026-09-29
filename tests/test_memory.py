"""`memory.Manager` end to end against fake step factories - no daemon, no socket, no network.
Same harness shape as `test_jobs.py`: `record`/`push` are the test's own, backed by a real
Ledger+State on a temp home."""
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from pathfinder import clock, config, memory
from pathfinder import rules as rules_mod
from pathfinder import tools as tools_mod
from pathfinder.ledger import Ledger
from pathfinder.llm import LLMError
from pathfinder.loop import TurnControl
from pathfinder.state import State

USER_MD = "---\nwaiting_for: []\nsubject_alerts: []\nnoise_domains: []\n---\n"
RULES_MD = "---\nconsolidate_min_events: 1\n---\n\n## job_done\n\n| always | - | silence | |\n"


def _steps(steps):
    """steps: a list of lists of (kind, payload) tuples, one per call to step(messages)."""
    calls = iter(steps)

    def factory(cfg, control, tools):
        def step(messages):
            return iter(next(calls))
        return step
    return factory


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
        self.pushed = []
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

    def push(self, signal):
        self.pushed.append(signal)

    def manager(self, step_factory=None, rules=None, senses=None, is_busy=None):
        rules = rules if rules is not None else rules_mod.Rules(consolidate_min_events=1)
        senses = senses if senses is not None else [_fake_sense({"job_done": None})]
        mgr = memory.Manager(self.home, self.record, self.push,
                             step_factory or _steps([[("text", "")]]),
                             rules, senses, is_busy or (lambda: False))
        self._managers.append(mgr)
        memory.bind(mgr)  # the tool handlers (edit_memory, read_ledger, ...) call the bound Manager
        return mgr

    def events(self, type=None):
        evs = list(self.ledger.read())
        return [e for e in evs if type is None or e["type"] == type]

    def _seed_pass_events(self):
        r = self.record
        r("session_start", session="s1")
        r("user", session="s1", turn="t1", origin="user", trust="user", text="hello")
        r("assistant", session="s1", turn="t1", origin="user", final=True, text="hi there")
        r("user", session="s1", turn="t2", origin="user", trust="user", text="how are you")
        r("assistant", session="s1", turn="t2", origin="user", final=True, text="good")
        u3 = r("user", session="s1", turn="t3", origin="user", trust="user", text="I like coffee")
        r("assistant", session="s1", turn="t3", origin="user", final=True, text="noted")
        sig = r("signal", session="s1", origin="signal", trust="self", sense="jobs", kind="job_done",
               key="k1", summary="a job finished", fields={})
        r("judgment", session="s1", origin="signal", signal_key="k1", speak=True, reason="because")
        r("decision", session="s1", origin="signal", signal_key="k1", action="speak", why="test",
         notice_id="n1", text="a job finished", signal_keys=["k1"])
        r("delivery", session="s1", origin="system", id="n1", via="test")
        last = r("reaction", session="s1", origin="user", delivery_id="n1", vote="up")
        return u3["seq"], sig["seq"], last["seq"]


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
    def test_wiki_applies_at_once(self):
        mgr = self.manager()
        control = TurnControl()
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
        control = TurnControl()
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
        control = TurnControl()
        control.session = "s1"
        result = mgr.edit("USER.md", "add", "", "- likes coffee", [], control)
        self.assertEqual(result, "applied")
        self.assertIn("likes coffee", (self.home / "USER.md").read_text(encoding="utf-8"))

        p = {"since": 0, "upto": 1, "lines": [], "allowed": {}, "read": 0,
            "applied": 0, "proposed": 0, "pages": [], "session": "s1"}
        control2 = TurnControl()
        control2.memory_pass, control2.session = p, "s1"
        last = self.record("user", session="s1", turn="t2", origin="user", trust="user", text="tea too")
        p["allowed"][last["seq"]] = last
        result2 = mgr.edit("USER.md", "add", "", "- likes tea", [last["seq"]], control2)
        self.assertTrue(result2.startswith("queued"))

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
            control = TurnControl()
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


class PassTest(MemoryHarness):
    def test_pass_reads_slice_and_advances_cursor(self):
        u3_seq, sig_seq, last_seq = self._seed_pass_events()
        steps = [
            [("tool_calls", [{"id": "c1", "name": "read_ledger", "arguments": {"after": 0, "limit": 80}}])],
            [("tool_calls", [{"id": "c2", "name": "edit_memory", "arguments": {
                "target": "wiki/Coffee.md", "op": "add", "old": "", "new": "# Coffee\nCoffee is good.",
                "evidence": [sig_seq]}}])],
            [("tool_calls", [{"id": "c3", "name": "edit_memory", "arguments": {
                "target": "USER.md", "op": "add", "old": "", "new": "- likes coffee",
                "evidence": [u3_seq]}}])],
            [("text", "wrote two lines")],
            [("text", "Summary: talked about coffee.")],
        ]
        mgr = self.manager(step_factory=_steps(steps),
                           rules=rules_mod.Rules(consolidate_min_events=1, consolidate_steps=10))
        mgr.run_pass()

        self.assertEqual(len(self.events("memory_apply")), 1)
        approvals = self.events("approval_pending")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["tool"], "edit_memory")
        self.assertEqual(len(self.events("session_summary")), 1)
        passes = self.events("memory_pass")
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0]["applied"], 1)
        self.assertEqual(passes[0]["proposed"], 1)
        self.assertEqual(passes[0]["read"], 9)
        self.assertEqual(self.state.memory_cursor, last_seq)
        self.assertEqual([s.kind for s in self.pushed], ["memory_proposed"])

    def test_pass_llm_error_keeps_the_slice(self):
        """D39 (live 2026-09-26: four offline passes skipped the owner's own instruction): a pass
        the model never answered leaves the cursor where it was and waits before retrying."""
        self._seed_pass_events()

        def factory(cfg, control, tools):
            def step(messages):
                raise LLMError(402, "no credit")
                yield  # pragma: no cover
            return step

        mgr = self.manager(step_factory=factory, rules=rules_mod.Rules(consolidate_min_events=1))
        mgr.run_pass()
        passes = self.events("memory_pass")
        self.assertEqual(len(passes), 1)
        self.assertIn("error", passes[0])
        self.assertEqual(passes[0]["upto"], passes[0]["since"])
        self.assertEqual(self.state.memory_cursor, 0)
        self.assertGreater(mgr._retry_at, time.time())

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

    def test_pass_makes_a_write_call_then_a_summary_call(self):
        rules = self._session_of_six()
        factory, log = self._recording([[("text", "nothing"), ("usage", {"total_tokens": 5})],
                                        [("text", "the summary"), ("usage", {"total_tokens": 7})]])
        self.manager(step_factory=factory, rules=rules).run_pass()
        self.assertEqual(len(log), 2)
        self.assertTrue(log[0][0] and not log[1][0])  # the write call has tools, the summary none
        self.assertIn(memory.SUMMARIZE_PROMPT, [m["content"] for m in log[1][1]])
        self.assertEqual([e["text"] for e in self.events("session_summary")], ["the summary"])
        p = self.events("memory_pass")[0]
        self.assertEqual((p["steps"], p["usage"]["total_tokens"]), (2, 12))

    def test_summary_is_recorded_when_the_write_call_wrote_nothing(self):
        rules = self._session_of_six()
        factory, log = self._recording([[("text", "nothing")], [("text", "the summary")]])
        self.manager(step_factory=factory, rules=rules).run_pass()
        self.assertEqual(self.events("memory_pass")[0]["applied"], 0)
        self.assertEqual(len(self.events("session_summary")), 1)

    def test_no_summary_call_without_new_messages(self):
        rules = self._session_of_six()
        self.record("session_summary", session="s1", origin="memory", upto=10**6, text="old")
        factory, log = self._recording([[("text", "nothing")]])
        self.manager(step_factory=factory, rules=rules).run_pass()
        self.assertEqual(len(log), 1)

    def test_write_call_has_no_final_step(self):
        """D30's extra final request is gone: an exhausted write budget makes no tool-less call
        of its own - the only tool-less request is the summary's."""
        rules = self._session_of_six()
        call = {"id": "c", "name": "read_ledger", "arguments": {"after": 0, "limit": 80}}
        factory, log = self._recording([[("tool_calls", [call])]] * 3 + [[("text", "the summary")]])
        self.manager(step_factory=factory, rules=rules).run_pass()
        self.assertEqual(len(log), 4)  # three write steps, one summary; with a final_step there would be five
        self.assertEqual([bool(t) for t, _ in log], [True, True, True, False])

    def test_write_outage_keeps_the_slice_and_makes_no_summary_call(self):
        rules = self._session_of_six()
        factory, log = self._recording([LLMError(500, "down")])
        mgr = self.manager(step_factory=factory, rules=rules)
        mgr.run_pass()
        self.assertEqual(len(log), 1)
        self.assertEqual(self.state.memory_cursor, 0)
        self.assertGreater(mgr._retry_at, time.time())

    def test_summary_llm_error_does_not_keep_the_slice(self):
        rules = self._session_of_six()
        factory, log = self._recording([[("text", "nothing")], LLMError(500, "down")])
        mgr = self.manager(step_factory=factory, rules=rules)
        mgr.run_pass()
        self.assertGreater(self.state.memory_cursor, 0)
        self.assertEqual(mgr._retry_at, 0.0)
        self.assertEqual(self.events("memory_pass")[0]["error"], "down")
        self.assertEqual(len(self.events("session_summary")), 0)

    def test_tool_call_in_the_summary_reply_is_an_honest_error(self):
        rules = self._session_of_six()
        call = {"id": "c", "name": "read_ledger", "arguments": {}}
        factory, log = self._recording([[("text", "nothing")], [("tool_calls", [call])]])
        self.manager(step_factory=factory, rules=rules).run_pass()
        self.assertEqual(self.events("memory_pass")[0]["error"], "summary: the model asked for a tool")
        self.assertEqual(len(self.events("session_summary")), 0)

    def test_forced_pass_clears_the_retry_wait(self):
        rules = self._session_of_six()
        factory, log = self._recording([[("text", "nothing")], [("text", "the summary")]])
        mgr = self.manager(step_factory=factory, rules=rules)
        mgr._retry_at = time.time() + 1800  # an earlier outage
        mgr.run_pass(force=True)
        self.assertEqual(mgr._retry_at, 0.0)

    def test_write_prompt_names_standing_instructions(self):
        for word in ("in any language", "how to behave", "not a list", "You must add", "USER.md", "a rule the judgments call for", "none of the kinds above", "nothing"):
            self.assertIn(word, " ".join(memory.CONSOLIDATE_PROMPT.split()))  # a line wrap is not a change
        self.assertNotIn("summary", memory.CONSOLIDATE_PROMPT.lower())

    def test_wiki_dir_0700(self):
        mgr = self.manager()
        mgr.apply({"target": "wiki/Coffee.md", "op": "add", "old": "", "new": "# Coffee\nGood.",
                   "evidence": []}, origin="user")
        self.assertEqual((self.home / "wiki").stat().st_mode & 0o777, 0o700)

    def test_pass_request_error_moves_on(self):
        """A 400 the slice itself causes would fail the same way every retry - it advances."""
        self._seed_pass_events()

        def factory(cfg, control, tools):
            def step(messages):
                raise LLMError(400, "context too long")
                yield  # pragma: no cover
            return step

        mgr = self.manager(step_factory=factory, rules=rules_mod.Rules(consolidate_min_events=1))
        mgr.run_pass()
        self.assertGreater(self.state.memory_cursor, 0)
        self.assertEqual(mgr._retry_at, 0.0)

    def test_pass_internal_error_still_advances(self):
        self._seed_pass_events()

        def factory(cfg, control, tools):
            def step(messages):
                raise RuntimeError("boom")
                yield  # pragma: no cover
            return step

        mgr = self.manager(step_factory=factory, rules=rules_mod.Rules(consolidate_min_events=1))
        report = mgr.run_pass()
        self.assertIn("internal: RuntimeError", report)
        passes = self.events("memory_pass")
        self.assertEqual(len(passes), 1)
        self.assertIn("internal: RuntimeError", passes[0]["error"])
        self.assertGreater(self.state.memory_cursor, 0)

    def test_summary_covers_messages_before_the_cursor(self):
        self.record("session_start", session="s1")
        self.record("user", session="s1", turn="t1", origin="user", trust="user", text="early one")
        self.record("assistant", session="s1", turn="t1", origin="user", final=True, text="early two")
        self.record("user", session="s1", turn="t2", origin="user", trust="user", text="early three")

        def factory1(cfg, control, tools):
            def step(messages):
                return iter([("text", "done")])  # three messages: too few for a summary
            return step

        mgr1 = self.manager(step_factory=factory1, rules=rules_mod.Rules(consolidate_min_events=1))
        mgr1.run_pass()
        self.assertEqual(len(self.events("session_summary")), 0)  # pass 1 produced no summary

        for i in range(9):
            self.record("user", session="s1", turn=f"t{i + 3}", origin="user", trust="user", text=f"later {i}")

        captured = {}

        def factory2(cfg, control, tools):
            def step(messages):
                captured["goal"] = messages[-1]["content"]
                return iter([("text", "done")])
            return step

        mgr2 = self.manager(step_factory=factory2, rules=rules_mod.Rules(consolidate_min_events=1))
        mgr2.run_pass()

        self.assertIn("Conversation since:", captured["goal"])  # the summary call reads them
        self.assertIn("early one", captured["goal"])
        self.assertIn("early two", captured["goal"])
        self.assertIn("early three", captured["goal"])

    def test_pass_evidence_must_be_from_its_slice(self):
        self._seed_pass_events()
        steps = [
            [("tool_calls", [{"id": "c1", "name": "read_ledger", "arguments": {"after": 0, "limit": 80}}])],
            [("tool_calls", [{"id": "c2", "name": "edit_memory", "arguments": {
                "target": "wiki/Coffee.md", "op": "add", "old": "", "new": "# Coffee\nx",
                "evidence": [999999]}}])],
            [("text", "done")],
        ]
        mgr = self.manager(step_factory=_steps(steps), rules=rules_mod.Rules(consolidate_min_events=1))
        mgr.run_pass()
        self.assertEqual(len(self.events("memory_apply")), 0)
        self.assertFalse((self.home / "wiki" / "Coffee.md").exists())

    def test_user_turn_evidence_is_the_last_user_message(self):
        mgr = self.manager()
        self.record("session_start", session="s1")
        self.record("user", session="s1", turn="t1", origin="user", trust="user", text="hi")
        last = self.record("user", session="s1", turn="t2", origin="user", trust="user", text="I like tea")
        control = TurnControl()
        control.session = "s1"
        result = mgr.edit("USER.md", "add", "", "- likes tea", [], control)
        self.assertTrue(result.startswith("queued"))
        pending = self.state.pending()
        self.assertEqual(pending[0]["args"]["evidence"], [last["seq"]])

    def test_read_ledger_pages(self):
        mgr = self.manager()
        lines = [({"seq": i}, f"line{i}") for i in range(1, 6)]
        p = {"lines": lines, "allowed": {}, "read": 0}
        self.assertEqual(mgr.read_ledger(p, 0, 2), "line1\nline2")
        self.assertEqual(mgr.read_ledger(p, 2, 2), "line3\nline4")
        self.assertEqual(mgr.read_ledger(p, 4, 2), "line5")
        self.assertEqual(mgr.read_ledger(p, 5, 2), "(end of range)")
        self.assertEqual(p["read"], 5)
        self.assertEqual(set(p["allowed"]), {1, 2, 3, 4, 5})

    def test_read_ledger_pages_by_size(self):
        mgr = self.manager()
        lines = [({"seq": i}, "x" * 300) for i in range(1, 121)]
        p = {"lines": lines, "allowed": {}, "read": 0}
        mgr.read_ledger(p, 0, 120)
        self.assertLess(len(p["allowed"]), 120)  # the 7,000-char budget, not the 120-line limit, capped it
        self.assertTrue(p["allowed"])  # always at least one line
        after = max(p["allowed"])
        while after < 120:
            mgr.read_ledger(p, after, 120)
            after = max(p["allowed"])
        self.assertEqual(set(p["allowed"]), set(range(1, 121)))
        self.assertEqual(p["read"], 120)

    def test_due_waits_for_idle_and_enough_lines(self):
        mgr = self.manager(rules=rules_mod.Rules(consolidate_min_events=3, consolidate_idle_min=20))
        mgr.state.last_user_ts = lambda: None
        self.assertFalse(mgr._due())

        mgr.state.last_user_ts = lambda: time.time()
        mgr._readable_events = lambda since, limit=None: ([], [(None, "x")] * 10)
        self.assertFalse(mgr._due())  # too recent

        mgr.state.last_user_ts = lambda: time.time() - 3600
        mgr._readable_events = lambda since, limit=None: ([], [(None, "x")] * 1)
        self.assertFalse(mgr._due())  # not enough readable events

        mgr._readable_events = lambda since, limit=None: ([], [(None, "x")] * 5)
        self.assertTrue(mgr._due())

    def test_pass_takes_at_most_400_lines_and_the_next_continues(self):
        r = self.record
        r("session_start", session="s1")
        for i in range(410):
            r("user", session="s1", turn=f"t{i}", origin="user", trust="user", text=f"msg {i}")
        mgr = self.manager(rules=rules_mod.Rules(consolidate_min_events=1))
        examined, readable = mgr._readable_events(0)
        self.assertEqual(len(readable), 400)
        upto = examined[-1]["seq"]
        self.assertLess(upto, self.ledger.last_seq)  # stopped short of the whole ledger

        examined2, readable2 = mgr._readable_events(upto)
        self.assertEqual(len(readable2), 10)  # the remaining 10 of the 410 seeded messages

    def test_stalled_after_three_silent_days(self):
        mgr = self.manager()
        now = time.time()
        bounds = clock.day_starts(now, 3)
        seq = [0]

        def backdate(type, ts, **data):
            seq[0] += 1
            self.state.apply({"seq": seq[0], "ts": ts, "type": type, "session": "", "turn": "",
                              "origin": "memory", "trust": "self", **data})

        for start in bounds:
            backdate("memory_pass", start + 10, since=0, upto=1, read=5, applied=0, proposed=0)
        self.assertTrue(mgr.stalled(now))

        backdate("memory_apply", bounds[0] + 20, target="wiki/a.md", op="add", old=None, new="x", approval=None)
        self.assertFalse(mgr.stalled(now))


class RenderLineTest(MemoryHarness):
    def test_render_line_shapes(self):
        state = self.state
        r = lambda e: memory.render_line(e, state)
        self.assertEqual(r({"seq": 1, "type": "user", "origin": "user", "text": "hi"}), "#1 [user] hi")
        self.assertIsNone(r({"seq": 1, "type": "user", "origin": "user", "job": "j1", "text": "hi"}))
        self.assertEqual(r({"seq": 2, "type": "user", "origin": "user", "steer": True, "text": "go"}),
                         "#2 [user steer] go")
        self.assertEqual(r({"seq": 3, "type": "assistant", "origin": "user", "final": True, "text": "ok"}),
                         "#3 [assistant] ok")
        self.assertIsNone(r({"seq": 3, "type": "assistant", "origin": "user", "text": "ok"}))
        self.assertEqual(r({"seq": 4, "type": "signal", "kind": "job_done", "summary": "did x"}),
                         "#4 [signal job_done] did x")
        self.assertIsNone(r({"seq": 7, "type": "tool_call", "name": "x"}))
        self.assertIsNone(r({"seq": 8, "type": "turn_start"}))
        self.assertEqual(r({"seq": 9, "type": "memory_apply", "target": "wiki/a.md", "op": "add"}),
                         "#9 [memory_apply] wiki/a.md add")
        self.assertEqual(r({"seq": 10, "type": "job_done", "summary": "s"}), "#10 [job_done] s")
        self.assertEqual(r({"seq": 11, "type": "job_failed", "reason": "r"}), "#11 [job_failed] r")

        self.record("signal", session="s1", origin="signal", trust="self", sense="jobs", kind="job_done",
                   key="k1", summary="x", fields={})
        self.assertEqual(r({"seq": 5, "type": "judgment", "signal_key": "k1", "speak": True, "reason": "why"}),
                         "#5 [judgment job_done] speak=True why")

        self.record("decision", session="s1", origin="signal", signal_key="k1", action="speak", why="w",
                   notice_id="n1", text="t", signal_keys=["k1"])
        self.assertEqual(r({"seq": 6, "type": "reaction", "delivery_id": "n1", "vote": "up"}),
                         "#6 [reaction up] on job_done")

        self.record("approval_pending", session="s1", origin="user", id="ap1", call_id="ap1",
                   tool="edit_memory", args={"target": "USER.md"}, messages=[])
        self.assertEqual(r({"seq": 12, "type": "approval", "id": "ap1", "decision": "approved"}),
                         "#12 [approval approved] edit_memory USER.md")


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
    def test_tools_for_origin_memory(self):
        pass_tools = set(tools_mod.for_origin("memory"))
        self.assertEqual(pass_tools, {"search_memory", "read_memory", "read_ledger", "edit_memory"})
        user_tools = set(tools_mod.for_origin("user"))
        self.assertTrue({"search_memory", "read_memory", "edit_memory"} <= user_tools)
        self.assertNotIn("read_ledger", user_tools)


if __name__ == "__main__":
    unittest.main()
