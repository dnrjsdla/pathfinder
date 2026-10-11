"""reminders sense - EventKit reminders (see `_eventkit.py`), split out from `calendar.py`
because its TRUST differs: a reminder is the user's own note, not someone else's words landing
on the calendar. Same off-without-PyObjC handling as calendar.py."""
from __future__ import annotations

from .. import clock
from ..signals import Signal
from ..tools import ToolSpec, normalize_local_datetime, oneline
from . import _eventkit as ek

OPTIONS: dict = {}
TRUST = "user"
KINDS = {
    "due_reminder": ("always", "-", "silence", "no rule configured for this kind"),
    "overdue_reminder": ("always", "-", "silence", "no rule configured for this kind"),
}
CONDITIONS: dict = {}  # none beyond core (minutes_until_under, minutes_over)
CADENCE_S = 300
_WINDOW_S = 3600
_AGENDA_S, _AGENDA_MAX = 86400, 10  # D42: what every turn carries
_OFF = "reminders sense is off: install pyobjc-framework-EventKit"
# keys this process has signalled: an overdue reminder stays in the window until done (live: one
# re-signalled 98 times a day), so each key speaks once per process, like mail's `_last_uid`.
# ponytail: process-local, so a restart re-signals each open key once (the 24 h dedupe holds it
# after that); persist it if restarts get frequent.
_announced: set = set()


def poll(options, now):
    try:
        reminders = ek.list_reminders(due_before_ts=now + _WINDOW_S)
    except ImportError:
        raise RuntimeError(_OFF) from None  # every poll; SenseLoop logs a repeated error once
    out = []
    for r in reminders:
        key = f"reminder:{r['id']}:{r['due']}"
        if r["due"] is None or key in _announced:
            continue
        kind = "overdue_reminder" if r["due"] < now else "due_reminder"
        title = oneline(r["title"])
        out.append(Signal(
            sense="reminders", kind=kind, key=key,
            summary=f"Reminder due: {title}",
            fields={"title": title, "due": clock.format_local(r["due"]),
                    "minutes_until": clock.minutes_until(r["due"], now)},
            trust=TRUST))
        _announced.add(key)  # after the append: a Signal that failed to build is retried next poll
    return out


def context(now):
    """D42: open reminders due within 24 h or overdue, soonest first."""
    if not ek.authorized("reminder"):
        return ["- reminders: not readable (access not granted yet)"]
    rems = sorted(ek.list_reminders(due_before_ts=now + _AGENDA_S), key=lambda r: r["due"])
    lines = [f"- reminder {clock.format_local(r['due'])}{' (overdue)' if r['due'] < now else ''}: "
             f"{oneline(r['title'], 80) or '(untitled)'}" for r in rems[:_AGENDA_MAX]]
    if len(rems) > _AGENDA_MAX:
        lines.append(f"- {len(rems) - _AGENDA_MAX} more reminders (list_reminders)")
    return lines or ["- reminders due within 24 h or overdue: none"]


def _list_reminders(control=None):
    try:
        return [ek.for_model(r) for r in ek.list_reminders()]
    except ImportError:
        raise RuntimeError(_OFF) from None


def _add_reminder(title, due, control=None):
    try:
        due_ts, _ = clock.parse_local(due)
        return ek.for_model(ek.add_reminder(title, due_ts))
    except ImportError:
        raise RuntimeError(_OFF) from None


def _complete_reminder(id, control=None):
    try:
        return ek.complete_reminder(id)
    except ImportError:
        raise RuntimeError(_OFF) from None


def _delete_reminder(id, control=None):
    try:
        return ek.delete_reminder(id)
    except ImportError:
        raise RuntimeError(_OFF) from None


_ID = {"id": {"type": "string", "description": "the reminder's id, from list_reminders"}}

TOOLS = {
    "list_reminders": ToolSpec(
        name="list_reminders", description="List incomplete reminders.",
        params={}, required=[], handler=_list_reminders,
    ),
    "add_reminder": ToolSpec(
        name="add_reminder", description="Add a reminder.",
        params={
            "title": {"type": "string"},
            "due": {"type": "string", "format": "local_datetime", "description": "local time, YYYY-MM-DD HH:MM"},
        },
        required=["title", "due"], handler=_add_reminder,
        normalize=normalize_local_datetime("due"), write=True,  # not reversible: no config row to delete
    ),
    # D38: both ask the user first, like add_reminder (the owner: "ask me for approval")
    "complete_reminder": ToolSpec(
        name="complete_reminder", description="Mark a reminder done.",
        params=_ID, required=["id"], handler=_complete_reminder, write=True,
    ),
    "delete_reminder": ToolSpec(
        name="delete_reminder", description="Delete a reminder. To mark one done, use complete_reminder.",
        params=_ID, required=["id"], handler=_delete_reminder, write=True,
    ),
}
