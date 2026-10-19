import json
import unittest
from types import SimpleNamespace
from unittest import mock

from pathfinder import search, tools
from pathfinder.llm import LLMError
from pathfinder.mcp import tools as mcp_tools
from pathfinder.mcp.servers import Entry
from pathfinder.tools import ToolSpec

REGISTERED = tools.TOOLS.get("search_tools")

WORDS = ["mail", "page", "archive", "shell", "reminder"]
CFG = {"embed_model": "local:fake-embed", "providers": {"local": {"base_url": "http://127.0.0.1:9/v1"}}}


def _spec(name, description, **params):
    return ToolSpec(name=name, description=description, params=params, required=[], handler=lambda **a: None)


def _fake_embed(calls):
    """A bag-of-words vector over WORDS; every call's input texts are kept in `calls`."""
    def embed(endpoint, texts):
        calls.append(list(texts))
        return [[float(w in t.lower()) for w in WORDS] + [0.01] for t in texts]
    return embed


REGISTRY = {
    "search_mail": _spec("search_mail", "Search the mail inbox.\nMore lines.", query={"type": "string"}),
    "mcp__notion__pages": _spec("mcp__notion__pages", "Notion pages.",
                                action={"type": "string", "enum": ["get", "archive"]}),
    "run_command": _spec("run_command", "Run a shell command.", command={"type": "string"}),
    "add_reminder": _spec("add_reminder", "Add a reminder.", title={"type": "string"}),
}


class SearchTest(unittest.TestCase):
    def setUp(self):
        search._INDEX.clear()
        patch = mock.patch.dict(tools.TOOLS, {**REGISTRY, "search_tools": search.SEARCH_TOOLS}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def _find(self, query, cfg=CFG, embed=None):
        with mock.patch("pathfinder.search.config.load", return_value=cfg), \
                mock.patch("pathfinder.search.openai_compat.embed", embed or _fake_embed([])):
            return search.find(query, None)

    def test_text_holds_name_description_params_and_string_enums(self):
        self.assertEqual(search.text(REGISTRY["mcp__notion__pages"]), "mcp__notion__pages\nNotion pages.\naction\nget archive")

    def test_search_falls_back_without_embeddings(self):
        def broken(endpoint, texts):
            raise LLMError(0, "URLError: connection refused")
        for cfg, embed, reason in (({**CFG, "embed_model": ""}, None, "no embed_model"),
                                   (CFG, broken, "connection refused")):
            out = self._find("archive a page", cfg, embed)
            self.assertIn("search is unavailable", out)
            self.assertIn(reason, out)
            for name in REGISTRY:
                self.assertIn(f"- {name}: ", out)
            self.assertNotIn("search_tools:", out)  # it does not list itself
            self.assertNotIn("More lines", out)  # the first line only
            self.assertLessEqual(len(out), search.LIMIT)

        calls = []
        out = self._find("archive a notion page", embed=_fake_embed(calls))
        self.assertEqual(out.split("\n")[1], json.dumps(REGISTRY["mcp__notion__pages"].schema()["function"]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 1 + len(REGISTRY))  # the query and every text, in one call
        tools.TOOLS["add_reminder"] = _spec("add_reminder", "Add or snooze a reminder.", title={"type": "string"})
        self._find("send mail", embed=_fake_embed(calls))
        self.assertEqual(calls[1], ["send mail", search.text(tools.TOOLS["add_reminder"])])  # only the changed text

    def test_output_fits_the_limit(self):
        many = {f"tool_{i:02}": _spec(f"tool_{i:02}", "mail " + "x" * 2500, query={"type": "string"})
                for i in range(40)}
        tools.TOOLS.clear()
        tools.TOOLS.update(many)
        for cfg in (CFG, {**CFG, "embed_model": ""}):
            out = self._find("mail", cfg)
            self.assertLessEqual(len(out), search.LIMIT)
            self.assertRegex(out, r"\(\d+ more tools not shown\)$")
        self.assertEqual(self._find("mail").count('"parameters"'), 2)  # a third full spec would not fit

    def test_exact_name_returns_that_spec(self):
        no_embed = mock.Mock(side_effect=AssertionError("no embed for a name"))
        out = self._find(" run_command ", embed=no_embed)
        self.assertEqual(out.split("\n")[1], json.dumps(REGISTRY["run_command"].schema()["function"]))
        tools.TOOLS["huge"] = _spec("huge", "Huge.", **{f"p{i}": {"type": "string"} for i in range(400)})
        out = self._find("huge", embed=no_embed)
        self.assertRegex(out, r"^The tool's schema is \d+ chars, over the result limit of 6000:\n- huge: Huge\.$")

    def test_mcp_tools_odd_enum_and_provenance(self):
        """A third-party schema's `enum` may be any value; a listed MCP line keeps the server's words, and the
        result says once whose words they are."""
        definitions = [{"name": "pages", "description": "Notion pages: get or archive.\nMore.",
                        "inputSchema": {"properties": {"mode": {"type": "string", "enum": 5},
                                                       "flag": {"type": "boolean", "enum": True}}}}]
        entries = [Entry("notion", d["name"], f"mcp__notion__{d['name']}", d) for d in definitions]
        specs = mcp_tools.specs(entries, None, SimpleNamespace(passed={e.qualified_name for e in entries}))
        tools.TOOLS.update(specs)
        self.assertTrue(search.text(specs["mcp__notion__pages"]).endswith("\nmode flag\n"))  # no enum values
        self.assertTrue(self._find("archive a page").startswith("Tools nearest"))  # ranked, no TypeError
        out = self._find("archive a page", {**CFG, "embed_model": ""})
        self.assertIn("\n- mcp__notion__pages: Notion pages: get or archive.\n", out)
        self.assertEqual(out.split("\n")[1], search.MCP_NOTE)
        self.assertNotIn(search.MCP_NOTE, self._find("run_command"))

    def test_vectors_of_another_dimension_are_re_embedded(self):
        calls = []
        self._find("mail", embed=lambda endpoint, texts: [[1.0, 0.0] for _ in texts])
        out = self._find("mail", embed=_fake_embed(calls))  # the ref now names a model of another size
        self.assertEqual([len(c) for c in calls], [1, 1 + len(REGISTRY)])  # every stored vector dropped, re-embedded
        nearest = next(line for line in out.split("\n") if line.startswith("{"))
        self.assertEqual(json.loads(nearest)["name"], "search_mail")

    def test_registered_as_a_read_tool(self):
        self.assertIs(REGISTERED, search.SEARCH_TOOLS)  # the real registry, read before setUp patches it
        self.assertFalse(search.SEARCH_TOOLS.write)


if __name__ == "__main__":
    unittest.main()
