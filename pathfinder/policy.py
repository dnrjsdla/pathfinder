"""Who may call what. `verdict()` reads only the tool's declared facts and effect grade, the owner's
config and the caller's origin - never a model name, never shell syntax. The only argument it looks
at is `escalate` (whether a sandboxed call asked to run outside its box) and the one an `effect_by`
map names."""
from . import config
from . import exec as _exec

DENIED_TEXT = (
    "denied: this is a policy decision, not a failure. Retrying or trying a "
    "different tool to work around it will not change the outcome."
)

# Grade first, then the origin's row, then grants, then the floors.

def _row(rest: str, **cells) -> dict:
    """One row keyed by every grade in config.GRADES plus "unknown": `cells`, and `rest` for the others."""
    return {grade: cells.get(grade, rest) for grade in (*config.GRADES, "unknown")}


_ASK_ROW = _row("ask", read="allow")

# The user's row by `approval_mode` (V17). An unknown effect asks in every mode. No full-auto until P3.
MODES = {
    "read-only": _row("deny", read="allow", unknown="ask"),
    "manual": _ASK_ROW,
    "auto": _row("ask", read="allow", write="allow"),
}
_ROWS = {
    "signal": _ASK_ROW,
    "system": _row("deny", read="allow"),
}
ORIGINS = ("user", *_ROWS)  # the origins `verdict` has a row for; the broker refuses any other


def _mcp_name(name: str) -> tuple:
    """(server, tool) of an `mcp__<server>__<tool>` name, ("", "") for a native (server ids hold no `_`)."""
    parts = name.split("__", 2)
    return (parts[1], parts[2]) if len(parts) == 3 and parts[0] == "mcp" else ("", "")


def effect(spec, args, tools_cfg) -> tuple[str, dict | None]:
    """(grade or "unknown", `{arg, value}` when an `effect_by` lookup missed), in V6's order. A native: a
    sandboxed call that leaves its box (escalate, or no sandbox on this machine) is destructive, else its
    explicit `effect`, else write or read by its `write` fact. An MCP tool: the owner's map
    `tools.<server>.<tool>` - `effect`, else `effect_by` keyed by one argument's value (an absent argument
    or an unmapped value is unknown, never a fall-through) - else the grade its annotations gave at load."""
    args = args or {}
    server, tool = _mcp_name(spec.name)
    if not server:
        if spec.sandboxed and (args.get("escalate") or not _exec.available()):
            return "destructive", None
        return spec.effect or ("write" if spec.write else "read"), None
    entry = (tools_cfg or {}).get(server, {}).get(tool, {})
    if "effect" in entry:
        return entry["effect"], None
    if "effect_by" in entry:
        arg, table = next(iter(entry["effect_by"].items()))
        if arg not in args:
            return "unknown", {"arg": arg}
        grade = table.get(args[arg]) if isinstance(args[arg], str) else None
        return (grade, None) if grade else ("unknown", {"arg": arg, "value": args[arg]})
    return spec.effect or "unknown", None


def verdict(spec, origin: str, args: dict | None, cfg: dict, session_grants: set) -> tuple[str, str, dict | None]:
    """(decision, grade, unknown) for one call. The row: the user's `approval_mode`, or the origin's. Then
    the attributes: in `manual` a sandboxed or reversible write runs; a networked read asks for a signal and
    is denied to the system. Then, for the user only, grants from three scopes at once - the tool's name,
    `mcp__<server>__*`, and the session's "always allow" set: a deny anywhere wins; else an allow lifts a
    write (only a write, and not read-only's deny) to allow; else an ask tightens an allow. Nothing lifts
    destructive, external_send or unknown (§8). A grant value outside allow/ask/deny is ignored."""
    grade, unknown = effect(spec, args, cfg.get("tools"))
    if origin != "user":
        decision = _ROWS[origin][grade]
        if grade == "read" and spec.network:
            decision = "ask" if origin == "signal" else "deny"
        return decision, grade, unknown
    mode = cfg.get("approval_mode", "manual")
    decision = MODES[mode][grade]
    if mode == "manual" and grade == "write" and (spec.sandboxed or spec.reversible):
        decision = "allow"
    grants = cfg.get("grants") or {}
    server, _ = _mcp_name(spec.name)
    scopes = (grants.get(spec.name), grants.get(f"mcp__{server}__*") if server else None,
              "allow" if spec.name in session_grants else None)
    if "deny" in scopes:
        decision = "deny"
    elif "allow" in scopes and grade == "write" and decision != "deny":
        decision = "allow"
    elif "ask" in scopes and decision == "allow":
        decision = "ask"
    return decision, grade, unknown
