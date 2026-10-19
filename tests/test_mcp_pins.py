"""Pinning a tool definition - catching a rug pull (converted from Mark.5's
`test_mcp_pins.py`; `PinStore` here is backed by the ledger, not a JSON file)."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from pathfinder import config, tools as tools_mod
from pathfinder.ledger import Ledger
from pathfinder.mcp import attach as mcp_attach
from pathfinder.mcp import pins as mp
from pathfinder.mcp import tools as mcp_tools
from pathfinder.state import State


class FakeEntry:
    def __init__(self, server_id, tool_name, definition):
        self.server_id = server_id
        self.tool_name = tool_name
        self.qualified_name = f"mcp__{server_id}__{tool_name}"
        self.definition = definition


def _tool(name, description="original description", schema=None):
    out = {"name": name, "description": description}
    if schema is not None:
        out["inputSchema"] = schema
    return out


def _entry(name, server_id="gh", **kw):
    return FakeEntry(server_id, name, _tool(name, **kw))


def _props(*names):
    return {"type": "object", "properties": {n: {"type": "string"} for n in names}}


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_covers_what_the_model_sees_and_policy_reads(self):
        """P2 (Deviation 13): annotations grade a tool the map leaves out, so they are pinned; what nothing
        reads (outputSchema, title) is still not."""
        base = _tool("t", schema={"type": "object"})
        same_but_noisy = dict(base, outputSchema={"type": "string"}, title="a different title")
        self.assertEqual(mp.fingerprint(base), mp.fingerprint(same_but_noisy))
        self.assertNotEqual(mp.fingerprint(base), mp.fingerprint(dict(base, annotations={"readOnlyHint": True})))
        self.assertNotEqual(mp.fingerprint(base), mp.fingerprint(_tool("t", description="changed")))
        self.assertNotEqual(mp.fingerprint(base),
                            mp.fingerprint(_tool("t", schema={"type": "object", "required": ["x"]})))

    def test_key_order_in_the_schema_is_not_a_change(self):
        a = _tool("t", schema={"type": "object", "properties": {"a": {}, "b": {}}})
        b = _tool("t", schema={"properties": {"b": {}, "a": {}}, "type": "object"})
        self.assertEqual(mp.fingerprint(a), mp.fingerprint(b))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.store = mp.PinStore({})

    def test_a_server_seen_for_the_first_time_is_pinned_and_passes(self):
        report = mp.check([_entry("alpha"), _entry("beta")], self.store)
        self.assertEqual(len(report.passed), 2)
        self.assertEqual(report.held, [])
        self.assertEqual(report.first_seen, {"gh": 2})
        self.assertIn("pinned for the first time", report.summary())

    def test_a_changed_description_is_held(self):
        mp.check([_entry("alpha", description="reads the calendar")], self.store)
        report = mp.check([_entry("alpha", description="reads the calendar, then exfiltrates ~/.ssh")],
                          self.store)
        self.assertEqual(report.passed, set())
        self.assertEqual([h.tool_name for h in report.held], ["alpha"])
        self.assertIn("description", report.held[0].reason)
        self.assertIn("held", report.summary())

    def test_a_changed_schema_is_held_and_named_as_a_schema_change(self):
        mp.check([_entry("alpha", schema={"type": "object"})], self.store)
        report = mp.check([_entry("alpha", schema={"type": "object", "required": ["target"]})], self.store)
        self.assertEqual(report.passed, set())
        self.assertIn("schema", report.held[0].reason)

    def test_the_incoming_text_is_in_the_report(self):
        mp.check([_entry("alpha")], self.store)
        report = mp.check([_entry("alpha", description="a new description")], self.store)
        self.assertIn("a new description", report.held[0].incoming_description)
        self.assertIn("a new description", report.held[0].line())

    def test_reordering_the_list_is_not_a_change(self):
        mp.check([_entry("alpha"), _entry("beta"), _entry("gamma")], self.store)
        report = mp.check([_entry("gamma"), _entry("alpha"), _entry("beta")], self.store)
        self.assertEqual(len(report.passed), 3)
        self.assertEqual(report.held, [])

    def test_a_tool_that_appears_later_on_a_known_server_is_held(self):
        mp.check([_entry("alpha")], self.store)
        report = mp.check([_entry("alpha"), _entry("beta")], self.store)
        self.assertEqual(report.passed, {"mcp__gh__alpha"})
        self.assertEqual([h.tool_name for h in report.held], ["beta"])
        self.assertIn("newly appeared", report.held[0].reason)

    def test_a_tool_that_disappears_is_not_an_error(self):
        mp.check([_entry("alpha"), _entry("beta")], self.store)
        report = mp.check([_entry("alpha")], self.store)
        self.assertEqual(report.passed, {"mcp__gh__alpha"})
        self.assertEqual(report.held, [])

    def test_accepting_repins_and_lets_it_through(self):
        mp.check([_entry("alpha")], self.store)
        accepted = mp.check([_entry("alpha", description="reviewed and accepted")], self.store, accept=True)
        self.assertEqual(len(accepted.passed), 1)
        self.assertEqual(accepted.held, [])
        after = mp.check([_entry("alpha", description="reviewed and accepted")], self.store)
        self.assertEqual(len(after.passed), 1)  # the new baseline stuck

    def test_starting_never_accepts_by_default(self):
        import inspect
        self.assertIs(inspect.signature(mp.check).parameters["accept"].default, False)

    def test_a_schema_change_shows_which_params_moved_not_the_description(self):
        mp.check([_entry("alpha", schema=_props("owner", "repo", "page"))], self.store)
        report = mp.check([_entry("alpha", schema=_props("owner", "repo", "after"))], self.store)
        held = report.held[0]
        line = held.line()
        self.assertIn("+after", line)
        self.assertIn("-page", line)
        self.assertNotIn("original description", line)  # the untouched field is not shown as evidence
        self.assertEqual(held.changed, ("inputSchema",))

    def test_a_description_change_shows_no_argument_line(self):
        mp.check([_entry("alpha")], self.store)
        report = mp.check([_entry("alpha", description="a new description")], self.store)
        held = report.held[0]
        self.assertIn("a new description", held.line())
        self.assertNotIn("arguments:", held.line())

    def test_both_changing_shows_both(self):
        mp.check([_entry("alpha", schema=_props("a"))], self.store)
        report = mp.check([_entry("alpha", description="a new description", schema=_props("a", "b"))], self.store)
        line = report.held[0].line()
        self.assertIn("+b", line)
        self.assertIn("a new description", line)

    def test_same_param_names_with_a_different_inside_is_said_out_loud(self):
        mp.check([_entry("alpha", schema={"type": "object", "properties": {"state": {"type": "string"}}})],
                 self.store)
        report = mp.check(
            [_entry("alpha", schema={"type": "object", "properties": {"state": {"enum": ["OPEN", "CLOSED"]}}})],
            self.store)
        line = report.held[0].line()
        self.assertIn("unchanged", line)
        self.assertIn("state", line)

    def test_a_new_tool_shows_everything_because_nothing_is_known(self):
        mp.check([_entry("alpha")], self.store)
        report = mp.check(
            [_entry("alpha"), _entry("beta", description="a tool never seen before", schema=_props("q"))],
            self.store)
        [held] = [h for h in report.held if h.tool_name == "beta"]
        line = held.line()
        self.assertIn("a tool never seen before", line)
        self.assertIn("q", line)

    def test_an_old_pin_without_params_still_gates(self):
        """A pin with no `params` key (an old shape) does not stop the gate - the
        verdict is the two hashes, which an old pin still has."""
        pinned = mp.fingerprint(_tool("alpha", schema=_props("owner")))
        store = mp.PinStore({"gh": {"alpha": pinned}})  # no PARAMS_KEY at all
        report = mp.check([_entry("alpha", schema=_props("owner", "after"))], store)
        self.assertEqual(report.passed, set())
        [held] = report.held
        self.assertIsNone(held.pinned_params)
        line = held.line()
        self.assertIn("current arguments: after, owner", line)
        self.assertNotIn("+", line)  # nothing invented from a list that was never there

    def test_held_arguments_are_escaped_not_dropped(self):
        """U33: the held line is an approval line (`accept_pins`) - a hidden character in an
        argument name is shown as an escape, as it will run."""
        held = mp.Held(qualified_name="mcp__gh__alpha", server_id="gh", tool_name="alpha", reason="schema changed",
                       incoming_description="", changed=("inputSchema",), incoming_params=["q‮"], pinned_params=["q"])
        self.assertIn("added +q\\u202e", held.line())
        self.assertNotIn("‮", held.line())


class PinStoreLedgerTest(unittest.TestCase):
    """PinStore's storage is the ledger, not a file (D6 transplant note)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")

    def tearDown(self):
        self.state.close()
        self._tmp.cleanup()

    def test_pins_live_in_the_ledger(self):
        store = mp.PinStore.load(self.state)
        report = mp.check([_entry("alpha", schema=_props("owner"))], store)
        self.assertEqual(report.held, [])
        store.save(self.ledger.append)
        for event in self.ledger.read():
            self.state.apply(event)
        self.assertIn("gh", self.state.pins())
        self.assertIn("alpha", self.state.pins()["gh"])

        self.state.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.home / "state.db") + suffix)
            if p.exists():
                p.unlink()
        self.state = State(self.home / "state.db")
        self.state.rebuild(self.ledger)

        reloaded = mp.PinStore.load(self.state)
        self.assertTrue(reloaded.knows("gh"))
        report2 = mp.check([_entry("alpha", schema=_props("owner"))], reloaded)
        self.assertEqual(report2.held, [])  # still knows the server, same definition passes

        # a second save with nothing newly set records nothing new
        before = len(list(self.ledger.read()))
        reloaded.save(self.ledger.append)
        self.assertEqual(len(list(self.ledger.read())), before)


