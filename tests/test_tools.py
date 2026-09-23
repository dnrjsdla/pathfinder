import unittest

from pathfinder import jobs  # noqa: F401 - import side effect: registers start_job into tools.TOOLS
from pathfinder import tools as tools_mod
from pathfinder.tools import ToolSpec, coerce, normalize_local_datetime

SPEC = ToolSpec(
    name="thing",
    description="",
    params={
        "s": {"type": "string"},
        "n": {"type": "integer"},
        "f": {"type": "number"},
        "b": {"type": "boolean"},
    },
    required=["s"],
    handler=lambda **a: None,
)


class CoerceTest(unittest.TestCase):
    def test_coerce_casts_declared_types(self):
        args, err = coerce(SPEC, {"s": "hi", "n": "5", "f": "2.5", "b": "true"})
        self.assertEqual(err, "")
        self.assertEqual(args, {"s": "hi", "n": 5, "f": 2.5, "b": True})

    def test_coerce_drops_undeclared_keys(self):
        args, err = coerce(SPEC, {"s": "hi", "extra": "nope"})
        self.assertEqual(err, "")
        self.assertNotIn("extra", args)

    def test_coerce_missing_required(self):
        args, err = coerce(SPEC, {})
        self.assertIn("missing required argument 's'", err)

    def test_coerce_uncastable_value(self):
        args, err = coerce(SPEC, {"s": "hi", "n": "not a number"})
        self.assertIn("must be an integer", err)

    def test_coerce_bool_string_false_is_false(self):
        # the bug this exists to prevent: python's bool("false") is True
        args, err = coerce(SPEC, {"s": "hi", "b": "false"})
        self.assertEqual(err, "")
        self.assertIs(args["b"], False)


class LocalDatetimeNormalizeTest(unittest.TestCase):
    def test_local_datetime_normalize(self):
        normalize = normalize_local_datetime("at")
        self.assertEqual(normalize({"at": "2026-09-22T09:00", "text": "x"}),
                         {"at": "2026-09-22 09:00", "text": "x"})
        self.assertEqual(normalize({"other": "unchanged"}), {"other": "unchanged"})  # key absent: no-op

    def test_local_datetime_normalize_refuses_offset_and_z(self):
        normalize = normalize_local_datetime("at")
        for bad in ("2026-09-22T09:00Z", "2026-09-22 09:00+09:00", "2026-09-22 09:00-05:00"):
            with self.assertRaises(ValueError):
                normalize({"at": bad})


class OriginRegistryTest(unittest.TestCase):
    def test_tools_for_origin(self):
        """`start_job` is user-only: a job cannot start a worker, and a signal turn cannot
        either - structure, not policy (the model never even sees the schema)."""
        user_names = set(tools_mod.for_origin("user"))
        job_names = set(tools_mod.for_origin("job"))
        signal_names = set(tools_mod.for_origin("signal"))
        self.assertIn("start_job", user_names)
        self.assertNotIn("start_job", job_names)
        self.assertNotIn("start_job", signal_names)
        # the read and write tools are still there for a job and a signal turn - policy (not
        # this registry) is what turns "write" into a gate for them.
        for names in (job_names, signal_names):
            self.assertIn("read_file", names)
            self.assertIn("write_file", names)

    def test_for_origin_memory_is_opt_in(self):
        """`memory` (the consolidation pass) sees no tool by default - a `ToolSpec` must
        explicitly opt in with `origins={..., "memory"}` (as memory.py's own tools do; this
        module ships none itself)."""
        self.assertNotIn("write_file", tools_mod.for_origin("memory"))
        self.assertNotIn("read_file", tools_mod.for_origin("memory"))
        spec = ToolSpec(name="memory_only_thing", description="", params={}, required=[],
                        handler=lambda **a: None, origins=frozenset({"memory"}))
        tools_mod.TOOLS["memory_only_thing"] = spec
        try:
            self.assertIn("memory_only_thing", tools_mod.for_origin("memory"))
        finally:
            del tools_mod.TOOLS["memory_only_thing"]


if __name__ == "__main__":
    unittest.main()
