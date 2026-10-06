import unittest
from unittest import mock

from pathfinder import policy
from pathfinder.tools import ToolSpec


def _spec(name, **kw):
    return ToolSpec(name=name, description="", params={}, required=[], handler=lambda **a: None, **kw)


READ = _spec("read_thing")
NET = _spec("net_thing", network=True)
SHELL = _spec("shell_thing", write=True, sandboxed=True)
WRITE = _spec("write_thing", write=True)
REVERSIBLE = _spec("reversible_thing", write=True, reversible=True)


class PolicyTableTest(unittest.TestCase):
    def test_policy_table(self):
        # (origin, spec, expected) - spec.params is a dict, so ToolSpec is unhashable;
        # a flat list of cases avoids using it as a dict key.
        cases = [
            ("user", READ, "allow"), ("user", SHELL, "allow"), ("user", NET, "allow"), ("user", WRITE, "ask"),
            ("signal", READ, "allow"), ("signal", SHELL, "ask"), ("signal", NET, "ask"), ("signal", WRITE, "ask"),
            ("job", READ, "allow"), ("job", SHELL, "ask"), ("job", NET, "ask"), ("job", WRITE, "ask"),
            ("system", READ, "allow"), ("system", SHELL, "deny"), ("system", NET, "deny"), ("system", WRITE, "deny"),
        ]
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            for origin, spec, expected in cases:
                self.assertEqual(policy.decide(spec, origin, {}, {}), expected, f"{origin} x {spec.name}")

    def test_escalate_forces_write(self):
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            self.assertEqual(policy.decide(SHELL, "user", {"escalate": True}, {}), "ask")
            self.assertEqual(policy.decide(SHELL, "signal", {"escalate": True}, {}), "ask")
            self.assertEqual(policy.decide(SHELL, "system", {"escalate": True}, {}), "deny")

    def test_missing_sandbox_forces_write(self):
        with mock.patch("pathfinder.policy._exec.available", return_value=False):
            self.assertEqual(policy.decide(SHELL, "user", {}, {}), "ask")
            self.assertEqual(policy.decide(SHELL, "signal", {}, {}), "ask")

    def test_grants_win_for_user_only(self):
        grants = {"write_thing": "allow"}
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            self.assertEqual(policy.decide(WRITE, "user", {}, grants), "allow")
            self.assertEqual(policy.decide(WRITE, "signal", {}, grants), "ask")
            self.assertEqual(policy.decide(WRITE, "job", {}, grants), "ask")
            self.assertEqual(policy.decide(WRITE, "system", {}, grants), "deny")

    def test_grant_never_covers_leaving_the_sandbox_and_typos_fail_closed(self):
        grants = {"shell_thing": "allow", "write_thing": "asl"}
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            self.assertEqual(policy.decide(SHELL, "user", {}, grants), "allow")
            self.assertEqual(policy.decide(SHELL, "user", {"escalate": True}, grants), "ask")
            self.assertEqual(policy.decide(WRITE, "user", {}, grants), "ask")  # a typo is not "allow"
        with mock.patch("pathfinder.policy._exec.available", return_value=False):
            self.assertEqual(policy.decide(SHELL, "user", {}, grants), "ask")  # no sandbox: not covered

    def test_grants_can_also_tighten(self):
        grants = {"read_thing": "deny"}
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            self.assertEqual(policy.decide(READ, "user", {}, grants), "deny")

    def test_policy_reversible(self):
        """D22: a reversible write (add_schedule, remove_schedule) runs by itself for the
        user; every other origin still asks (job/signal) or denies (system) exactly as an
        ordinary write; an irreversible write's own row is unchanged; grants still win for user."""
        cases = [
            ("user", REVERSIBLE, "allow"), ("signal", REVERSIBLE, "ask"),
            ("job", REVERSIBLE, "ask"), ("system", REVERSIBLE, "deny"),
        ]
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            for origin, spec, expected in cases:
                self.assertEqual(policy.decide(spec, origin, {}, {}), expected, f"{origin} x {spec.name}")
            self.assertEqual(policy.decide(WRITE, "user", {}, {}), "ask")  # irreversible: unchanged
            grants = {"reversible_thing": "deny"}
            self.assertEqual(policy.decide(REVERSIBLE, "user", {}, grants), "deny")  # grants still win

    def test_memory_origin_row(self):
        """The consolidation pass: read allowed, reversible edits allowed, everything else
        denied - and grants (the user's, for user turns) never apply to origin `memory`."""
        cases = [
            ("memory", READ, "allow"), ("memory", SHELL, "deny"), ("memory", NET, "deny"),
            ("memory", WRITE, "deny"), ("memory", REVERSIBLE, "allow"),
        ]
        with mock.patch("pathfinder.policy._exec.available", return_value=True):
            for origin, spec, expected in cases:
                self.assertEqual(policy.decide(spec, origin, {}, {}), expected, f"{origin} x {spec.name}")
            grants = {"edit_memory": "allow"}
            self.assertEqual(policy.decide(REVERSIBLE, "memory", {}, grants), "allow")
            self.assertEqual(policy.decide(WRITE, "memory", {}, grants), "deny")


if __name__ == "__main__":
    unittest.main()
