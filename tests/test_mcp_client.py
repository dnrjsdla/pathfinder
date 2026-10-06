"""MCP client tests - spec compliance pinned down without any network.

The transport is faked so it is clear what is being checked: against a real
server, "the server answered that way" and "we asked that way" get tangled
together. Faking the transport isolates the second half.

The HTTP layer (header assembly, SSE parsing) is checked separately by faking
`urlopen`.
"""
from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from pathfinder.mcp import protocol
from pathfinder.mcp import session as session_mod
from pathfinder.mcp import transport as tr


class FakeTransport(tr.Transport):
    """Records every message sent and returns a canned result."""

    def __init__(self, results: dict | None = None) -> None:
        self.sent: list[tuple[dict, dict]] = []
        self.results = results or {}
        self.closed = False

    def send(self, message, *, extra_headers=None, timeout=30):
        self.sent.append((message, dict(extra_headers or {})))
        result = self.results.get(message["method"])
        if isinstance(result, Exception):
            raise result
        return result if result is not None else {}

    def close(self):
        self.closed = True


class FakeStdioTransport(tr.StdioTransport):
    """Same fake behaviour as `FakeTransport`, but typed as `StdioTransport` so
    `ServerSession`'s transport-based lifecycle guess picks legacy."""

    def __init__(self, results: dict | None = None) -> None:
        self.sent: list[tuple[dict, dict]] = []
        self.results = results or {}

    def send(self, message, *, extra_headers=None, timeout=30):
        self.sent.append((message, dict(extra_headers or {})))
        result = self.results.get(message["method"])
        if isinstance(result, Exception):
            raise result
        return result if result is not None else {}

    def close(self):
        pass


class EnvelopeTest(unittest.TestCase):
    def test_every_request_carries_meta(self):
        """Stateless, so every request must carry its own version, identity, capabilities."""
        fake = FakeTransport({"tools/list": {"tools": []}})
        session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS).list_tools()

        message, _ = fake.sent[0]
        meta = message["params"]["_meta"]
        self.assertEqual(message["jsonrpc"], "2.0")
        self.assertEqual(meta["io.modelcontextprotocol/protocolVersion"], protocol.PROTOCOL_VERSION)
        self.assertEqual(meta["io.modelcontextprotocol/clientInfo"]["name"], "pathfinder")

    def test_elicitation_is_not_declared(self):
        """Would compete with the approval gate as a second "ask the human" path."""
        self.assertEqual(protocol.CLIENT_CAPABILITIES, {})

    def test_request_ids_do_not_repeat(self):
        fake = FakeTransport({"tools/list": {"tools": []}})
        s = session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS)
        s.list_tools(force=True)
        s.list_tools(force=True)
        self.assertEqual([m["id"] for m, _ in fake.sent], [1, 2])


class ErrorKindTest(unittest.TestCase):
    def test_jsonrpc_error_raises(self):
        with self.assertRaises(protocol.McpProtocolError) as caught:
            protocol.read_result({"error": {"code": -32601, "message": "Method not found"}})
        self.assertEqual(caught.exception.code, -32601)

    def test_tool_error_is_a_result_not_an_exception(self):
        """`isError: true` is a result the tool produced. Turning it into an exception
        would hide information the model could use to fix its call."""
        fake = FakeTransport({"tools/call": {
            "resultType": "complete",
            "content": [{"type": "text", "text": "invalid date format"}],
            "isError": True,
        }})
        out = session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS
                                        ).call_tool("book", {"date": "yesterday"})
        self.assertIs(out["isError"], True)
        self.assertIn("date", out["content"][0]["text"])

    def test_unknown_result_type_is_refused(self):
        """Pretending to know an unknown shape would make everything after it a lie."""
        with self.assertRaises(protocol.McpProtocolError):
            protocol.read_result({"result": {"resultType": "input_required"}})


class ToolNameTest(unittest.TestCase):
    def test_qualify_round_trip(self):
        self.assertEqual(protocol.qualify("github", "search"), "mcp__github__search")
        self.assertEqual(protocol.unqualify("mcp__github__search"), ("github", "search"))

    def test_tool_name_may_contain_the_separator(self):
        """A `__` inside the server-side tool name does not break the split
        (only the first two `__` are consumed)."""
        name = protocol.qualify("gh", "admin__list")
        self.assertEqual(protocol.unqualify(name), ("gh", "admin__list"))

    def test_foreign_names_are_not_ours(self):
        for name in ("add_event", "mcp__", "mcp__gh", "other__gh__x"):
            with self.subTest(name=name):
                self.assertIsNone(protocol.unqualify(name))


class HeaderEncodingTest(unittest.TestCase):
    def test_header_value_encoding(self):
        self.assertEqual(protocol.header_value("us-west1"), "us-west1")
        for raw in ("café", " padded ", "line1\nline2"):
            expected = "=?base64?" + base64.b64encode(raw.encode("utf-8")).decode("ascii") + "?="
            with self.subTest(raw=raw):
                self.assertEqual(protocol.header_value(raw), expected)

    def test_sentinel_lookalike_is_encoded(self):
        """Plain text that happens to look like the sentinel - not wrapping it
        would make the server decode it as if it were one."""
        out = protocol.header_value("=?base64?literal?=")
        self.assertEqual(out, "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?=")


_SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string", "x-mcp-header": "Region"},
        "query": {"type": "string"},
        "opts": {"type": "object", "properties": {
            "dry": {"type": "boolean", "x-mcp-header": "Dry"}}},
    },
}


class XMcpHeaderTest(unittest.TestCase):
    def test_annotated_params_become_headers(self):
        fake = FakeTransport({
            "tools/list": {"tools": [{"name": "sql", "inputSchema": _SCHEMA}]},
            "tools/call": {"content": []},
        })
        s = session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS)
        s.list_tools()
        s.call_tool("sql", {"region": "us-west1", "query": "SELECT 1",
                            "opts": {"dry": False}})

        _, headers = fake.sent[-1]
        self.assertEqual(headers["Mcp-Param-Region"], "us-west1")
        # Python's `False` stringified as-is would be "False", which fails server-side comparison.
        self.assertEqual(headers["Mcp-Param-Dry"], "false")
        self.assertNotIn("Mcp-Param-Query", headers)

    def test_absent_value_omits_the_header(self):
        """No value, no header - sending one anyway is a 400 HeaderMismatch."""
        fake = FakeTransport({
            "tools/list": {"tools": [{"name": "sql", "inputSchema": _SCHEMA}]},
            "tools/call": {"content": []},
        })
        s = session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS)
        s.list_tools()
        s.call_tool("sql", {"query": "SELECT 1"})
        self.assertEqual(fake.sent[-1][1], {})

    def test_tools_with_invalid_annotations_are_dropped(self):
        """Spec MUST - a tool with an invalid annotation is dropped from the list. Others survive."""
        schemas = [
            {"properties": {"a": {"type": "string", "x-mcp-header": "bad header"}}},
            {"properties": {"a": {"type": "number", "x-mcp-header": "Num"}}},
            {"properties": {"a": {"type": "string", "x-mcp-header": "X"},
                            "b": {"type": "string", "x-mcp-header": "x"}}},
        ]
        for schema in schemas:
            with self.subTest(schema=schema):
                fake = FakeTransport({"tools/list": {"tools": [
                    {"name": "bad", "inputSchema": schema},
                    {"name": "good", "inputSchema": {"type": "object", "properties": {}}},
                ]}})
                names = [t["name"] for t in
                         session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS
                                                   ).list_tools()]
                self.assertEqual(names, ["good"])

    def test_annotation_under_array_items_is_ignored(self):
        """Past `items` the annotation is not statically reachable, so it is invalid - and dropped."""
        schema = {"type": "object", "properties": {
            "rows": {"type": "array", "items": {
                "type": "object",
                "properties": {"r": {"type": "string", "x-mcp-header": "R"}}}}}}
        fake = FakeTransport({
            "tools/list": {"tools": [{"name": "t", "inputSchema": schema}]},
            "tools/call": {"content": []},
        })
        s = session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS)
        self.assertEqual([t["name"] for t in s.list_tools()], ["t"])
        s.call_tool("t", {"rows": [{"r": "x"}]})
        self.assertEqual(fake.sent[-1][1], {})


class ToolListCacheTest(unittest.TestCase):
    def test_tool_list_is_cached(self):
        """Re-fetching every turn would make the tool array shift, breaking the model prompt cache."""
        fake = FakeTransport({"tools/list": {"tools": [{"name": "a"}], "ttlMs": 60_000}})
        s = session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS)
        s.list_tools()
        s.list_tools()
        self.assertEqual(len(fake.sent), 1)
        s.list_tools(force=True)
        self.assertEqual(len(fake.sent), 2)

    def test_expired_cache_refetches(self):
        fake = FakeTransport({"tools/list": {"tools": [{"name": "a"}], "ttlMs": 1_000}})
        s = session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS)
        clock = {"t": 1000.0}
        with mock.patch.object(session_mod.time, "monotonic", lambda: clock["t"]):
            s.list_tools()
            clock["t"] += 2.0
            s.list_tools()
        self.assertEqual(len(fake.sent), 2)


