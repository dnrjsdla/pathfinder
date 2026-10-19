import unittest
from unittest import mock

from pathfinder import config, ledger, policy
from pathfinder.tools import ToolSpec


def _spec(name, **kw):
    return ToolSpec(name=name, description="", params={}, required=[], handler=lambda **a: None, **kw)


NET = _spec("net_thing", network=True)
SHELL = _spec("shell_thing", write=True, sandboxed=True)
REVERSIBLE = _spec("reversible_thing", write=True, reversible=True)


def _natives() -> dict:
    """Every native in the registry with every sense imported (memory and skills register at import)."""
    from pathfinder import memory, signals, skills, tools  # noqa: F401 - imported for their registration
    registry = dict(tools.TOOLS)
    for _name, mod in signals.sense_modules():
        registry.update(getattr(mod, "TOOLS", {}) or {})
    return {name: spec for name, spec in registry.items() if not name.startswith("mcp__")}


# The Behaviour list's natives that write; every other native is a read (mail, memory and registry reads,
# propose_install with `network` among them). delete_reminder and add_schedule are graded by hand (below).
WRITES = {"write_file", "add_event", "add_reminder", "complete_reminder", "remove_schedule", "edit_memory", "run_command"}
LATER = {"delete_reminder", "add_schedule"}

GRADED = {g: _spec(f"{g}_thing", effect=g) for g in ("read", "write", "destructive", "external_send")}
GRADED["unknown"] = _spec("mcp__srv__thing")  # MCP, no map, no annotations

# (row, read, write, destructive, external_send, unknown) - the contract's policy table, no full-auto row
MATRIX = [
    (("user", "read-only"), "allow", "deny", "deny", "deny", "ask"),
    (("user", "manual"), "allow", "ask", "ask", "ask", "ask"),
    (("user", "auto"), "allow", "allow", "ask", "ask", "ask"),
    (("signal", "manual"), "allow", "ask", "ask", "ask", "ask"),
    (("system", "manual"), "allow", "deny", "deny", "deny", "deny"),
]


def _verdict(spec, origin="user", args=None, mode="manual", grants=None, tools=None, session=frozenset()):
    cfg = {"approval_mode": mode, "grants": grants or {}, "tools": tools or {}}
    return policy.verdict(spec, origin, args, cfg, set(session))[0]


