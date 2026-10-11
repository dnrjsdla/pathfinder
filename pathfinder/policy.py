"""Who may call what. `decide()` reads only the tool's declared facts (write,
network, sandboxed) and the caller's origin - never a model name, never shell
syntax. The only argument it looks at is `escalate` (whether a sandboxed call
asked to run outside its box)."""
from . import exec as _exec

DENIED_TEXT = (
    "denied: this is a policy decision, not a failure. Retrying or trying a "
    "different tool to work around it will not change the outcome."
)

# origin -> category -> decision. "read" is any tool that is none of write,
# network, sandboxed or reversible.
_TABLE = {
    "user":   {"read": "allow", "sandboxed": "allow", "network": "allow", "write": "ask",  "reversible": "allow"},
    "signal": {"read": "allow", "sandboxed": "ask",    "network": "ask",   "write": "ask",  "reversible": "ask"},
    "job":    {"read": "allow", "sandboxed": "ask",    "network": "ask",   "write": "ask",  "reversible": "ask"},
    "system": {"read": "allow", "sandboxed": "deny",   "network": "deny",  "write": "deny", "reversible": "deny"},
    # the consolidation pass reads the vault and the ledger and makes reversible line edits,
    # nothing else; grants never apply to it (they are the user's, for user turns - the
    # existing origin == "user" check above already ensures this)
    "memory":  {"read": "allow", "sandboxed": "deny",   "network": "deny",  "write": "deny", "reversible": "allow"},
}


def _category(spec, args) -> str:
    # A sandboxed tool's side effects are contained, so it is judged on that -
    # not on its `write` fact - UNLESS it asks to leave the sandbox (escalate)
    # or there is no sandbox on this machine to run it in at all. It must
    # never run bare just because the sandbox is missing.
    if spec.sandboxed:
        escalate = bool((args or {}).get("escalate"))
        return "write" if (escalate or not _exec.available()) else "sandboxed"
    if spec.write:
        # D22: a write the user can undo by deleting a row (remove_schedule) runs by itself for
        # the user; add_schedule asks, because a due schedule runs as a user turn (WP2b amends
        # D22); a calendar write may sync and invite, so it stays "write".
        return "reversible" if spec.reversible else "write"
    if spec.network:
        return "network"
    return "read"


DECISIONS = ("allow", "ask", "deny")


def decide(spec, origin: str, args: dict | None = None, grants: dict | None = None) -> str:
    category = _category(spec, args)
    left_the_box = spec.sandboxed and category == "write"
    if origin == "user" and not left_the_box:
        # The owner's word wins over the table, for their own turns only. "Do not ask me about
        # the shell" covers the sandboxed shell, never escalate=true or running bare on a
        # machine with no sandbox. A value outside DECISIONS falls back to the table (config
        # refuses it too): a typo must not switch the gate off.
        granted = (grants or {}).get(spec.name)
        if granted in DECISIONS:
            return granted
    return _TABLE[origin][category]
