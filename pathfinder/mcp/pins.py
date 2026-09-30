"""Pin tool definitions - the layer that catches a rug pull.

Spec 2026-07-28 has no signature or hash on a tool definition. Worse, `tools/list`
now requires `ttlMs`/`cacheScope`, which nudges a client toward caching and
re-fetching on expiry - if a server changes its description in between, the new
text slides straight into the system prompt. Catching that is entirely our job.

This file is a known_hosts file: the first thing seen is written down, and any
difference afterward stops and asks a person. Storage is the ledger (`state.pins()`,
`record("pin_accept", ...)`) - not a local file - so a pin survives `state.db`
deletion the same way everything else derived does.

What gets hashed: exactly what reaches the model - name (the dict key), description,
inputSchema. Not `annotations` or `outputSchema`, which we never show the model -
hashing those would raise an alarm over something nobody looked at, and an alarm
that fires on nothing gets ignored a few times in.

One tool, one hash - hashing the whole list as a blob would false-alarm on a mere
reorder, and the spec only SHOULDs a stable order from the server.

Hashed per field, not as one blob: "the description changed" vs. "the schema
changed" changes what a user should conclude - a schema change usually means a new
argument, a description change might be an injection.

The evidence a hold shows: for a description change, the incoming text is what
needs judging, and it just arrived from the server - so the old description is
never stored (storing it would mean keeping a full copy of every server's surface).
A schema change is different: the question is what changed, and the incoming value
alone cannot answer that. This bit a real case (GitHub's `list_issues`, cursor
pagination replacing offset pagination): the reason said "schema differs" but the
screen showed the description - a field that had not changed. The user was asked to
judge with no material to judge from, and a gate like that just teaches "accept
without reading". So the top-level argument names are pinned alongside the hashes -
names only (not type/enum/description) is enough to say "+after -page", and beyond
that is a copy again. This list is evidence, never a fingerprint: it plays no part
in the verdict (not in FIELDS) - the two hashes still decide that.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..tools import visible
from .protocol import sanitize

# The fields that go into the hash. This list IS the definition of "what the model sees".
FIELDS = ("description", "inputSchema")

# Evidence recorded alongside a pin. NOT a fingerprint (not in FIELDS).
PARAMS_KEY = "params"

_LABELS = {"description": "description", "inputSchema": "schema"}


def _digest(value: Any) -> str:
    """sha256 of normalized JSON. sort_keys so key order never moves the result."""
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprint(definition: dict[str, Any]) -> dict[str, str]:
    """Per-field fingerprint of this tool definition. This is the ONLY thing the verdict uses."""
    return {name: _digest(definition.get(name)) for name in FIELDS}


def param_names(definition: dict[str, Any]) -> list[str]:
    """This tool's top-level argument names.

    Not walking into nested objects is deliberate: this exists so a person can see
    at a glance what grew or shrank, and flattening nested objects would make this a
    copy too. A change inside a nested object is already caught by the inputSchema
    hash - this list stays the same in that case, and that itself is information
    ("the arguments are the same, but something inside changed").
    """
    schema = definition.get("inputSchema")
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    return sorted(props) if isinstance(props, dict) else []


@dataclass
class Held:
    """One tool that was not attached. Everything a person needs must be here.

    The evidence differs by reason: a description change needs the incoming text, a
    schema change needs how the arguments moved. Mixing the two and always showing
    the description means a schema change shows a field that did NOT change.
    """

    qualified_name: str
    server_id: str
    tool_name: str
    reason: str                 # one line for a person to read
    incoming_description: str   # the judgment target when the description changed
    # Which FIELDS changed. What evidence to show branches on this - `reason` is
    # never parsed for it, because then wording it more clearly would quietly break
    # the display logic.
    changed: tuple[str, ...] = FIELDS
    # The judgment target when the schema changed. `pinned_params` being None means
    # there is nothing to compare against (an old-format pin, or a tool seen for
    # the first time).
    incoming_params: list[str] = field(default_factory=list)
    pinned_params: list[str] | None = None

    def param_delta(self) -> str:
        """How the arguments moved. With no old list, only the incoming ones are said."""
        incoming = ", ".join(self.incoming_params) or "(none)"
        if self.pinned_params is None:
            return f"current arguments: {incoming}"
        added = [p for p in self.incoming_params if p not in self.pinned_params]
        removed = [p for p in self.pinned_params if p not in self.incoming_params]
        if not added and not removed:
            # Same names, different hash - type, enum, description or nesting changed.
            # Showing "no change" here would be wrong - that is information too.
            return f"argument names unchanged ({incoming}) - type, enum or argument description differs"
        parts = []
        if added:
            parts.append("added " + ", ".join("+" + p for p in added))
        if removed:
            parts.append("removed " + ", ".join("-" + p for p in removed))
        return " · ".join(parts)

    def line(self) -> str:
        out = [f"{sanitize(self.qualified_name)} - {self.reason}"]
        if "inputSchema" in self.changed:
            out.append(f"      arguments: {visible(self.param_delta())}")  # an approval line: shown as it runs
        body = " ".join(sanitize(self.incoming_description or "").split())
        if "description" in self.changed and body:
            out.append(f"      incoming description: {body[:200]}")
        return "\n".join(out)


@dataclass
class PinReport:
    """The comparison's result. A non-empty `held` means part of the surface is held back."""

    passed: set[str] = field(default_factory=set)      # qualified_name of every tool that passed
    held: list[Held] = field(default_factory=list)
    first_seen: dict[str, int] = field(default_factory=dict)  # server -> count pinned for the first time

    def summary(self) -> str:
        parts = []
        for server_id, count in self.first_seen.items():
            parts.append(f"{server_id}: {count} pinned for the first time")
        if self.held:
            parts.append(f"{len(self.held)} tool(s) held, definition changed")
        return " · ".join(parts)


