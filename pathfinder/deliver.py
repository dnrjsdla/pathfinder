"""When to hand a signal turn's notice over. A notice waits while the target session's turn is
running or the user is visibly typing (`presence`); once the way clears it goes out over the
socket if a client is attached, or as a macOS banner if not - either way it is recorded, so
history and a later `pathfinder listen` see it, and a daemon killed mid-outbox delivers on
restart (`rebuild`, from `state.undelivered()`)."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass


@dataclass
class Notice:
    id: str
    session: str
    text: str
    why: str
    signal_keys: list


def banner(text: str) -> bool:
    """The default `banner`: a macOS notification. Injectable - tests pass their own. Returns
    whether it succeeded; `ensure_ascii=False`, or a non-ASCII (e.g. Korean) notice becomes a
    literal `\\uXXXX` escape sequence that osascript cannot display."""
    script = f'display notification {json.dumps(text, ensure_ascii=False)} with title "PATHFINDER"'
    result = subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    return result.returncode == 0


class Outbox:
    """FIFO of `Notice`. `is_running`, `attached`, `send` and `record` are the daemon's own
    knowledge of its sessions and its ledger+state write - Outbox holds none of that itself,
    so it is testable without a socket."""

    def __init__(self, state, rules, *, is_running, attached, send, record, banner=banner):
        self.state = state
        self.rules = rules
        self.is_running = is_running   # session -> bool
        self.attached = attached       # session -> bool (any client on it)
        self.send = send               # (session, msg) -> None, over the socket
        self.record = record           # (type, **fields) -> event
        self.banner = banner
        self.queue: list[Notice] = []

    def rebuild(self) -> None:
        """Restore pending notices from the ledger (through `state`) - a daemon killed with a
        full outbox still holds them, queued, after restart. Delivery follows the normal
        `release()` triggers (a turn ending, a presence update), not an immediate flush here -
        so a restart never pops a banner behind the caller's back before anything else runs."""
        for row in self.state.undelivered():
            self.queue.append(Notice(row["id"], row["session"], row["text"], row["why"], row["signal_keys"]))

    def enqueue(self, notice: Notice) -> None:
        self.queue.append(notice)
        self.release()

    def hold(self, session: str) -> bool:
        if self.is_running(session):
            return True
        presence = self.state.presence()
        if presence is None:
            return False
        fresh = time.time() - presence["ts"] < 60
        return fresh and presence["idle_s"] < self.rules.idle_threshold_s

    def release(self) -> None:
        """Deliver every releasable notice, in order; one still held for its own session (or
        whose banner just failed) stays queued in place - it does not block another session's
        notice behind it, and it is retried on the next release()."""
        self.queue = [n for n in self.queue if self.hold(n.session) or not self._deliver(n)]

    def _deliver(self, notice: Notice) -> bool:
        """True on success. A failed banner is not recorded as a delivery - the notice stays in
        the outbox, to go out for real on the next release()."""
        if self.attached(notice.session):
            self.send(notice.session, {"type": "notice", "id": notice.id, "text": notice.text, "why": notice.why})
            via = "socket"
        else:
            if not self.banner(notice.text):
                return False
            via = "banner"
        self.record("delivery", session=notice.session, origin="signal", id=notice.id, via=via)
        self.record("assistant", session=notice.session, origin="signal", text=notice.text, final=True)
        return True
