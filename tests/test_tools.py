import unittest

from pathfinder.tools import ToolSpec, coerce

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


if __name__ == "__main__":
    unittest.main()
