"""memory sense - documents the two kinds `memory.Manager` pushes directly (there is no `build`
here: a proposal or a stalled pass is never something a client can hand the daemon over the
socket, so the push-sense check's `hasattr(module, "build")` is false and `signal memory ...` is
refused, same as `jobs`)."""
from __future__ import annotations

OPTIONS: dict = {}
TRUST = "self"         # the pass's own text, not the user's words: no envelope authority.
CONDITIONS: dict = {}  # no condition words
KINDS = {
    "memory_proposed": ("always", "-", "silence", "no rule configured for this kind"),
    "memory_stalled": ("always", "-", "silence", "no rule configured for this kind"),
}
