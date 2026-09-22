"""The one place local wall-clock time is computed. `ledger.partition()` stays the only
UTC-day-to-file mapping; everything that reads a *local* calendar (quiet hours, the daily
budget's day, the "Local time now" line the model needs to turn "tomorrow at 9" into a
`local_datetime`) lives here and nowhere else - test_invariants checks the whole package for
this, ledger.py excepted."""
from __future__ import annotations

import re
import time
from datetime import datetime

_WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}$")


def _zone_name() -> str:
    # ponytail: /etc/localtime is macOS/Linux; a system with no such symlink (or none set up)
    # falls back to the abbreviation - good enough for a line the model reads, not a spec.
    try:
        import os
        link = os.readlink("/etc/localtime")
        idx = link.find("zoneinfo/")
        if idx != -1:
            return link[idx + len("zoneinfo/"):]
    except OSError:
        pass
    return time.strftime("%Z") or "local"


def now_line(now: float | None = None) -> str:
    """The system message every turn gets: 'Local time now: 2026-09-22 15:04 (Tue), Asia/Seoul'."""
    dt = datetime.fromtimestamp(now if now is not None else time.time())
    return f"Local time now: {dt.strftime('%Y-%m-%d %H:%M')} ({_WEEKDAY[dt.weekday()]}), {_zone_name()}"


def format_local(ts: float) -> str:
    """The canonical local string for a timestamp - the inverse of `parse_local`, used by a
    sense that computes the next `at` of a repeating schedule."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def local_day(ts: float | None = None) -> str:
    """The local calendar date of `ts` - the budget's and the dedupe's day. A rate limit, so
    losing it at a day boundary (a restart, a DST jump) costs nothing, unlike a cursor."""
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d")


def day_start(ts: float | None = None) -> float:
    """Timestamp of local midnight on `ts`'s calendar day - the boundary `state.today()` counts
    from."""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time())
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def hour(ts: float | None = None) -> int:
    """The local hour (0-23) - what quiet_hours compares against."""
    return datetime.fromtimestamp(ts if ts is not None else time.time()).hour


def parse_local(text: str) -> tuple[float, str]:
    """Parse a `local_datetime` argument: `YYYY-MM-DD HH:MM` or with a `T`. Refuses a trailing
    `Z` or a `+HH:MM`/`-HH:MM` offset - the model must write local wall-clock time, never UTC.
    Returns (timestamp, canonical string) so the gate and RULES.md show exactly when it lands."""
    s = (text or "").strip()
    if s.endswith("Z") or _OFFSET_RE.search(s):
        raise ValueError("local_datetime must be local wall-clock time without an offset (no Z, no +HH:MM)")
    s = s.replace("T", " ", 1)
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("local_datetime must look like YYYY-MM-DD HH:MM")
    return time.mktime(dt.timetuple()), dt.strftime("%Y-%m-%d %H:%M")


def minutes_until(ts: float, now: float | None = None) -> float:
    return (ts - (now if now is not None else time.time())) / 60


def next_repeat(ts: float, repeat: str) -> float:
    """Advance a fired schedule's `at` by one period. Plain seconds, not a calendar add: a
    schedule can drift an hour across a DST boundary - a known ceiling, not a bug to chase here."""
    if repeat == "daily":
        return ts + 86400
    if repeat == "weekly":
        return ts + 7 * 86400
    return ts