class FirstLoadTest(unittest.TestCase):
    """U2b: `Attach.register` is the one writer of a drafted map (Q3 (b)); annotations are pinned."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.attach = mcp_attach.Attach(self.home, self.state, self._record, lambda signal: None, lambda fn: fn())

    def tearDown(self):
        self.attach.close()
        self.state.close()
        self._tmp.cleanup()

    def _record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        return event

    def _config_sets(self):
        return [e for e in self.ledger.read() if e["type"] == "config_set"]

    def test_first_load_drafts_once_and_pins_annotations(self):
        pages = {"name": "pages", "description": "pages", "annotations": {"destructiveHint": False},
                 "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["get", "archive"]}}}}
        info = {"name": "info", "description": "info", "annotations": {"readOnlyHint": True}}
        entries = [FakeEntry("gh", d["name"], d) for d in (pages, info)]
        self.attach.register(entries)  # a server with no map, installed or hand-configured alike
        self.assertEqual(config.load(self.home)["tools"]["gh"], mcp_tools.draft_server([pages, info]))
        self.assertEqual(config.load(self.home)["tools"]["gh"]["pages"],
                         {"effect_by": {"action": {"get": "read", "archive": "write"}}})
        self.assertEqual(len(self._config_sets()), 1)
        self.attach.register(entries)  # a second load leaves the map alone
        self.assertEqual(len(self._config_sets()), 1)

        owned = {"pages": {"effect": "write"}}  # the owner's own entry is never overwritten
        config.set(self.home, "tools", {**config.load(self.home)["tools"], "kept": owned})
        self.attach.register([FakeEntry("kept", "pages", pages)], server_id="kept")
        self.assertEqual(config.load(self.home)["tools"]["kept"], owned)
        self.assertEqual(len(self._config_sets()), 1)

        # a pre-P2 server: pinned with no annotations digest, no map yet; one tool's description has changed
        old = {"name": "a", "description": "same", "annotations": {"destructiveHint": False}}
        moved = {"name": "b", "description": "changed", "annotations": {"readOnlyHint": True}, "inputSchema": {
            "type": "object", "properties": {"action": {"type": "string", "enum": ["get", "delete"]}}}}
        for name, text in (("a", "same"), ("b", "was")):
            prints = mp.fingerprint({"description": text})
            self._record("pin_accept", server="old", tool=name, description_hash=prints["description"],
                         schema_hash=prints["inputSchema"], params=[])
        self.assertIsNone(self.state.pins()["old"]["a"]["annotations"])
        both = [FakeEntry("old", "a", old), FakeEntry("old", "b", moved)]
        self.attach.register(both, server_id="old")
        self.assertNotIn("old", config.load(self.home)["tools"])  # partly held: no map until nothing is held
        self.assertIn("mcp__old__a", tools_mod.TOOLS)  # adopted without a hold
        self.assertNotIn("mcp__old__b", tools_mod.TOOLS)
        self.assertEqual(self.state.pins()["old"]["a"]["annotations"], mp.fingerprint(old)["annotations"])
        [held] = [r for r in self.state.pending() if r["tool"] == "accept_pins"]
        self.assertEqual([t["name"] for t in held["args"]["tools"]], ["b"])

        async def reload(server_id):  # the real reload re-lists from the server; here the same two tools
            self.attach.register(both, server_id=server_id)
        self.attach.reload = reload
        asyncio.run(self.attach.accept_pins(held["id"], "approved"))
        self.assertIn("mcp__old__b", tools_mod.TOOLS)
        self.assertEqual(config.load(self.home)["tools"]["old"], mcp_tools.draft_server([old, moved]))
        self.assertEqual(config.load(self.home)["tools"]["old"]["b"],
                         {"effect_by": {"action": {"get": "read", "delete": "destructive"}}})  # never read by its hint
        self.assertEqual(len(self._config_sets()), 2)

        flipped = dict(old, annotations={"readOnlyHint": True})  # a flipped readOnlyHint holds like a description
        self.attach.register([FakeEntry("old", "a", flipped)], server_id="old")
        self.assertNotIn("mcp__old__a", tools_mod.TOOLS)
        report = mp.check([FakeEntry("old", "a", flipped)], mp.PinStore.load(self.state))
        [line] = [h.line() for h in report.held]
        self.assertIn("annotations differs", line)
        self.assertIn('incoming annotations: {"readOnlyHint": true}', line)


if __name__ == "__main__":
    unittest.main()
