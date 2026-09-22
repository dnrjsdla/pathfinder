"""macOS calendar/reminders via EventKit (PyObjC) - trimmed from Mark.5's
`brain/bridges/eventkit_bridge.py` to just what `calendar.py` and `reminders.py` need: list
events in a window, add an event, list incomplete reminders (optionally due before a time), add
a reminder. No update, delete, recurrence, alarms or priority - out of scope for this partition.

Timestamps in and out are plain epoch floats (`time.time()`-comparable), not ISO strings - the
callers already hold a `clock.parse_local` timestamp, so there is no second date format to keep
in sync with EventKit's own.

PyObjC (`EventKit`, `Foundation`) is imported lazily inside each function that needs it, so
importing this module never fails on a machine without it - only calling it does."""
from __future__ import annotations

import threading

from .. import clock

_ENTITY_EVENT, _ENTITY_REMINDER = 0, 1  # EKEntityType: stable across EventKit versions
_ACCESS_TIMEOUT = 60.0
_FETCH_TIMEOUT = 30.0

_store = None


def _get_store():
    from EventKit import EKEventStore
    global _store
    if _store is None:
        _store = EKEventStore.alloc().init()
    return _store


def _to_nsdate(ts: float):
    from Foundation import NSDate
    return NSDate.dateWithTimeIntervalSince1970_(ts)


def _from_nsdate(nsdate) -> float | None:
    return None if nsdate is None else nsdate.timeIntervalSince1970()


def ensure_access(entity: str) -> None:
    """entity 'event'|'reminder', full access - a write tool reads first (dedup, matching), so
    writeOnly is refused the same as denied (Mark.5's own note: a quiet empty list there reads
    as "nothing exists", not "can't look")."""
    from EventKit import EKEventStore
    ent_type = _ENTITY_EVENT if entity == "event" else _ENTITY_REMINDER
    store = _get_store()
    status = EKEventStore.authorizationStatusForEntityType_(ent_type)
    if status == 3:  # authorized / fullAccess
        return
    if status != 0:  # restricted / denied / writeOnly - not recoverable here
        raise PermissionError(f"{entity} access is not authorized (status {status}); allow it in "
                              f"System Settings > Privacy & Security > {entity.capitalize()}s")
    done = threading.Event()
    box = {}

    def handler(granted, error=None):
        box["granted"] = bool(granted)
        done.set()

    if entity == "event" and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    elif entity == "reminder" and hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
        store.requestFullAccessToRemindersWithCompletion_(handler)
    else:  # older macOS
        store.requestAccessToEntityType_completion_(ent_type, handler)
    if not done.wait(_ACCESS_TIMEOUT):
        raise TimeoutError(f"{entity} access prompt timed out")
    if not box.get("granted"):
        raise PermissionError(f"{entity} access was denied")


def list_events(start_ts: float, end_ts: float) -> list:
    ensure_access("event")
    store = _get_store()
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        _to_nsdate(start_ts), _to_nsdate(end_ts), None)
    out = [{
        "id": str(ev.eventIdentifier() or ""),
        "title": str(ev.title() or ""),
        "start": _from_nsdate(ev.startDate()),
        "end": _from_nsdate(ev.endDate()),
        "all_day": bool(ev.isAllDay()),
        "notes": str(ev.notes() or "") or None,
    } for ev in (store.eventsMatchingPredicate_(pred) or [])]
    out.sort(key=lambda e: e["start"] or 0)
    return out


def add_event(title: str, start_ts: float, end_ts: float) -> dict:
    from EventKit import EKEvent
    ensure_access("event")
    store = _get_store()
    ev = EKEvent.eventWithEventStore_(store)
    ev.setCalendar_(store.defaultCalendarForNewEvents())
    ev.setTitle_(str(title))
    ev.setStartDate_(_to_nsdate(start_ts))
    ev.setEndDate_(_to_nsdate(end_ts))
    ok, err = store.saveEvent_span_error_(ev, 0, None)  # span=thisEvent
    if not ok:
        raise RuntimeError(f"could not save the event: {err}")
    return {"id": str(ev.eventIdentifier() or ""), "title": str(title), "start": start_ts, "end": end_ts}


def _fetch_incomplete_reminders(store) -> list:
    pred = store.predicateForRemindersInCalendars_(None)
    done = threading.Event()
    box = {}

    def handler(reminders):
        box["reminders"] = reminders
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(pred, handler)
    if not done.wait(_FETCH_TIMEOUT):
        raise TimeoutError("reminder fetch timed out")
    return [r for r in (box.get("reminders") or []) if not r.isCompleted()]


def list_reminders(due_before_ts: float | None = None) -> list:
    """Incomplete reminders, optionally only those due at or before `due_before_ts`."""
    from Foundation import NSCalendar
    ensure_access("reminder")
    out = []
    for r in _fetch_incomplete_reminders(_get_store()):
        comps = r.dueDateComponents()
        due = _from_nsdate(NSCalendar.currentCalendar().dateFromComponents_(comps)) if comps else None
        if due_before_ts is not None and (due is None or due > due_before_ts):
            continue
        out.append({"id": str(r.calendarItemIdentifier() or ""), "title": str(r.title() or ""), "due": due})
    return out


def add_reminder(title: str, due_ts: float) -> dict:
    from Foundation import NSDateComponents
    from EventKit import EKReminder
    ensure_access("reminder")
    store = _get_store()
    rem = EKReminder.reminderWithEventStore_(store)
    rem.setCalendar_(store.defaultCalendarForNewReminders())
    rem.setTitle_(str(title))
    # clock.py is the one place a timestamp becomes a local date - parse its own string back
    # apart rather than computing the y/m/d/h/m breakdown here a second way.
    date_part, time_part = clock.format_local(due_ts).split(" ")
    year, month, day = (int(x) for x in date_part.split("-"))
    hour, minute = (int(x) for x in time_part.split(":"))
    comps = NSDateComponents.alloc().init()
    comps.setYear_(year)
    comps.setMonth_(month)
    comps.setDay_(day)
    comps.setHour_(hour)
    comps.setMinute_(minute)
    rem.setDueDateComponents_(comps)
    ok, err = store.saveReminder_commit_error_(rem, True, None)
    if not ok:
        raise RuntimeError(f"could not save the reminder: {err}")
    return {"id": str(rem.calendarItemIdentifier() or ""), "title": str(title), "due": due_ts}
