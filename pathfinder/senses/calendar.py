"""calendar sense - events via EventKit (see `_eventkit.py`). Off without PyObjC: `poll` logs
one `error` event through the daemon's normal sense-failure path (SenseLoop.on_error) the first
time, then quietly returns [] on every later poll - the calendar simply never speaks again,
rather than filling the ledger with the same "not installed" error every 5 minutes."""
from __future__ import annotations

from .. import clock
from ..signals import Signal
from ..tools import ToolSpec, normalize_local_datetime, oneline
from . import _eventkit as ek

OPTIONS: dict = {}
TRUST = "third_party"  # an invitation is someone else's words, even though it lands on the user's calendar
KINDS = {"upcoming_event": ("always", "-", "silence", "no rule configured for this kind")}
CADENCE_S = 300
_WINDOW_S = 3600  # look one hour ahead; RULES.md's minutes_until_under does the real filtering
_MAX_WINDOW_S = 31 * 86400  # list_events window cap - an unbounded range is an unbounded EventKit scan
_AGENDA_S, _AGENDA_MAX = 86400, 10  # D42: what every turn carries
_OFF = "calendar sense is off: install pyobjc-framework-EventKit"
_off_logged = False


def _all_day(signal, value, today, now):
    return signal.fields.get("all_day") is value


CONDITIONS = {"all_day": _all_day}


def poll(options, now):
    global _off_logged
    try:
        events = ek.list_events(now, now + _WINDOW_S)
    except ImportError:
        if _off_logged:
            return []
        _off_logged = True
        raise RuntimeError(_OFF) from None
    out = []
    for ev in events:
        if ev["start"] is None or ev["start"] < now:  # started: not upcoming (live: a holiday re-signalled 92 times a day)
            continue
        title = oneline(ev["title"])
        out.append(Signal(
            sense="calendar", kind="upcoming_event", key=f"event:{ev['id']}:{ev['start']}",
            summary=f"Upcoming: {clock.format_local(ev['start'])} {title}",
            fields={"title": title, "start": clock.format_local(ev["start"]),
                    "end": clock.format_local(ev["end"]) if ev["end"] else None,
                    "all_day": ev["all_day"], "notes": ev.get("notes") and oneline(ev["notes"]),
                    "minutes_until": clock.minutes_until(ev["start"], now)},
            trust=TRUST))
    return out


def context(now):
    """D42: the next 24 h, titles only and one line each - an invitation is third-party text."""
    if not ek.authorized("event"):
        return ["- calendar: not readable (access not granted yet)"]
    events = [ev for ev in ek.list_events(now, now + _AGENDA_S) if ev["start"] is not None]
    lines = [f"- event {clock.format_local(ev['start'])}{' (all day)' if ev['all_day'] else ''}: "
             f"{oneline(ev['title'], 80) or '(untitled)'}" for ev in events[:_AGENDA_MAX]]
    if len(events) > _AGENDA_MAX:
        lines.append(f"- {len(events) - _AGENDA_MAX} more events (list_events)")
    return lines or ["- calendar, next 24 h: nothing"]


def _list_events(control=None, **kw):
    from_ts, _ = clock.parse_local(kw["from"])
    to_ts, _ = clock.parse_local(kw["to"])
    if to_ts - from_ts > _MAX_WINDOW_S:
        raise ValueError("list_events window must not exceed 31 days")
    try:
        return [ek.for_model(ev) for ev in ek.list_events(from_ts, to_ts)]
    except ImportError:
        raise RuntimeError(_OFF) from None


def _add_event(title, start, end, control=None):
    start_ts, _ = clock.parse_local(start)
    end_ts, _ = clock.parse_local(end)
    if end_ts < start_ts:
        raise ValueError("end must not be before start")
    try:
        return ek.for_model(ek.add_event(title, start_ts, end_ts))
    except ImportError:
        raise RuntimeError(_OFF) from None


TOOLS = {
    "list_events": ToolSpec(
        name="list_events", description="List calendar events in a local time range.",
        params={
            "from": {"type": "string", "format": "local_datetime", "description": "local time, YYYY-MM-DD HH:MM"},
            "to": {"type": "string", "format": "local_datetime", "description": "local time, YYYY-MM-DD HH:MM"},
        },
        required=["from", "to"], handler=_list_events, normalize=normalize_local_datetime("from", "to"),
    ),
    "add_event": ToolSpec(
        name="add_event", description="Add a calendar event.",
        params={
            "title": {"type": "string"},
            "start": {"type": "string", "format": "local_datetime", "description": "local time, YYYY-MM-DD HH:MM"},
            "end": {"type": "string", "format": "local_datetime", "description": "local time, YYYY-MM-DD HH:MM"},
        },
        required=["title", "start", "end"], handler=_add_event,
        normalize=normalize_local_datetime("start", "end"), write=True,  # a calendar write may sync and invite - not reversible
    ),
}
