"""attach sense - fed by the daemon's own `attach {cwd}` protocol message, not a hook. Always
silence: it exists so rules (and, later, the catalog) know what the user is working on, not to
speak on its own (blueprint §8's "context" source)."""
from __future__ import annotations

import os

from ..signals import Signal

OPTIONS: dict = {}
TRUST = "self"
CONDITIONS: dict = {}
KINDS = {"context": ("always", "-", "silence", "no rule configured for this kind")}


def build(fields: dict, now: float | None = None) -> Signal:
    cwd = fields.get("cwd", "")
    project = os.path.basename(cwd.rstrip("/")) or cwd
    return Signal(sense="attach", kind="context", key=f"attach:{cwd}",
                 summary=f"Working in {project}", fields={"cwd": cwd, "project": project}, trust=TRUST)