class PinStore:
    """Pins in memory, backed by the ledger (not a file). `load()` reads
    `state.pins()`; `save()` records one `pin_accept` per entry that changed since
    the last save, so a daemon restart never re-records pins nothing changed on."""

    def __init__(self, servers: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._servers = servers
        self._dirty: set[tuple[str, str]] = set()

    @classmethod
    def load(cls, state) -> "PinStore":
        """Build from `state.pins()`: `{server: {tool: {"description": h, "schema": h,
        "params": [...]}}}` -> this store's own shape (`FIELDS` names, so `check()`
        can compare against `fingerprint()` output directly)."""
        servers: dict[str, dict[str, dict[str, Any]]] = {}
        for server_id, tools in state.pins().items():
            servers[server_id] = {
                tool_name: {"description": prints["description"], "inputSchema": prints["schema"],
                           PARAMS_KEY: prints["params"]}
                for tool_name, prints in tools.items()
            }
        return cls(servers)

    def knows(self, server_id: str) -> bool:
        """Has this server been seen before. If not, this is a first pin, not a change."""
        return server_id in self._servers

    def get(self, server_id: str, tool_name: str) -> dict[str, Any] | None:
        return (self._servers.get(server_id) or {}).get(tool_name)

    def set(self, server_id: str, tool_name: str, prints: dict[str, Any]) -> None:
        """Write both fingerprints plus the evidence (`params`), and mark the entry
        dirty so the next `save()` records it."""
        self._servers.setdefault(server_id, {})[tool_name] = prints
        self._dirty.add((server_id, tool_name))

    def save(self, record) -> None:
        """Record one `pin_accept` per dirty entry, then clear the dirty set - a
        daemon start that only calls `load()` + `check()` re-records nothing."""
        for server_id, tool_name in self._dirty:
            prints = self._servers[server_id][tool_name]
            record("pin_accept", server=server_id, tool=tool_name,
                  description_hash=prints["description"], schema_hash=prints["inputSchema"],
                  params=prints.get(PARAMS_KEY, []))
        self._dirty.clear()


def _changed_fields(old: dict[str, str], new: dict[str, str]) -> tuple[str, ...]:
    """Which FIELDS differ. Turning that into words for a person is the caller's job."""
    return tuple(f for f in FIELDS if old.get(f) != new.get(f))


def check(entries, store: PinStore, *, accept: bool = False) -> PinReport:
    """Compare what the servers sell right now against what is pinned.

    `accept=True` writes the incoming definitions as the new baseline - a person
    reviewed and accepted them (`pathfinder approvals`, the `accept_pins` row).
    Otherwise this only compares; anything else is held.

    A server seen for the first time is pinned wholesale and passes entirely -
    there is no way to have a user pre-approve text they have never seen. Setting
    the baseline is the first run's job; catching a CHANGE after that is what this
    actually protects.

    A tool that newly appears on a KNOWN server is held too - the description is
    already headed into the system prompt before the tool is ever called, so one
    new tool is one new block of text.
    """
    report = PinReport()
    by_server: dict[str, list] = {}
    for entry in entries:
        by_server.setdefault(entry.server_id, []).append(entry)

    for server_id, server_entries in by_server.items():
        known = store.knows(server_id)
        for entry in server_entries:
            prints = fingerprint(entry.definition)
            pinned = store.get(server_id, entry.tool_name)
            # A hand-edited pin could hold something other than a list. Then it
            # can't be compared - leaving it as a string would make `p not in "abc"`
            # a silently-wrong substring check.
            was = (pinned or {}).get(PARAMS_KEY)

            def hold(reason: str, changed: tuple[str, ...]) -> None:
                report.held.append(Held(
                    qualified_name=entry.qualified_name, server_id=server_id,
                    tool_name=entry.tool_name, reason=reason, changed=changed,
                    incoming_description=str(entry.definition.get("description") or ""),
                    incoming_params=param_names(entry.definition),
                    pinned_params=was if isinstance(was, list) else None))

            if accept or not known:
                store.set(server_id, entry.tool_name,
                         {**prints, PARAMS_KEY: param_names(entry.definition)})
                report.passed.add(entry.qualified_name)
                if not known:
                    report.first_seen[server_id] = report.first_seen.get(server_id, 0) + 1
                continue

            if pinned is None:
                # A tool never seen before - nothing to compare, description and
                # arguments are both new.
                hold("newly appeared on a known server", FIELDS)
                continue

            changed = _changed_fields(pinned, prints)
            if changed:
                hold(f"{'/'.join(_LABELS[f] for f in changed)} differs from what is pinned", changed)
                continue

            report.passed.add(entry.qualified_name)
    return report
