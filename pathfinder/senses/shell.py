"""shell sense - a zsh `preexec`/`precmd` hook pushes `pathfinder signal shell {cmd, exit,
seconds, cwd}` after every command (docs/senses.md has the snippet). Push only: no `poll`, no
CADENCE_S. Each push is a real occurrence, not a polling artifact - unlike mail or calendar,
seeing the same command fail three times in a row is exactly what `count_over` is for, so the
dedupe key includes the moment it arrived rather than collapsing repeats into "seen"."""
from __future__ import annotations

import time

from ..signals import Signal
from ..tools import oneline

OPTIONS: dict = {}
TRUST = "self"
KINDS = {
    "command_failed": ("always", "-", "silence", "no rule configured for this kind"),
    "command_finished": ("always", "-", "silence", "no rule configured for this kind"),
}


def _exit_nonzero(signal, value, today, now):
    return signal.fields.get("exit", 0) != 0


def _duration_over(signal, value, today, now):
    return (signal.fields.get("seconds") or 0) > value


CONDITIONS = {"exit_nonzero": _exit_nonzero, "duration_over": _duration_over}


def _clip(cmd: str, n: int = 60) -> str:
    return oneline(cmd if len(cmd) <= n else cmd[:n] + "...")  # len(): a non-string cmd stays an error


def build(fields: dict, now: float | None = None) -> Signal:
    """Turn one pushed `{cmd, exit, seconds, cwd}` into a Signal - the daemon's `signal`
    protocol handler calls this for a push sense; a poll sense never needs it."""
    now = now if now is not None else time.time()
    cmd, exit_code = fields.get("cmd", ""), fields.get("exit", 0)
    if exit_code != 0:
        kind, summary = "command_failed", f"Command failed (exit {exit_code}): {_clip(cmd)}"
    else:
        kind, summary = "command_finished", f"Command finished: {_clip(cmd)}"
    key = f"shell:{cmd}:{fields.get('cwd', '')}:{now}"
    return Signal(sense="shell", kind=kind, key=key, summary=summary, fields=fields, trust=TRUST)
