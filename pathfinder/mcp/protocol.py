"""The narrow end of MCP: envelopes, error kinds, the tool-naming rule.

Everything here is dictated by the spec; no judgment calls live in this file (those
belong to policy.py and the daemon). Keeping that boundary means a spec revision
has one place to fix.

Baseline revision: 2026-07-28. It dropped three things the older revision had:
an `initialize` handshake (stateless now; every request carries its own `_meta`),
`Mcp-Session-Id` (no session concept), and the GET/resume stream. But real stdio
servers still speak the old revision, so both are supported (see `Lifecycle`,
and the dual-era probe in `session.py`).

The two error kinds matter: a tool that failed (`isError: true`) is a normal
result, not an exception - the model needs to see it to retry with better
arguments (one layer up, `mcp.tools` raises `McpToolError` for it, so the broker
records a failed call). An exception here means the *protocol* broke, not the tool call.
"""
from __future__ import annotations

import base64
import re
from enum import Enum
from typing import Any

# The revision we speak. The `MCP-Protocol-Version` header and the `_meta` value
# in the body MUST match, or the server rejects the request with 400.
PROTOCOL_VERSION = "2026-07-28"

# The revision we propose under the legacy handshake. The server may pick a
# different one; when it does, we use whatever it picked.
LEGACY_VERSION = "2025-06-18"


class Lifecycle(str, Enum):
    """Which opening line the session speaks - independent of the transport.

    `stdio` and `http` each carry either era; the two axes multiply. This is
    why the handshake choice lives in `session.py`, not in a transport class.
    """

    STATELESS = "stateless"   # 2026-07-28: no handshake, every request self-describes
    LEGACY = "legacy"         # 2025-06-18 or earlier: initialize -> initialized -> the rest


CLIENT_NAME = "pathfinder"
CLIENT_VERSION = "0.1.0"

# The transport re-reads these keys out of the body to fill headers - they must
# agree, so the header is always derived from the body, never set independently.
META_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
_META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"

# Declared client capabilities. **Left empty on purpose.**
#
# Declaring `elicitation` (the server asking the human for input) would open a
# second "ask the human" path next to the approval gate, and it would no longer
# be clear which path a given question went through. Not declaring it means
# servers don't use it.
CLIENT_CAPABILITIES: dict[str, Any] = {}

# Prefix for tool names. The spec asks a client that merges several servers to
# prefix by server identity (SHOULD), and warns that `serverInfo.name` is not
# guaranteed unique - so this uses the id we assign in our own config instead.
#
# The separator is `__`, not `.`: the spec allows dots in tool names (its own
# example is `admin.tools.list`), and most providers' function-name rules
# reject dots. Server ids are forbidden from containing `_` (config.py), so
# splitting on the first `__` is always unambiguous.
NAMESPACE = "mcp"
SEPARATOR = "__"


class McpError(Exception):
    """Root of every error this package raises."""


class McpTransportError(McpError):
    """Could not talk to the server at all - connection failure, timeout, or an
    unreadable response."""


class McpProtocolError(McpError):
    """The server returned a JSON-RPC `error` object - the request itself was
    invalid, or the server does not support it.

    A failed tool call (`isError: true`) does **not** raise this - that is a
    normal result.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


def envelope(method: str, params: dict[str, Any] | None = None,
             *, request_id: int | None,
             lifecycle: Lifecycle = Lifecycle.STATELESS) -> dict[str, Any]:
    """Build one JSON-RPC message.

    Under the stateless revision, **every request carries its own version and
    capabilities** (`_meta`) instead of relying on a one-time handshake. Under
    the legacy revision that agreement already happened during `initialize`,
    so `_meta` is omitted - adding it would just be an unknown key.

    `request_id=None` means a notification (e.g. `notifications/initialized`):
    JSON-RPC identifies notifications by the absence of `id`, and the server
    never replies to one.
    """
    body = dict(params or {})
    if lifecycle is Lifecycle.STATELESS:
        body["_meta"] = {
            META_VERSION: PROTOCOL_VERSION,
            _META_CLIENT_INFO: {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            _META_CLIENT_CAPS: CLIENT_CAPABILITIES,
        }
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": body}
    if request_id is not None:
        message["id"] = request_id
    return message


def initialize_params() -> dict[str, Any]:
    """Arguments for the legacy handshake.

    The same three things the stateless revision sends every time (version,
    identity, capabilities), just in a different spot - `capabilities` is
    empty here too, for the same reason as `CLIENT_CAPABILITIES`.
    """
    return {
        "protocolVersion": LEGACY_VERSION,
        "capabilities": CLIENT_CAPABILITIES,
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
    }


def read_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull `result` out of a reply envelope. Raises on `error`."""
    if "error" in payload:
        err = payload["error"] or {}
        raise McpProtocolError(int(err.get("code", 0)),
                               str(err.get("message", "(no message)")),
                               err.get("data"))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise McpTransportError(f"result is not an object: {type(result).__name__}")
    # `input_required` means the server wants user input (MRTR). We never declare
    # elicitation so this should not happen - but if it does, stop here rather
    # than silently reading it as an empty result: pretending to know an unknown
    # shape makes everything after it a lie.
    kind = result.get("resultType")
    if kind not in (None, "complete"):
        raise McpProtocolError(-32000,
                               f"unsupported resultType: {kind}. "
                               "pathfinder does not declare elicitation.")
    return result


# --- tool names ---------------------------------------------------------------

def sanitize(text: str) -> str:
    """Strip control characters, keep newlines. Everything a server writes that a model or a
    terminal will read passes through here (a description, a held tool's line)."""
    return "".join(c for c in text if c.isprintable() or c == "\n")


def qualify(server_id: str, tool_name: str) -> str:
    """The full name, e.g. `mcp__github__search_issues`."""
    return f"{NAMESPACE}{SEPARATOR}{server_id}{SEPARATOR}{tool_name}"


def unqualify(qualified: str) -> tuple[str, str] | None:
    """Full name -> (server id, tool name within that server). `None` if it is
    not one of ours.

    Safe even if the tool's own name contains another `__` - only the first
    two splits matter.
    """
    head, sep, rest = qualified.partition(SEPARATOR)
    if not sep or head != NAMESPACE:
        return None
    server_id, sep, tool_name = rest.partition(SEPARATOR)
    if not sep or not server_id or not tool_name:
        return None
    return server_id, tool_name


# --- header value encoding -----------------------------------------------------

# The visible ASCII RFC 9110 allows. Outside that range, wrap in the base64 sentinel.
_HEADER_SAFE = re.compile(r"^[\x21-\x7E]([\x20-\x7E]*[\x21-\x7E])?$")
_SENTINEL = re.compile(r"^=\?base64\?.*\?=$")


def header_value(raw: str) -> str:
    """Shape a value for a header. Values that don't fit get wrapped as
    `=?base64?...?=`.

    A tool name or value with non-ASCII characters or whitespace would either
    break the header or make the server reject it (400, header vs. body
    mismatch) - the spec defines this sentinel format, so we use it. Plain text
    that already looks like the sentinel must also be wrapped, or the server
    would try to decode it.
    """
    if raw and _HEADER_SAFE.match(raw) and not _SENTINEL.match(raw):
        return raw
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="