class _Response:
    def __init__(self, body: bytes, content_type: str):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpLayerTest(unittest.TestCase):
    def _capture(self, body: bytes, content_type: str = "application/json"):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["request"] = request
            seen["timeout"] = timeout
            return _Response(body, content_type)

        patcher = mock.patch.object(tr.urllib.request, "urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_http_headers_follow_the_spec(self):
        seen = self._capture(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}).encode())

        t = tr.StreamableHttpTransport("https://example.com/mcp",
                                       headers={"Authorization": "Bearer x"})
        t.send(protocol.envelope("tools/call", {"name": "get_weather"}, request_id=1))

        headers = {k.lower(): v for k, v in seen["request"].headers.items()}
        self.assertEqual(headers["mcp-protocol-version"], protocol.PROTOCOL_VERSION)
        self.assertEqual(headers["mcp-method"], "tools/call")
        self.assertEqual(headers["mcp-name"], "get_weather")
        self.assertEqual(headers["authorization"], "Bearer x")
        # The server picks which shape it replies with, so both must be accepted.
        self.assertIn("application/json", headers["accept"])
        self.assertIn("text/event-stream", headers["accept"])

    def test_protocol_version_header_matches_the_body(self):
        """A mismatch gets a 400 from the server - both must come from one place."""
        seen = self._capture(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())
        message = protocol.envelope("tools/list", request_id=1)
        tr.StreamableHttpTransport("https://example.com/mcp").send(message)

        header = {k.lower(): v for k, v in seen["request"].headers.items()}
        body_version = message["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
        self.assertEqual(header["mcp-protocol-version"], body_version)

    def test_non_ascii_tool_name_is_encoded_in_the_header(self):
        seen = self._capture(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())
        tr.StreamableHttpTransport("https://example.com/mcp").send(
            protocol.envelope("tools/call", {"name": "café_event"}, request_id=1))

        headers = {k.lower(): v for k, v in seen["request"].headers.items()}
        self.assertTrue(headers["mcp-name"].startswith("=?base64?"))

    def test_sse_response_yields_the_final_result(self):
        """Progress notifications may be interleaved before the final reply."""
        stream = (
            ":\n\n"                                        # keep-alive comment
            'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            'data: {"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text",'
            '"text":"done"}]}}\n\n'
        ).encode()
        self._capture(stream, "text/event-stream")

        out = tr.StreamableHttpTransport("https://example.com/mcp").send(
            protocol.envelope("tools/call", {"name": "t"}, request_id=7))
        self.assertEqual(out["content"][0]["text"], "done")

    def test_sse_without_our_response_is_an_error(self):
        self._capture(b'data: {"jsonrpc":"2.0","id":99,"result":{}}\n\n', "text/event-stream")
        with self.assertRaises(protocol.McpTransportError):
            tr.StreamableHttpTransport("https://example.com/mcp").send(
                protocol.envelope("tools/list", request_id=1))


class DualEraProbeTest(unittest.TestCase):
    """The one design change from the base spec: `server/discover` first costs
    a timeout per (silent) legacy server, so the probe guesses by transport and
    only flips on an explicit JSON-RPC error."""

    def test_probe_accepts_stateless_when_discover_answers(self):
        fake = FakeTransport({"server/discover": {"protocolVersions": [protocol.PROTOCOL_VERSION]}})
        s = session_mod.ServerSession("s", fake)
        s.discover()
        self.assertEqual(s.lifecycle, protocol.Lifecycle.STATELESS)
        methods = [m["method"] for m, _ in fake.sent]
        self.assertNotIn("initialize", methods)

    def test_probe_flips_to_legacy_when_discover_is_refused(self):
        fake = FakeTransport({
            "server/discover": protocol.McpProtocolError(-32601, "Method not found"),
            "initialize": {"protocolVersion": "2025-06-18", "capabilities": {}},
        })
        s = session_mod.ServerSession("s", fake)
        s.discover()
        self.assertEqual(s.lifecycle, protocol.Lifecycle.LEGACY)
        self.assertEqual(s.negotiated_version, "2025-06-18")
        methods = [m["method"] for m, _ in fake.sent]
        self.assertIn("notifications/initialized", methods)

    def test_list_tools_runs_the_probe_first(self):
        """Review blocker: `entries()` -> `list_tools()` never called `discover()`, so a stateless
        stdio server (guessed legacy) failed with -32601 and the probe only ever ran in tests."""
        fake = FakeStdioTransport({  # typed stdio: the guess is legacy, so `initialize` goes first
            "initialize": protocol.McpProtocolError(-32601, "Method not found"),
            "server/discover": {"protocolVersions": [protocol.PROTOCOL_VERSION]},
            "tools/list": {"tools": [{"name": "t", "inputSchema": {"type": "object"}}]},
        })
        s = session_mod.ServerSession("s", fake)
        tools = s.list_tools()
        self.assertEqual([t["name"] for t in tools], ["t"])
        self.assertEqual(s.lifecycle, protocol.Lifecycle.STATELESS)
        methods = [m["method"] for m, _ in fake.sent]
        self.assertEqual(methods[:2], ["initialize", "server/discover"])

    def test_explicit_lifecycle_skips_the_probe(self):
        fake = FakeTransport({"server/discover": protocol.McpProtocolError(-32601, "Method not found")})
        s = session_mod.ServerSession("s", fake, lifecycle=protocol.Lifecycle.STATELESS)
        with self.assertRaises(protocol.McpProtocolError):
            s.discover()
        self.assertEqual(s.lifecycle, protocol.Lifecycle.STATELESS)  # never flipped

    def test_probe_guesses_legacy_first_for_stdio_transport(self):
        fake = FakeStdioTransport({"initialize": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        s = session_mod.ServerSession("s", fake)
        self.assertEqual(s.lifecycle, protocol.Lifecycle.LEGACY)
        s.discover()
        self.assertEqual(fake.sent[0][0]["method"], "initialize")


if __name__ == "__main__":
    unittest.main()
