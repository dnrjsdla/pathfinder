"""Skills (D58, D61): procedures the owner approved, one directory, `<home>/skills/<name>.md`.

A skill is an instruction, so it is trusted where a wiki page (knowledge, data) is not, and its
only mark is its location. Nothing writes there but the owner's hand, the seed copy below and an
approved `edit_memory` from a user turn. The model sees each skill's name and description inside
`read_skill`'s own description; a body is read only when its skill is."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from . import keychain
from . import tools as tools_mod
from .rules import _parse_frontmatter
from .tools import ToolSpec

SEEDS = Path(__file__).parent / "skills"  # package data; nothing reads it but `bind`
NAME_RE = re.compile(r"[a-z0-9-]+")
DESCRIPTION_MAX = 160
INDEX_MAX = 4000
HEADER = ("[Skill '{name}' - a procedure the owner approved; follow it. "
          "Secrets go through /secret, never the chat.]")
_log = logging.getLogger("pathfinder.skills")  # mcp/attach.py turns its warnings into `error` events
_home: Path | None = None
_warned: list = []  # the problems at the last read: each is warned once, when it appears


def bind(home) -> None:
    """Once at daemon start: every shipped seed whose file is missing is written into the home,
    as `rules.load` writes RULES.md; an existing file, edited or not, is never touched."""
    global _home, _warned
    _home, _warned = Path(home), []
    folder = _home / "skills"
    folder.mkdir(mode=0o700, exist_ok=True)
    for seed in sorted(SEEDS.glob("*.md")):
        try:  # born private; O_EXCL: never through a symlink, a dangling one included
            fd = os.open(folder / seed.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(seed.read_text(encoding="utf-8"))


def _load(path: Path) -> tuple:
    """(description, body) of one skill file, or ValueError saying why it is not a skill."""
    if path.is_symlink():
        raise ValueError("it is a symlink; a skill is a file in the skills directory itself")
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    description = meta.get("description")
    if meta.get("name") != path.stem or not NAME_RE.fullmatch(path.stem):
        raise ValueError("its name must equal the file's stem (lowercase, digits, hyphens)")
    if not isinstance(description, str) or not 0 < len(description) <= DESCRIPTION_MAX:
        raise ValueError(f"its description must be one line of 1-{DESCRIPTION_MAX} characters")
    if keychain.TOKEN_RE.search(text):
        raise ValueError("it holds a token-shaped string; a skill names the intake (/secret), never a value")
    return description, body


def index(home) -> str:
    """Every skill as `- <name>: <description>`, sorted by name, never cut; read on every request,
    so an edit in place counts at once (50 skills cost about a millisecond). A file that is not a
    skill, and an index over INDEX_MAX characters, are refused with a warning to the owner."""
    global _warned
    lines, problems = [], []
    for path in sorted((Path(home) / "skills").glob("*.md"), key=lambda p: p.stem):
        try:
            lines.append(f"- {path.stem}: {_load(path)[0]}")
        except (OSError, ValueError) as e:  # a hand-written file must not break every turn
            problems.append(f"skill {path.name} is not loaded: {e}")
    text = "\n".join(lines)
    if len(text) > INDEX_MAX:
        problems.append(f"the skills index is {len(text)} characters, over {INDEX_MAX}: no skill is listed")
        text = f"(index too large: {len(lines)} skills not listed - shorten their descriptions)"
    for problem in problems:
        if problem not in _warned:
            _log.warning("%s", problem)
    _warned = problems
    return text


def _read_skill(name, control=None):
    if not NAME_RE.fullmatch(name) or not (path := _home / "skills" / f"{name}.md").is_file():
        raise ValueError(f"no skill named {name!r}")
    try:
        body = _load(path)[1]
    except ValueError as e:
        raise ValueError(f"skill {name!r} is not loaded: {e}") from None
    return HEADER.format(name=name) + "\n" + tools_mod.clip(body)


class _SkillSpec(ToolSpec):
    """`read_skill`'s description carries the index (D61), so it is built per request; it stays
    byte-stable until a skill changes, which suits caching."""

    def schema(self) -> dict:
        out = super().schema()
        out["function"]["description"] = f"{self.description}\n{(index(_home) if _home else '') or '(none yet)'}"
        return out


TOOLS = {
    "read_skill": _SkillSpec(
        name="read_skill",
        description="Read a skill: a procedure the owner approved for one task. Before doing a task "
                    "a skill below covers, read it and follow it. Skills:",
        params={"name": {"type": "string", "description": "the skill's name, as listed"}},
        required=["name"], handler=_read_skill, origins=frozenset({"user", "job"}),
    ),
}
tools_mod.TOOLS.update(TOOLS)
