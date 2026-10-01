"""What PATHFINDER's hands can do. `ToolSpec` is a registry entry; `coerce`
casts a model's arguments to the declared JSON-schema types before the broker
ever checks policy or calls a handler - an uncast `escalate="false"` (a string,
which Python treats as truthy) must not sail past the gate as an escalation."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import clock
from . import exec as _exec

READ_CLIP = 8000
LEDGER_CLIP = 2000  # what a recorded tool_call/tool_result (a job's own copies too) keeps


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict          # arg name -> {"type": "string"|"integer"|"number"|"boolean", "description": str}
    required: list
    handler: Callable[..., Any]
    write: bool = False       # has an external side effect -> needs the user's ok
    network: bool = False     # can send data out
    sandboxed: bool = False   # side effects are confined by exec.py
    reversible: bool = False  # a write the user can undo by deleting a row (D22) -> user: allow
    # who may even see this tool's schema (default: the four turn origins; `memory` - the
    # consolidation pass - is opt-in)
    origins: frozenset = frozenset({"user", "signal", "job", "system"})
    # Runs after coerce and before policy: what the gate shows is what will happen. A relative
    # path becomes absolute here; a local_datetime param is canonicalized by normalize_local_datetime.
    # May raise ValueError with a message the model can act on - the broker turns it into a tool error.
    normalize: Callable[[dict], dict] | None = None

    def schema(self) -> dict:
        """The OpenAI-style function-tool dict the adapter sends the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.params, "required": self.required},
            },
        }


def for_origin(origin: str) -> dict:
    """The registry a turn of this origin may call - `start_job` declares `origins={"user"}`, so
    a job's own registry (origin "job") and a signal turn's (origin "signal") never contain it:
    "a worker cannot make a worker" and "a mail cannot start a job" are structure, not policy."""
    # list(): an MCP reload mutates TOOLS on the loop thread while a job or a pass iterates here
    return {name: spec for name, spec in list(TOOLS.items()) if origin in spec.origins}


_BOOL = {"true": True, "false": False, "1": True, "0": False}


def _cast(kind: str, value: Any) -> tuple:
    """(cast value, error reason). A model sends untyped JSON, so booleans and
    integers often arrive as strings - the string "false" is truthy in Python,
    so leaving it uncast can flip a cancellation into a confirmation."""
    if kind == "integer":
        if isinstance(value, bool):
            return None, "must be an integer"
        if isinstance(value, int):
            return value, ""
        if isinstance(value, float) and value.is_integer():
            return int(value), ""
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip()), ""
        return None, "must be an integer"
    if kind == "number":
        if isinstance(value, bool):
            return None, "must be a number"
        if isinstance(value, (int, float)):
            return float(value), ""
        if isinstance(value, str):
            try:
                return float(value.strip()), ""
            except ValueError:
                return None, "must be a number"
        return None, "must be a number"
    if kind == "boolean":
        if isinstance(value, bool):
            return value, ""
        if isinstance(value, str) and value.strip().lower() in _BOOL:
            return _BOOL[value.strip().lower()], ""
        if isinstance(value, int) and value in (0, 1):
            return bool(value), ""
        return None, "must be true or false"
    if kind == "string":
        return (value, "") if isinstance(value, str) else (None, "must be a string")
    return value, ""  # an undeclared type is passed through unchanged


def coerce(spec: ToolSpec, args: dict) -> tuple:
    """(cast args, error string). An undeclared key is dropped, not an error -
    the model gets the same result either way (the key is unusable), but a
    dropped key is silent while a missing required one is not."""
    out = {}
    for key, value in (args or {}).items():
        if key not in spec.params:
            continue
        if value is None:
            continue
        kind = spec.params[key].get("type", "string")
        cast, why = _cast(kind, value)
        if why:
            return {}, f"{spec.name}: '{key}' {why} (got {value!r})"
        out[key] = cast
    for key in spec.required:
        if key not in out:
            return {}, f"{spec.name}: missing required argument '{key}'"
    return out, ""


def clip(text: str, limit: int = READ_CLIP) -> str:
    """The one clip helper - `_read_file` uses it directly; `broker.call` reuses it to bound
    every tool result the model ever sees, regardless of which tool produced it."""
    if len(text) <= limit:
        return text
    head = tail = limit // 2
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...({dropped} characters omitted)...\n{text[-tail:]}"


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def clean(text) -> str:
    """The one character rule for someone else's text (D62): whole ANSI sequences out, every non-printable
    character (controls, bidi marks, zero-width) dropped, a newline and a tab kept (a CRLF pair becomes a
    newline), any other whitespace one space. Nothing is collapsed, so code and a page keep their shape."""
    text = _ANSI_RE.sub("", str(text)).replace("\r\n", "\n")
    return "".join(ch if ch in "\n\t" or not ch.isspace() else " "
                   for ch in text if ch.isspace() or ch.isprintable())


