"""schedule sense - the user's own reminders, `cfg["schedules"]` (written only through
`config.set` - by `pathfinder config set` directly, or through this file's tools).
Polls every 30 s; a due row fires once (dropped if `repeat` is `none`, else `at` advances)."""
from __future__ import annotations

import threading
import uuid

from .. import clock, config
from ..signals import Signal
from ..tools import ToolSpec, normalize_local_datetime, oneline

OPTIONS: dict = {}   # nothing to configure - the schedule list itself lives in the config file
TRUST = "user"       # the user wrote the reminder's text
CONDITIONS: dict = {}  # none beyond core
KINDS = {"schedule_due": ("always", "-", "silence", "no rule configured for this kind")}
CADENCE_S = 30

# `signals.load` binds this to the daemon's own home - the poll below and every tool handler
# read it from here, never from `config.home()`, so both ever answer with the same home even
# when PATHFINDER_HOME (what config.home() reads) differs from what the daemon was actually
# given (true in most tests, and the review's own repro).
HOME = None

# ponytail: process-local lock; the CLI's `config set` can still race with this and lose an
# update - add an flock (like ledger.append's) around the read-modify-write if that is observed.
_LOCK = threading.Lock()


def poll(options, now):
    home = options.get("_home")
    if home is None:
        return []
    with _LOCK:
        rows = config.load(home).get("schedules", [])
        fired, remaining, changed = [], [], False
        for row in rows:
            due_ts, _ = clock.parse_local(row["at"])
            if due_ts > now:
                remaining.append(row)
                continue
            changed = True
            fired.append(Signal(
                sense="schedule", kind="schedule_due", key=f"schedule:{row['id']}:{row['at']}",
                summary=f"Scheduled note: {oneline(row['text'])}",
                fields={"id": row["id"], "text": row["text"], "at": row["at"]}, trust=TRUST))
            if row["repeat"] != "none":
                # Catch up past every missed occurrence (a laptop closed for a week) in one
                # loop, but emit only the ONE signal above for the whole run - not one per
                # missed day.
                next_ts = due_ts
                while next_ts <= now:
                    next_ts = clock.next_repeat(next_ts, row["repeat"])
                remaining.append({**row, "at": clock.format_local(next_ts)})
            # a one-shot row that fired is simply dropped
        if changed:
            config.set(home, "schedules", remaining)
    return fired


def _require_home():
    if HOME is None:
        raise RuntimeError("schedule sense not loaded (no home bound)")
    return HOME


def _add_schedule(at, text, repeat="none", control=None):
    home = _require_home()
    row = {"id": uuid.uuid4().hex[:8], "at": at, "repeat": repeat, "text": text}
    with _LOCK:
        rows = list(config.load(home).get("schedules", []))
        rows.append(row)
        config.set(home, "schedules", rows)
    return row


def _list_schedules(control=None):
    return config.load(_require_home()).get("schedules", [])


def _remove_schedule(id, control=None):
    home = _require_home()
    with _LOCK:
        rows = config.load(home).get("schedules", [])
        kept = [r for r in rows if r["id"] != id]
        if len(kept) == len(rows):  # live 2026-09-26: a Reminders id came back "removed", three times
            raise LookupError(f"no schedule with id {id!r}; ids come from list_schedules")
        config.set(home, "schedules", kept)
    return {"removed": id}


TOOLS = {
    "add_schedule": ToolSpec(
        name="add_schedule", description="Add a scheduled note PATHFINDER says at a time, once or on a repeat (not a Reminders item).",
        params={
            "at": {"type": "string", "format": "local_datetime", "description": "local time, YYYY-MM-DD HH:MM, no offset"},
            "text": {"type": "string", "description": "what to say when it fires"},
            "repeat": {"type": "string", "description": "none|daily|weekly"},
        },
        required=["at", "text"], handler=_add_schedule, normalize=normalize_local_datetime("at"),
        write=True, reversible=True,
    ),
    "list_schedules": ToolSpec(
        name="list_schedules", description="List scheduled notes.",
        params={}, required=[], handler=_list_schedules,
    ),
    "remove_schedule": ToolSpec(
        name="remove_schedule", description="Remove a scheduled note by id.",
        params={"id": {"type": "string", "description": "the schedule's id, from list_schedules"}},
        required=["id"], handler=_remove_schedule, write=True, reversible=True,
    ),
}
