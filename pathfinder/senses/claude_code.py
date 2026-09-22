"""claude_code sense - pushed by Claude Code's `Stop` and `Notification` hooks (docs/senses.md
has the settings.json block): `pathfinder signal claude_code {event, cwd, message}`. Speaks only
when the user is away from PATHFINDER itself - the `away` condition, computed by the daemon from
`presence` and handed in on `today` before `rules.decide` ever runs (`state.Today.away`; see
`state.today`'s `session`/`idle_threshold_s` arguments and `daemon.run_signal_turn`) - a
predicate only gets `(signal, value, today, now)`, never the rules object or the state itself."""
from __future__ import annotations

import time

from ..signals import Signal

OPTIONS: dict = {}
TRUST = "self"
KINDS = {
    "task_done": ("always", "-", "silence", "no rule configured for this kind"),
    "needs_input": ("always", "-", "silence", "no rule configured for this kind"),
}
CONDITIONS = {"away": lambda signal, value, today, now: today.away}


def build(fields: dict, now: float | None = None) -> Signal:
    now = now if now is not None else time.time()
    event, cwd = fields.get("event"), fields.get("cwd", "")
    kind = "task_done" if event == "Stop" else "needs_input"
    default = "Claude Code finished a task" if kind == "task_done" else "Claude Code needs input"
    summary = fields.get("message") or default
    return Signal(sense="claude_code", kind=kind, key=f"claude_code:{cwd}:{event}:{now}",
                 summary=summary, fields=fields, trust=TRUST)
