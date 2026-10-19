"""catalog sense - documents the four kinds the MCP tool path pushes: `tool_candidate` (this
file's own `poll`, below), `tool_held` (the daemon's pin check, on a rug pull), `install_done`/
`install_failed` (the installer, a later unit). Only `tool_candidate` is polled here.

`search_registry` (below) is registered only while this sense is on - `signals.load` merges a
sense's `TOOLS` into the shared registry only for senses named in `cfg["senses"]`."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .. import catalog, config, mirror
from .. import installer as installer_mod
from ..signals import Signal
from ..state import State
from ..tools import ToolSpec

_log = logging.getLogger("pathfinder.senses.catalog")

OPTIONS = {"registry": catalog.REGISTRY, "cadence_h": 24}
TRUST = "third_party"   # the registry's own text - someone else's words, not the user's or ours
CONDITIONS: dict = {}   # `count_over` is a CORE condition (rules.py) - nothing to add here
KINDS = {
    "tool_candidate": ("always", "-", "silence", "no rule configured for this kind"),
    "tool_held": ("always", "-", "silence", "no rule configured for this kind"),
    "install_done": ("always", "-", "silence", "no rule configured for this kind"),
    "install_failed": ("always", "-", "silence", "no rule configured for this kind"),
}
CADENCE_S = 24 * 3600

# `signals.load` sets this to the sense's configured `registry` option - `_search_registry` below
# gets no `options` (a tool handler never does), so it reads the registry from here instead,
# the way `schedule.py`'s tools read its module-level `HOME`.
REGISTRY_URL = None
HOME = None  # `signals.load` binds the home the same way (mirror.search reads <home>/registry.db)

# The chars `config._MCP_SERVER_ID` accepts after the first position - kept in sync by hand,
# the same alphabet, so a candidate's sanitized id matches what an install would register under.
_SEG_RE = re.compile(r"[^a-z0-9-]+")
_MAX_LICENSE_LOOKUPS = 10


def _server_id(name: str) -> str:
    seg = _SEG_RE.sub("-", name.rsplit("/", 1)[-1].lower()).strip("-")
    return seg[:32] or "server"


def poll(options, now, state=None):
    """`state` is normally opened here from `options["_home"]` (the door `schedule.py`/`mail.py`
    use for `_home`/the mail connection) - tests may pass one in directly instead."""
    home = options.get("_home")
    if home is None:
        return []
    owns_state = state is None
    if owns_state:
        state = State(Path(home) / "state.db")
    try:
        return _poll(options, now, state, home)
    finally:
        if owns_state:
            state.close()


def _poll(options, now, state, home):
    mirror.refresh(home, options.get("registry", catalog.REGISTRY))  # returns at once; the rows below are the last sync's
    tags = catalog.tags(state, home, now)
    if not tags:
        return []
    cfg = config.load(home) if home is not None else {}
    installed = set(cfg.get("mcp_servers") or {})
    project_names = {Path(c.rstrip("/")).name.lower() for c in state.context_cwds(now - 7 * 86400) if c}
    # Only this sense's own past candidates count as a "last poll" - `tool_held`/`install_*`
    # rows share `sense = 'catalog'` but are not a poll, and a pin held 30 days ago must not
    # pin `since` there forever for a poll that keeps finding nothing to report.
    row = state.db.execute(
        "SELECT MAX(ts) FROM signals WHERE sense = 'catalog' AND kind = 'tool_candidate'").fetchone()
    since = row[0] if row and row[0] is not None else now - options.get("cadence_h", 24) * 3600
    since = max(since, now - 7 * 86400)  # never look back further than a week - a deaf sense

    out, checks = [], 0
    for item in mirror.changed_since(home, since):
        try:
            cand = catalog.candidate(item)
        except (KeyError, TypeError) as exc:
            _log.warning("skipping malformed registry item: %s", exc)
            continue
        if cand["name"] != catalog.oneline(cand["name"], 200):
            continue  # a name that cannot be shown as it is is never proposed
        matched = catalog.matches(cand, tags)
        if not matched:
            continue
        if _server_id(cand["name"]) in installed or catalog.declined(state, cand["name"], now):
            continue
        if checks >= _MAX_LICENSE_LOOKUPS:
            break
        checks += 1
        if catalog.license(cand)["verdict"] != "ok":
            continue
        project = next((t for t in matched if t in project_names), matched[0])
        out.append(Signal(
            sense="catalog", kind="tool_candidate", key=f"catalog:{catalog.oneline(cand['name'], 200)}@{catalog.oneline(cand['version'], 40)}",
            summary=f"New tool for {project}: {catalog.oneline(cand['name'], 80)} - {catalog.oneline(cand['description'], 80)}",
            fields={"candidate": cand, "matched_tags": matched}, trust=TRUST))
    return out


def _search_registry(query, control=None):
    if not query.split():
        raise ValueError("search_registry needs English words for what the tool does or is called")
    items, note = mirror.search(HOME, query, REGISTRY_URL or catalog.REGISTRY)
    lines = [catalog.listing(item) for item in items]
    # live: an empty list read as "the registry has no such server", and the model offered to
    # edit config files by hand instead
    return (lines or [f"no server in the registry copy matches {catalog.oneline(query, 80)!r}; search English words "
                      "for what it does or is called (gmail, brave search, weather)"]) + ([note] if note else [])


def _propose_install(name, control=None):
    session = getattr(control, "session", None) or ""
    return installer_mod.propose_install_by_name(name, session, registry=REGISTRY_URL)


TOOLS = {
    "search_registry": ToolSpec(
        name="search_registry",
        description="Search the public MCP registry for servers by name and description (the user asks what tools "
                    "exist for X). The registry is in English: search English words for what the tool does or is called.",
        params={"query": {"type": "string", "description": "English words for what the tool does or is called (names and descriptions are searched): gmail, brave search, weather"}},
        required=["query"], handler=_search_registry, origins=frozenset({"user"}), network=True,
    ),
    "propose_install": ToolSpec(
        name="propose_install",
        description="Check and propose installing an MCP server by name (as search_registry or the registry shows "
                    "it) - runs the checks and queues one approval; tell the user what it found.",
        params={"name": {"type": "string", "description": "the server name"}},
        required=["name"], handler=_propose_install, origins=frozenset({"user"}), network=True,
    ),
}
