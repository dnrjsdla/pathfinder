"""The daemon's MCP seam: load the configured servers, pin-check, register passing tools,
hold the rest as `accept_pins` proposals, reload one server after an approve."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from .. import config, signals, tools
from . import pins as mcp_pins
from . import servers as mcp_servers
from . import tools as mcp_tools


class _McpLogHandler(logging.Handler):
    """`pathfinder.mcp`'s WARNING+ lines -> one `error` event each, through `dispatch` (may fire off the loop thread)."""

    def __init__(self, dispatch, record):
        super().__init__(level=logging.WARNING)
        self._dispatch = dispatch
        self._record = record

    def emit(self, record):
        message = record.getMessage()[:300]
        self._dispatch(lambda: self._record("error", origin="system", message=message))


class Attach:
    """The daemon's MCP seam: load the configured servers, pin-check, register passing tools into
    tools.TOOLS, hold the rest as `accept_pins` proposals, reload one server after an approve."""

    def __init__(self, home, state, record, push_signal, dispatch):
        self.home = home
        self.state = state
        self.record = record
        self.push_signal = push_signal
        self.dispatch = dispatch
        # The handler must be listening before `mcp_servers.load()` runs below - a secret
        # missing at load time logs its WARNING right there, not lazily.
        self._log_handler = _McpLogHandler(lambda fn: self.dispatch(fn), record)
        logging.getLogger("pathfinder.mcp").addHandler(self._log_handler)
        self.servers = mcp_servers.Servers(mcp_servers.load(config.load(home)))  # no process spawns yet
        self.loaded = None  # the daemon's `start()` sets this to the background `load()` task

    async def load(self) -> None:
        """Off the loop thread (a stdio server is a child process): discover, pin-check, register."""
        self.register(await asyncio.to_thread(self.servers.entries))

    async def reload(self, server_id: str) -> None:
        """One server, no restart - the installer's path after an approve, `accept_pins`'s once it has recorded the hashes."""
        if self.loaded is not None and not self.loaded.done():
            await self.loaded  # never overlap with the initial load's own entries()/register() call
        cfg = config.load(self.home)
        match = next((c for c in mcp_servers.load(cfg) if c.server_id == server_id), None)
        if match:
            self.servers.load_one(match)
        entries = await asyncio.to_thread(self.servers.entries)
        self.register([e for e in entries if e.server_id == server_id], server_id=server_id)

    def register(self, entries, *, server_id=None) -> None:
        """Pin-check; replace stale `mcp__` keys (scoped to `server_id`, never accumulating); propose+signal each held server."""
        store = mcp_pins.PinStore.load(self.state)
        report = mcp_pins.check(entries, store)
        store.save(self.record)
        for name in [n for n in tools.TOOLS if n.startswith("mcp__")
                    and (server_id is None or n.startswith(f"mcp__{server_id}__"))]:
            del tools.TOOLS[name]
        tools.TOOLS.update(mcp_tools.specs(entries, self.servers, report))

        by_name = {e.qualified_name: e for e in entries}
        by_server: dict[str, list] = {}
        for held in report.held:
            by_server.setdefault(held.server_id, []).append(held)
        pending = {(r["args"]["server"], json.dumps(r["args"]["tools"], sort_keys=True))
                   for r in self.state.pending() if r["tool"] == "accept_pins"}
        for sid, held_list in by_server.items():
            aid = uuid.uuid4().hex[:12]
            prints = {h.qualified_name: mcp_pins.fingerprint(by_name[h.qualified_name].definition) for h in held_list}
            rows = [{"name": h.tool_name, "description_hash": prints[h.qualified_name]["description"],
                    "schema_hash": prints[h.qualified_name]["inputSchema"],
                    "params": mcp_pins.param_names(by_name[h.qualified_name].definition)} for h in held_list]
            if (sid, json.dumps(rows, sort_keys=True)) in pending:
                continue  # the same hold already waits in the box: a restart is not news (review)
            self.record("approval_pending", origin="system", id=aid, call_id=aid, session="", turn="", tool="accept_pins",
                        args={"server": sid, "held": [h.line() for h in held_list], "tools": rows}, messages=[])
            self.push_signal(signals.Signal(sense="catalog", kind="tool_held", key=f"tool_held:{sid}:{aid}",
                summary=f"Held {len(held_list)} tool(s) of '{sid}': description or schema changed - approve {aid} to accept",
                fields={"server": sid, "held": len(held_list), "approval": aid}, trust="self"))

    async def accept_pins(self, approval_id: str, decision: str) -> None:
        """approved: one `pin_accept` per tool, then reload the server. denied: nothing."""
        row = self.state.approval(approval_id)
        if row is None or decision != "approved":
            return
        server_id = row["args"]["server"]
        for tool in row["args"]["tools"]:
            self.record("pin_accept", server=server_id, tool=tool["name"], description_hash=tool["description_hash"],
                        schema_hash=tool["schema_hash"], params=tool["params"])
        await self.reload(server_id)

    def close(self) -> None:
        self.servers.close()
        [tools.TOOLS.pop(n) for n in list(tools.TOOLS) if n.startswith("mcp__")]  # TOOLS is shared, module-wide
        logging.getLogger("pathfinder.mcp").removeHandler(self._log_handler)
