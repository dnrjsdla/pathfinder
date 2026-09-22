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


def banner(text: str) -> None:
    """The default `banner`: a macOS notification. Injectable - tests pass their own."""
    script = f'display notification {json.dumps(text)} with title "PATHFINDER"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


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
        presence = self.state.presence(session)
        if presence is None:
            return False
        fresh = time.time() - presence["ts"] < 60
        return fresh and presence["idle_s"] < self.rules.idle_threshold_s

    def release(self, session: str | None = None) -> None:
        """Send every releasable notice in FIFO order, stopping at the first one still held -
        a later notice never jumps a held earlier one."""
        while self.queue and not self.hold(self.queue[0].session):
            self._deliver(self.queue.pop(0))

    def _deliver(self, notice: Notice) -> None:
        if self.attached(notice.session):
            self.send(notice.session, {"type": "notice", "id": notice.id, "text": notice.text, "why": notice.why})
            via = "socket"
        else:
            self.banner(notice.text)
            via = "banner"
        self.record("delivery", session=notice.session, origin="signal", id=notice.id, via=via)
        self.record("assistant", session=notice.session, origin="signal", text=notice.text, final=True)
