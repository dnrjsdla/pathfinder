"""reminders sense - EventKit reminders (see `_eventkit.py`), split out from `calendar.py`
because its TRUST differs: a reminder is the user's own note, not someone else's words landing
on the calendar. Same off-without-PyObjC handling as calendar.py."""
from __future__ import annotations

from .. import clock
from ..signals import Signal
from ..tools import ToolSpec, normalize_local_datetime
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
_OFF = "reminders sense is off: install pyobjc-framework-EventKit"
_off_logged = False


def poll(options, now):
    global _off_logged
    try:
        reminders = ek.list_reminders(due_before_ts=now + _WINDOW_S)
    except ImportError:
        if _off_logged:
            return []
        _off_logged = True
        raise RuntimeError(_OFF) from None
    out = []
    for r in reminders:
        if r["due"] is None:
            continue
        kind = "overdue_reminder" if r["due"] < now else "due_reminder"
        out.append(Signal(
            sense="reminders", kind=kind, key=f"reminder:{r['id']}:{r['due']}",
            summary=f"Reminder due: {r['title']}",
            fields={"title": r["title"], "due": clock.format_local(r["due"]),
                    "minutes_until": clock.minutes_until(r["due"], now)},
            trust=TRUST))
    return out


def _list_reminders(control=None):
    try:
        return ek.list_reminders()
    except ImportError:
        raise RuntimeError(_OFF) from None


def _add_reminder(title, due, control=None):
    try:
        due_ts, _ = clock.parse_local(due)
        return ek.add_reminder(title, due_ts)
    except ImportError:
        raise RuntimeError(_OFF) from None


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
}
