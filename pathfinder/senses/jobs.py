"""jobs sense - documents the two kinds `jobs.Manager` pushes directly (there is no `build`
here: a job's outcome is never something a client can hand the daemon over the socket, so the
push-sense check's `hasattr(module, "build")` is false and `signal jobs ...` is refused, same as
any other unknown or non-push sense)."""
from __future__ import annotations

OPTIONS: dict = {}
TRUST = "user"        # the outcome of what the user started - D8 sec 8: no budget, no quiet hours
CONDITIONS: dict = {}  # no condition words
KINDS = {
    "job_done": ("always", "-", "silence", "no rule configured for this kind"),
    "job_failed": ("always", "-", "silence", "no rule configured for this kind"),
}