def oneline(text, n=None) -> str:
    """Someone else's text (a registry name, a mail subject, a calendar title) on one line: `clean`,
    whitespace to one space, at most `n` characters. Everything such text hands the model or the screen
    outside a tool result goes through this."""
    return " ".join(clean(text).split())[:n]


def visible(text) -> str:
    """An approval line shows exactly what runs: every character `clean` would drop or change is
    written as an escape (`\\n`, `\\t`, `\\u200b`, `\\U000e0041`) instead, so a hidden character is
    seen, not removed; printable text, Korean too, stays as it is, and the line stays one line."""
    return "".join(ch if ch.isprintable() else {"\n": "\\n", "\t": "\\t"}.get(ch) or
                   (f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}") for ch in str(text))


def map_strings(value, fn):
    """Every string in `value`, dict keys too, however deep, through `fn`; a tuple comes back a list."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {map_strings(k, fn): map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [map_strings(v, fn) for v in value]
    return value


def clip_for_ledger(value, limit: int = LEDGER_CLIP):
    """Shorter and head-only, unlike `clip()` above - what the daemon and a job keep of a tool
    call's args/result in the ledger, not what the model itself is shown."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...(truncated)"
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= limit else text[:limit] + "...(truncated)"


def normalize_local_datetime(*keys):
    """Build a `normalize` for a spec whose params declare `"format": "local_datetime"` on
    `keys`: parses each with clock.parse_local and rewrites it canonical, so the gate and
    RULES.md show exactly when it lands. `parse_local`'s ValueError (a `Z` or an offset) is
    left to propagate - the broker turns it into a tool error the model can act on."""
    def normalize(args):
        out = dict(args)
        for key in keys:
            if key in out:
                _, out[key] = clock.parse_local(out[key])
        return out
    return normalize


def _absolute_path(args):
    """`~` expands; a relative path lands in the workspace, like the shell's cwd - never in
    the daemon's own cwd; a symlink is followed. The gate shows the result, so the user sees
    where it really goes, not a link's name."""
    p = Path(args["path"]).expanduser()
    if not p.is_absolute():
        p = _exec.workspace() / p
    return {**args, "path": str(p.resolve())}


def _read_file(path, control=None):
    p = Path(path)
    if _exec.is_secret(p):
        raise ValueError(f"refused: {path} is a secret path")
    return clip(p.read_text(encoding="utf-8"))


def _write_file(path, content, control=None):
    p = Path(path)
    if _exec.is_secret(p):  # a shell rc or a key file is code execution on the next login
        raise ValueError(f"refused: {path} is a secret path")
    from . import skills  # here: skills.py imports this module
    folder, parent = skills._home and skills._home / "skills", p.resolve().parent  # through any symlink
    if folder and folder.is_dir() and parent.is_dir() and os.path.samefile(parent, folder):  # by inode: case too
        raise ValueError(f"refused: {path} - a skill is written only by the owner, the seed copy or an approved edit_memory")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p), "bytes": len(content.encode("utf-8"))}


def _run_command(command, escalate=False, control=None):
    return _exec.run_command(command, escalate, control=control)


TOOLS = {
    "run_command": ToolSpec(
        name="run_command",
        description="Run a shell command in a sandboxed workspace; the network is open (curl works).",
        params={
            "command": {"type": "string", "description": "the shell command to run"},
            "escalate": {"type": "boolean", "description": "run outside the sandbox (needs the user's approval)"},
        },
        required=["command"],
        handler=_run_command,
        write=True, sandboxed=True,
    ),
    "read_file": ToolSpec(
        name="read_file",
        description="Read a UTF-8 text file.",
        params={"path": {"type": "string", "description": "absolute path, or ~/... for the home directory; a relative path lands in the workspace"}},
        required=["path"],
        handler=_read_file, normalize=_absolute_path,
    ),
    "write_file": ToolSpec(
        name="write_file",
        description="Write a UTF-8 text file, creating parent directories as needed.",
        params={
            "path": {"type": "string", "description": "absolute path, or ~/... for the home directory; a relative path lands in the workspace"},
            "content": {"type": "string", "description": "the file content"},
        },
        required=["path", "content"],
        handler=_write_file, normalize=_absolute_path,
        write=True,
    ),
}
