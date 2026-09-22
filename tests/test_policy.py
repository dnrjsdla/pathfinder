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


if __name__ == "__main__":
    unittest.main()
