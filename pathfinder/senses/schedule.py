"""schedule sense - the user's own reminders, `cfg["schedules"]` (written only through
`config.set` - by `pathfinder config set` directly, or through this file's tools).
Polls every 30 s; a due row fires once (dropped if `repeat` is `none`, else `at` advances)."""
from __future__ import annotations

import uuid

from .. import clock, config
from ..signals import Signal
from ..tools import ToolSpec, normalize_local_datetime

OPTIONS: dict = {}   # nothing to configure - the schedule list itself lives in the config file
TRUST = "user"       # the user wrote the reminder's text
CONDITIONS: dict = {}  # none beyond core
KINDS = {"schedule_due": ("always", "-", "silence", "no rule configured for this kind")}
CADENCE_S = 30


def poll(options, now):
    home = options.get("_home")
    if home is None:
        return []
    cfg = config.load(home)
    rows = cfg.get("schedules", [])
    fired, remaining, changed = [], [], False
    for row in rows:
        due_ts, _ = clock.parse_local(row["at"])
        if due_ts > now:
            remaining.append(row)
            continue
        changed = True
        fired.append(Signal(
            sense="schedule", kind="schedule_due", key=f"schedule:{row['id']}:{row['at']}",
            summary=f"Scheduled note: {row['text']}",
            fields={"id": row["id"], "text": row["text"], "at": row["at"]}, trust=TRUST))
        if row["repeat"] != "none":
            remaining.append({**row, "at": clock.format_local(clock.next_repeat(due_ts, row["repeat"]))})
        # a one-shot row that fired is simply dropped
    if changed:
        config.set(home, "schedules", remaining)
    return fired


def _add_schedule(at, text, repeat="none", control=None):
    home = config.home()
    rows = list(config.load(home).get("schedules", []))
    row = {"id": uuid.uuid4().hex[:8], "at": at, "repeat": repeat, "text": text}
    rows.append(row)
    config.set(home, "schedules", rows)
    return row


def _list_schedules(control=None):
    return config.load(config.home()).get("schedules", [])


def _remove_schedule(id, control=None):
    home = config.home()
    rows = [r for r in config.load(home).get("schedules", []) if r["id"] != id]
    config.set(home, "schedules", rows)
    return {"removed": id}


TOOLS = {
    "add_schedule": ToolSpec(
        name="add_schedule", description="Add a scheduled reminder, once or on a repeat.",
        params={
            "at": {"type": "string", "format": "local_datetime", "description": "local time, YYYY-MM-DD HH:MM, no offset"},
            "text": {"type": "string", "description": "what to say when it fires"},
            "repeat": {"type": "string", "description": "none|daily|weekly"},
        },
        required=["at", "text"], handler=_add_schedule, normalize=normalize_local_datetime("at"),
        write=True, reversible=True,
    ),
    "list_schedules": ToolSpec(
        name="list_schedules", description="List scheduled reminders.",
        params={}, required=[], handler=_list_schedules,
    ),
    "remove_schedule": ToolSpec(
        name="remove_schedule", description="Remove a scheduled reminder by id.",
        params={"id": {"type": "string", "description": "the schedule's id, from list_schedules"}},
        required=["id"], handler=_remove_schedule, write=True, reversible=True,
    ),
}
