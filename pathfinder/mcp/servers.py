"""Server configs merged into one tool catalog - this package's one outside door.

    config's "mcp_servers" setting  ->  load()  ->  ServerConfig[]  ->  Servers  ->  tools + call

A secret never sits in the config file (D6.1, CLAUDE.md): an `env`/`headers` value
of the form `keychain:<account>` is resolved through the macOS Keychain service
`pathfinder-mcp-<server_id>` at load time (`_resolve` has the whole grammar). A server
whose required secret is missing is not dropped - it is marked `disabled` and stays listed with the reason (Mark.5's rule:
"tools 0" and "never configured" must not look the same).

A server that fails at runtime does not take the others down with it - `entries()`
records the failure through the standard logger and moves on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import exec, keychain
from .protocol import Lifecycle, McpError, qualify, unqualify
from .session import ServerSession
from .transport import StdioTransport, StreamableHttpTransport, Transport

_log = logging.getLogger("pathfinder.mcp")


@dataclass(frozen=True)
class ServerConfig:
    server_id: str
    transport_kind: str = "http"
    lifecycle: Lifecycle | None = None  # None = derive from transport_kind (session's default)
    endpoint: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()  # argv, never a shell string
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 30
    # A configured server this process cannot use right now. Never dropped from the
    # list for this - "configured, secret missing" must look different from "never
    # configured" (Mark.5's rule).
    disabled: str = ""

    def transport(self) -> Transport:
        if self.transport_kind == "stdio":
            return StdioTransport(list(self.command), env=exec.child_env(self.env))
        return StreamableHttpTransport(self.endpoint, headers=self.headers)


@dataclass
class Entry:
    """One tool of one enabled server - what `mcp/tools.py` turns into a `ToolSpec`."""
    server_id: str
    tool_name: str
    qualified_name: str
    definition: dict[str, Any]


def _resolve(values: dict[str, str], server_id: str, lookup: Callable[[str, str], str | None]):
    """Swap `keychain:` values for the Keychain secret (U34): `keychain:<account>` is required,
    `keychain?:<account>` optional, and `=<default>` after either is the value used when the
    item is missing - the account ends at the first `=`. Every other value passes through
    literally - no `${VAR}` expansion; secrets live in the Keychain and nowhere else. Returns
    (resolved, missing, unset): the required names not found, which keep the server from
    starting, and the optional ones not found, which are simply not set. The installer's check
    reads an entry through this too, so the check and the start agree."""
    out: dict[str, str] = {}
    missing, unset = [], []
    for key, value in values.items():
        if not (isinstance(value, str) and value.startswith(("keychain:", "keychain?:"))):
            out[key] = value
            continue
        kind, _, rest = value.partition(":")
        account, eq, default = rest.partition("=")
        secret = lookup(f"pathfinder-mcp-{server_id}", account)
        if secret is None and eq:
            secret = default
        if secret is not None:
            out[key] = secret
        else:
            (unset if kind == "keychain?" else missing).append(key)
    return out, missing, unset


def load(cfg: dict, lookup: Callable[[str, str], str | None] = keychain.secret) -> list[ServerConfig]:
    """`cfg["mcp_servers"]` (already validated by `config.py`) -> one ServerConfig
    per entry, in the order they appear."""
    out = []
    for server_id, spec in (cfg.get("mcp_servers") or {}).items():
        kind = spec["transport"]
        timeout = spec.get("timeout", 30)
        lifecycle = Lifecycle(spec["protocol"]) if "protocol" in spec else None
        values, missing, _ = _resolve(spec.get("env" if kind == "stdio" else "headers") or {}, server_id, lookup)
        reason = f"secret missing: {', '.join(missing)}" if missing else ""
        if kind == "stdio":
            out.append(ServerConfig(server_id=server_id, transport_kind=kind, lifecycle=lifecycle,
                                    command=tuple(spec["command"]), env=values, timeout=timeout,
                                    disabled=reason))
        else:
            out.append(ServerConfig(server_id=server_id, transport_kind=kind, lifecycle=lifecycle,
                                    endpoint=spec["url"], headers=values, timeout=timeout,
                                    disabled=reason))
    return out


class Servers:
    """Every enabled server's tools as one catalog. What the outside knows ends here -
    transport and protocol never leak past `entries()`/`call()`."""

    def __init__(self, configs: list[ServerConfig] | None = None) -> None:
        self._sessions: dict[str, ServerSession] = {}
        self._disabled: dict[str, str] = {}
        for cfg in configs or []:
            self.load_one(cfg)

    def load_one(self, cfg: ServerConfig) -> None:
        """Add one server to a running set - the installer's path after an approve,
        no restart needed. Replacing a server already running closes its old session
        first - otherwise that session's child process is dropped on the floor."""
        old = self._sessions.pop(cfg.server_id, None)
        if old is not None:
            old.close()
        if cfg.disabled:
            # `attach.reload` re-resolves every configured server, so this runs again for a
            # server whose secret was already reported missing - log it once per id, not once
            # per reload (the daemon's `error` event then appears once per start, not per reload).
            if self._disabled.get(cfg.server_id) != cfg.disabled:
                _log.warning("%s: %s", cfg.server_id, cfg.disabled)
            self._disabled[cfg.server_id] = cfg.disabled
            return
        self._disabled.pop(cfg.server_id, None)  # re-enabled: the old reason no longer applies
        # `cfg.lifecycle` is None unless config set an explicit `protocol` override -
        # passing None through lets ServerSession guess by transport and self-correct
        # once via the dual-era probe (`discover()`); an explicit value is never probed.
        self._sessions[cfg.server_id] = ServerSession(
            cfg.server_id, cfg.transport(), timeout=cfg.timeout, lifecycle=cfg.lifecycle)

    def unload(self, server_id: str) -> None:
        """Close and fully forget a server - the installer's uninstall/rollback path."""
        session = self._sessions.pop(server_id, None)
        if session is not None:
            session.close()
        self._disabled.pop(server_id, None)

    def disabled(self) -> list[tuple[str, str]]:
        """Configured servers this process cannot use, and why."""
        return list(self._disabled.items())

    def entries(self) -> list[Entry]:
        """Every tool of every enabled server. A server that fails to list is logged
        and skipped - the rest still come through."""
        out = []
        for server_id, session in list(self._sessions.items()):
            try:
                tools = session.list_tools()
            except McpError as exc:
                _log.warning("%s: could not list tools: %s", server_id, exc)
                continue
            for tool in tools:
                name = str(tool.get("name", ""))
                if not name:
                    continue
                out.append(Entry(server_id=server_id, tool_name=name,
                                 qualified_name=qualify(server_id, name), definition=tool))
        return out

    def call(self, qualified_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        split = unqualify(qualified_name)
        if split is None:
            raise McpError(f"not an MCP tool name: {qualified_name}")
        server_id, tool_name = split
        session = self._sessions.get(server_id)
        if session is None:
            raise McpError(f"server not attached: {server_id}")
        return session.call_tool(tool_name, arguments)

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
