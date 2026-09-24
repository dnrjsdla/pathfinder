"""One server - ask what it sells, run one of them.

A session is **a conversation partner, not a connection**. Since 2026-07-28 MCP
has no session or handshake, so there is no state to hold onto here except a
tool-list cache, which is a convenience, not protocol state.

**Not true for the legacy era** (`Lifecycle.LEGACY`): there a handshake exists,
and this is where it is known - not the transport, so stdio and http share the
same handshake logic. It runs **lazily, right before the first request**
(`_call`); doing it at construction time would wake every configured server the
moment one session object is created.

Tool lists are cached because re-fetching every turn makes the tool array
shift slightly between turns, which breaks the model prompt's cache prefix -
the spec itself asks servers for a deterministic order for this reason.

Dual-era probe: the base spec has stateless clients probe `server/discover`
first. Measurement found real stdio servers do not answer that method at all -
not even an error, they simply never reply - so probing it first would cost a
full timeout per server. `discover()` instead guesses an era from the
transport, tries it, and only falls back to the other era on an explicit
JSON-RPC error (never on a timeout, which is left as what it is).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from .protocol import (Lifecycle, McpProtocolError, envelope, header_value,
                       initialize_params)
from .transport import StdioTransport, Transport

_log = logging.getLogger("pathfinder.mcp")

# Default lifetime for a server that does not send `ttlMs`. No reason to pick
# a short one - a server that changes its tools can say so (list_changed), and
# one that doesn't is polled anyway.
DEFAULT_TTL_MS = 300_000

# RFC 9110 tchar. An `x-mcp-header` value outside this makes that tool definition invalid.
_TCHAR = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Types `x-mcp-header` may annotate. `number` is excluded because the spec
# explicitly leaves it out (float formatting could disagree between header and body).
_HEADER_TYPES = {"string", "integer", "boolean"}

# The era a failed probe flips to.
_OTHER_ERA = {Lifecycle.STATELESS: Lifecycle.LEGACY, Lifecycle.LEGACY: Lifecycle.STATELESS}


def _guess_lifecycle(transport: Transport) -> Lifecycle:
    """Default era by transport, before any probe: stdio servers overwhelmingly
    speak the legacy handshake (measurement, 2026-08-22); anything else is
    assumed to speak the current stateless revision."""
    return Lifecycle.LEGACY if isinstance(transport, StdioTransport) else Lifecycle.STATELESS


class ServerSession:
    """One server. Ask it for tools, call one."""

    def __init__(self, server_id: str, transport: Transport, *,
                 timeout: int = 30,
                 lifecycle: Lifecycle | None = None) -> None:
        self.server_id = server_id
        # An explicit lifecycle wins outright and is never probed or flipped -
        # it is a config override. `None` is a guess, subject to `discover()`'s
        # one-shot probe.
        self._explicit = lifecycle is not None
        self.lifecycle = lifecycle if lifecycle is not None else _guess_lifecycle(transport)
        self._probed = False
        self._transport = transport
        self._timeout = timeout
        self._next_id = 0
        self._tools: list[dict[str, Any]] | None = None
        self._expires_at = 0.0
        # Has the legacy handshake finished? Always true under the stateless
        # era (nothing to do).
        self._ready = self.lifecycle is not Lifecycle.LEGACY
        # The version the server picked. Filled only under the legacy era (observational).
        self.negotiated_version = ""

    # ---- spec methods ---------------------------------------------------------

    def discover(self) -> dict[str, Any]:
        """The server's identity, supported version, capabilities.

        Tries `self.lifecycle` (the explicit setting, or the transport-based
        guess). If that probe gets a JSON-RPC error back (e.g. -32601 method
        not found) and the era was only a guess, the other era is tried once
        and `self.lifecycle` becomes whichever one answered. A timeout or
        transport error is not retried - it is what it is.
        """
        if self._explicit or self._probed:
            result = self._discover_for(self.lifecycle)
            self._probed = True
            return result
        try:
            result = self._discover_for(self.lifecycle)
        except McpProtocolError:
            self.lifecycle = _OTHER_ERA[self.lifecycle]
            self._ready = self.lifecycle is not Lifecycle.LEGACY  # a flip to legacy owes a handshake
            result = self._discover_for(self.lifecycle)
        self._probed = True
        return result

    def _discover_for(self, lifecycle: Lifecycle) -> dict[str, Any]:
        if lifecycle is Lifecycle.LEGACY:
            return self._handshake()
        self._ready = True
        return self._send("server/discover")

    def list_tools(self, *, force: bool = False) -> list[dict[str, Any]]:
        """The tools this server sells. Reuses the cache while it is alive.

        A tool whose `x-mcp-header` annotation violates the spec is **dropped
        from the list** (spec MUST) - one bad tool must not take the rest down
        with it, so only that tool is skipped, with a warning.
        """
        if not force and self._tools is not None and time.monotonic() < self._expires_at:
            return self._tools

        result = self._call("tools/list")
        raw = result.get("tools")
        if not isinstance(raw, list):
            raw = []

        tools: list[dict[str, Any]] = []
        for tool in raw:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            problem = _bad_header_annotation(tool.get("inputSchema"))
            if problem:
                _log.info(f"[mcp:{self.server_id}] dropped tool {tool['name']}: {problem}")
                continue
            tools.append(tool)

        ttl = result.get("ttlMs")
        ttl_ms = ttl if isinstance(ttl, (int, float)) and ttl > 0 else DEFAULT_TTL_MS
        self._tools = tools
        self._expires_at = time.monotonic() + (ttl_ms / 1000.0)
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one tool. Takes the name as the server knows it (no prefix).

        **`isError: true` is a result, not an exception.** The spec recommends
        showing it to the model - a bad date format or an out-of-range value is
        something the model can fix. Turning it into an exception would hide
        that information.
        """
        args = dict(arguments or {})
        extra = self._param_headers(name, args)
        return self._call("tools/call", {"name": name, "arguments": args},
                          extra_headers=extra)

    def close(self) -> None:
        self._transport.close()

    # ---- internal ---------------------------------------------------------

    def _call(self, method: str, params: dict[str, Any] | None = None,
              *, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        if not self._probed and not self._explicit:
            self.discover()         # first contact: the era probe (review: list_tools skipped it)
        elif not self._ready:
            self._handshake()       # legacy era: once, before the first real request
        return self._send(method, params, extra_headers=extra_headers)

    def _send(self, method: str, params: dict[str, Any] | None = None,
              *, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Send **without** going through the handshake. The handshake itself uses this."""
        self._next_id += 1
        message = envelope(method, params, request_id=self._next_id,
                           lifecycle=self.lifecycle)
        return self._transport.send(message, extra_headers=extra_headers,
                                    timeout=self._timeout)

    def _handshake(self) -> dict[str, Any]:
        """`initialize` -> `notifications/initialized`.

        **`instructions` is never read.** What a server puts there is not one
        tool's description but an instruction for the **whole conversation**,
        and reading it into the prompt would make a third party's sentence
        into pathfinder's own instruction. Measurement found a reference
        server's `instructions` field doing exactly this (steering answers
        about the server's own instructions) - not a corner case, a reference
        implementation demonstrating the risk. A tool description can be
        quoted with its source attached; there is no tool to attach this one to.
        So it is dropped.

        The server may pick a different version than the one we proposed - we
        don't block on that. The methods we use (`tools/list`, `tools/call`)
        have the same shape across the legacy era; we just record what it picked.
        """
        result = self._send("initialize", initialize_params())
        self.negotiated_version = str(result.get("protocolVersion") or "")
        # **The notification goes first.** The spec allows a server to ignore
        # requests before it arrives, and some do.
        self._transport.send(
            envelope("notifications/initialized", request_id=None,
                     lifecycle=self.lifecycle), timeout=self._timeout)
        self._ready = True
        if self.negotiated_version:
            _log.info(f"[mcp:{self.server_id}] legacy handshake - "
                      f"server picked version {self.negotiated_version}")
        return result

    def _param_headers(self, tool_name: str, args: dict[str, Any]) -> dict[str, str]:
        """Copy `x-mcp-header`-annotated arguments into headers.

        A client must support this whether or not the server uses it (spec
        MUST) - it lets a middlebox route on headers without parsing the body.
        No value means no header; the spec requires that, and sending one
        the server doesn't expect is a 400.
        """
        schema = self._schema_for(tool_name)
        if not schema:
            return {}
        headers: dict[str, str] = {}
        for path, name in _header_params(schema):
            value = _dig(args, path)
            if value is None:
                continue
            headers[f"Mcp-Param-{name}"] = header_value(_as_header_text(value))
        return headers

    def _schema_for(self, tool_name: str) -> dict[str, Any] | None:
        for tool in self._tools or []:
            if tool.get("name") == tool_name:
                schema = tool.get("inputSchema")
                return schema if isinstance(schema, dict) else None
        return None


# --- x-mcp-header rules --------------------------------------------------------

def _walk(schema: dict[str, Any], prefix: tuple[str, ...] = ()):
    """Follows only the `properties` chain.

    The spec says the annotation is invalid past `items`, `oneOf`, or `$ref`.
    Walking only the statically reachable path is exactly how that rule is enforced.
    """
    props = schema.get("properties")
    if not isinstance(props, dict):
        return
    for key, spec in props.items():
        if not isinstance(spec, dict):
            continue
        path = prefix + (key,)
        yield path, spec
        yield from _walk(spec, path)


def _header_params(schema: dict[str, Any]) -> list[tuple[tuple[str, ...], str]]:
    out = []
    for path, spec in _walk(schema):
        name = spec.get("x-mcp-header")
        if isinstance(name, str) and name:
            out.append((path, name))
    return out


def _bad_header_annotation(schema: Any) -> str | None:
    """The reason an `x-mcp-header` annotation is invalid, or `None`.

    A non-`None` return means that tool is dropped from `list_tools`.
    """
    if not isinstance(schema, dict):
        return None
    seen: set[str] = set()
    for path, spec in _walk(schema):
        name = spec.get("x-mcp-header")
        if name is None:
            continue
        if not isinstance(name, str) or not name:
            return f"{'.'.join(path)}: x-mcp-header is empty"
        if not _TCHAR.match(name):
            return f"{'.'.join(path)}: x-mcp-header '{name}' has an invalid character"
        if name.lower() in seen:
            return f"x-mcp-header '{name}' is duplicated"
        seen.add(name.lower())
        kind = spec.get("type")
        if kind not in _HEADER_TYPES:
            return f"{'.'.join(path)}: x-mcp-header only applies to {sorted(_HEADER_TYPES)}, got '{kind}'"
    return None


def _dig(args: dict[str, Any], path: tuple[str, ...]) -> Any:
    cursor: Any = args
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


def _as_header_text(value: Any) -> str:
    """Spec-defined per-type stringification. Python's `True` must not pass through as-is."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


__all__ = ["ServerSession", "McpProtocolError"]