class VerdictTest(unittest.TestCase):
    def test_effect_table(self):
        natives = _natives()
        self.assertLessEqual(WRITES | LATER, set(natives))
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            for name, spec in natives.items():
                if name not in LATER:
                    want = "write" if name in WRITES else "read"
                    self.assertEqual(policy.effect(spec, {}, {}), (want, None), name)
            shell = natives["run_command"]
            self.assertEqual(policy.effect(shell, {"escalate": True}, {})[0], "destructive")
        with mock.patch("pathfinder.policy._exec.available", return_value=False):
            self.assertEqual(policy.effect(shell, {}, {})[0], "destructive")  # no sandbox on the machine
        self.assertEqual(policy.effect(natives["delete_reminder"], {}, {}), ("destructive", None))
        schedule = natives["add_schedule"]
        self.assertEqual(policy.effect(schedule, {}, {}), ("external_send", None))
        for mode in ("manual", "auto"):  # asks in every mode that can run it (read-only denies), no grant lifts it
            for grants, session in (({}, ()), ({"add_schedule": "allow"}, {"add_schedule"})):
                self.assertEqual(_verdict(schedule, mode=mode, grants=grants, session=session), "ask", mode)
        self.assertEqual(_verdict(schedule, mode="read-only"), "deny")

        self.assertEqual(set(policy.MODES), set(config._APPROVAL_MODES))  # no full-auto in either
        self.assertEqual(set(GRADED), {*config.GRADES, "unknown"})
        for (origin, mode), *cells in MATRIX:
            # a signal or system turn never reads the user's approval_mode: its row holds under every mode
            for mode in policy.MODES if origin != "user" else (mode,):
                for grade, want in zip(GRADED, cells):
                    self.assertEqual(_verdict(GRADED[grade], origin, mode=mode), want, f"{origin} {mode} {grade}")

        # sandboxed / reversible lift a write in user · manual only
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            for spec in (SHELL, REVERSIBLE, natives["run_command"], natives["edit_memory"], natives["remove_schedule"]):
                self.assertEqual(_verdict(spec), "allow", spec.name)
                self.assertEqual(_verdict(spec, mode="read-only"), "deny", spec.name)
                self.assertEqual(_verdict(spec, "signal"), "ask", spec.name)
                self.assertEqual(_verdict(spec, "system"), "deny", spec.name)
            self.assertEqual(_verdict(SHELL, args={"escalate": True}), "ask")
            self.assertEqual(_verdict(SHELL, args={"escalate": True}, mode="auto"), "ask")
        # a networked read asks for a signal and is denied to the system
        self.assertEqual(_verdict(NET), "allow")
        self.assertEqual(_verdict(NET, "signal"), "ask")
        self.assertEqual(_verdict(NET, "system"), "deny")

    def test_one_origin_vocabulary(self):
        self.assertEqual(set(policy.ORIGINS), set(ledger.ORIGINS))
        self.assertEqual(set(policy.ORIGINS), set(_spec("any").origins))

    def test_unknown_effect_asks(self):
        bare = _spec("mcp__srv__pages")  # no map, no annotations
        read_by_annotations = _spec("mcp__srv__pages", effect="read")
        tools = {"srv": {"pages": {"effect_by": {"action": {"get": "read", "archive": "write"}}}}}
        cases = [
            (bare, {}, {}, None),
            (read_by_annotations, {"action": "delete"}, tools, {"arg": "action", "value": "delete"}),  # value absent
            (read_by_annotations, {}, tools, {"arg": "action"}),                                          # arg absent
        ]
        for spec, args, tools_cfg, unknown in cases:
            self.assertEqual(policy.effect(spec, args, tools_cfg), ("unknown", unknown))
            for mode in policy.MODES:
                for grants, session in (({}, ()), ({spec.name: "allow", "mcp__srv__*": "allow"}, {spec.name})):
                    with self.subTest(mode=mode, args=args, grants=grants):
                        self.assertEqual(_verdict(spec, args=args, mode=mode, grants=grants, tools=tools_cfg,
                                                  session=session), "ask")
        # the map's hits, and its plain `effect`, come before the annotations
        self.assertEqual(policy.effect(read_by_annotations, {"action": "archive"}, tools), ("write", None))
        self.assertEqual(policy.effect(read_by_annotations, {"action": "get"}, tools), ("read", None))
        self.assertEqual(policy.effect(read_by_annotations, {}, {"srv": {"pages": {"effect": "destructive"}}}),
                         ("destructive", None))
        self.assertEqual(policy.effect(read_by_annotations, {}, {"other": tools["srv"]}), ("read", None))

    def test_grants_never_lift_destructive(self):
        for grade in ("destructive", "external_send", "unknown"):
            spec = _spec(f"mcp__srv__{grade}", effect="" if grade == "unknown" else grade)
            for grants, session in (({spec.name: "allow"}, ()), ({"mcp__srv__*": "allow"}, ()), ({}, {spec.name})):
                for mode in ("manual", "auto"):
                    with self.subTest(grade=grade, grants=grants, session=session, mode=mode):
                        self.assertEqual(_verdict(spec, mode=mode, grants=grants, session=session), "ask")
        write = _spec("mcp__srv__write", effect="write")
        native_write = _spec("native_write", effect="write")
        # a deny in any scope beats an allow in another
        self.assertEqual(_verdict(write, grants={"mcp__srv__*": "deny"}, session={write.name}), "deny")
        self.assertEqual(_verdict(write, grants={write.name: "deny", "mcp__srv__*": "allow"}), "deny")
        self.assertEqual(_verdict(GRADED["read"], grants={"read_thing": "deny"}), "deny")
        # an allow in any scope lifts a write; a server ask does not block a session allow
        for grants, session in (({write.name: "allow"}, ()), ({"mcp__srv__*": "allow"}, ()), ({}, {write.name})):
            self.assertEqual(_verdict(write, grants=grants, session=session), "allow")
        self.assertEqual(_verdict(write, grants={"mcp__srv__*": "ask"}, session={write.name}), "allow")
        self.assertEqual(_verdict(native_write, grants={"native_write": "allow"}), "allow")
        self.assertEqual(_verdict(native_write, grants={"native_write": "allow"}, mode="read-only"), "deny")
        # an ask tightens an allow; a typo is ignored; grants are the user's only
        self.assertEqual(_verdict(native_write, grants={"native_write": "ask"}, mode="auto"), "ask")
        self.assertEqual(_verdict(GRADED["read"], grants={"read_thing": "ask"}), "ask")
        self.assertEqual(_verdict(native_write, grants={"native_write": "alow"}), "ask")
        self.assertEqual(_verdict(native_write, "signal", grants={"native_write": "allow"}, session={"native_write"}), "ask")
        self.assertEqual(_verdict(native_write, "system", grants={"native_write": "allow"}), "deny")


if __name__ == "__main__":
    unittest.main()
