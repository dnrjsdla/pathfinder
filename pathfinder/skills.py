"""Skills (D58, D61): procedures the owner approved, one directory, `<home>/skills/<name>.md`.

A skill is an instruction, so it is trusted where a wiki page (knowledge, data) is not, and its
only mark is its location. Nothing writes there but the owner's hand, the seed copy below and an
approved `edit_memory` from a user turn. The model sees each skill's name and description inside
`read_skill`'s own description; a body is read only when its skill is."""
from __future__ import annotations

import logging
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
_log = logging.getLogger("pathfinder.skills")  # the daemon's stderr is <home>/daemon.log
_home: Path | None = None
_cache: tuple = (None, "")  # ((directory, its mtime), the index it gave)


def bind(home) -> None:
    """Once at daemon start: every shipped seed whose file is missing is written into the home,
    as `rules.load` writes RULES.md; an existing file, edited or not, is never touched."""
    global _home
    _home = Path(home)
    folder = _home / "skills"
    folder.mkdir(mode=0o700, exist_ok=True)
    for seed in sorted(SEEDS.glob("*.md")):
        path = folder / seed.name
        if not path.exists():
            path.touch(mode=0o600)  # born private, as everything under the home
            path.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")


def _load(path: Path) -> tuple:
    """(description, body) of one skill file, or ValueError saying why it is not a skill."""
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
    """Every skill as `- <name>: <description>`, sorted by name, never cut; redone when the
    directory's mtime moves. A file that is not a skill, and an index over INDEX_MAX characters,
    are refused here with a warning in the daemon's log."""
    global _cache
    folder = Path(home) / "skills"
    try:
        key = (folder, folder.stat().st_mtime_ns)
    except FileNotFoundError:
        return ""
    if _cache[0] == key:
        return _cache[1]
    lines = []
    for path in sorted(folder.glob("*.md"), key=lambda p: p.stem):
        try:
            lines.append(f"- {path.stem}: {_load(path)[0]}")
        except (OSError, ValueError) as e:  # a hand-written file must not break every turn
            _log.warning("skill %s is not loaded: %s", path.name, e)
    text = "\n".join(lines)
    if len(text) > INDEX_MAX:
        _log.warning("the skills index is %d characters, over %d: no skill is loaded until "
                     "descriptions are shortened or skills removed", len(text), INDEX_MAX)
        text = ""
    _cache = (key, text)
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
        out["function"]["description"] = f"{self.description}\n{index(_home) or '(none yet)'}"
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
