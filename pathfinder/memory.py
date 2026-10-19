"""The memory vault: `<home>/USER.md`, `<home>/RULES.md` and `<home>/wiki/*.md`. Files are the
truth; `state`'s page index, summaries and counters are a derived index over them.

Two things live here: the model's own memory tools (registered into `tools.TOOLS` at import), and
the code-only gate (`Manager.vet`) that decides whether a proposed edit may land before any of it
is written. The consolidation pass returns in partition 4. `Manager` is bound once (`memory.bind`)."""
from __future__ import annotations

import difflib
import os
import tempfile
import threading
import uuid
from pathlib import Path

from . import clock, config
from . import rules as rules_mod
from . import skills as skills_mod
from . import tools as tools_mod
from .llm import LLMError, openai_compat, resolve
from .signals import sense_modules
from .state import State
from .tools import ToolSpec


# -- the vault -------------------------------------------------------------------------------

def vault_path(home, target: str) -> Path:
    """`target` is exactly `USER.md`, `RULES.md`, `wiki/<name>.md` or `skills/<name>.md` (`<name>`
    has no `/`, no `..`, is not empty and ends with `.md`) - anything else is outside the vault."""
    home = Path(home)
    if target in ("USER.md", "RULES.md"):
        return home / target
    for folder in ("wiki", "skills"):
        name = target[len(folder) + 1:] if target.startswith(folder + "/") else ""
        if name and "/" not in name and ".." not in name and name.endswith(".md"):
            return home / folder / name
    raise ValueError(f"outside the vault: {target!r}")

def _cut_at_line(text: str, limit: int) -> str:
    cut = text.rfind("\n", 0, limit)
    return text if len(text) <= limit else text[:cut if cut != -1 else limit]

def context(home, state) -> str:
    """USER.md's body plus the wiki map (newest first, cut at 1,000 chars at a line boundary)."""
    user_path = Path(home) / "USER.md"
    body = rules_mod._parse_frontmatter(user_path.read_text(encoding="utf-8"))[1].strip() if user_path.exists() else ""
    pages = state.pages()
    if not body and not pages:
        return ""
    wiki_map = _cut_at_line(
        "Wiki pages:\n" + "\n".join(f"- {p['title']} ({clock.format_local(p['mtime'])[:10]})" for p in pages),
        1000)
    return f"{body}\n\n{wiki_map}" if body else wiki_map

def _reindex(home, cfg, state) -> None:
    """Every `wiki/*.md` on disk (one batched embed call), `remove_page` for the rest."""
    home = Path(home)
    wiki_dir = home / "wiki"
    disk = {f"wiki/{p.name}": p for p in sorted(wiki_dir.glob("*.md"))} if wiki_dir.exists() else {}
    targets = list(disk)
    texts = [disk[t].read_text(encoding="utf-8") for t in targets]
    model = cfg["embed_model"]
    vectors = [None] * len(targets)
    if model and texts:
        try:
            vectors = openai_compat.embed(resolve(cfg, model), texts)
        except LLMError:
            model = ""  # embedding failed for the whole batch: index FTS-only, drop stale vectors
    for target, text, vector in zip(targets, texts, vectors):
        p = disk[target]
        first = text.split("\n", 1)[0] if text else ""
        title = first[2:].strip() if first.startswith("# ") else p.stem
        state.index_page(target, title, text, p.stat().st_mtime, vector, model if vector is not None else "")
    for page in state.pages():
        if page["path"] not in disk:
            state.remove_page(page["path"])

def reindex_pages(home, cfg) -> None:
    """The CLI's `pathfinder reindex` entry: no daemon, builds its own State."""
    state = State(Path(home) / "state.db")
    try:
        _reindex(home, cfg, state)
    finally:
        state.close()


# -- the gate (no model) ----------------------------------------------------------------------

def _find_block(lines: list, block: list) -> list:
    """Every index where `block` occurs as consecutive lines of `lines` - exact match first. A
    one-line block with no exact match falls back to the single line similar enough (ratio >=
    0.8) that a model retyping a line it just read still finds it (live: a free-tier model
    retyped a Korean line after reading it verbatim)."""
    n = len(block)
    if n == 0 or n > len(lines):
        return []
    exact = [i for i in range(len(lines) - n + 1) if lines[i:i + n] == block]
    if exact or n != 1:
        return exact
    fuzzy = [i for i, line in enumerate(lines)
             if difflib.SequenceMatcher(None, line, block[0]).ratio() >= 0.8]
    return fuzzy if len(fuzzy) == 1 else []

def _apply_edit(text: str, op: str, old: str, new: str, idx: int | None) -> str:
    if op == "add":
        return new if not text else text.rstrip("\n") + "\n" + new
    lines = text.split("\n") if text else []
    old_lines = old.split("\n")
    head, tail = lines[:idx], lines[idx + len(old_lines):]
    if op == "replace":
        return "\n".join(head + new.split("\n") + tail)
    return "\n".join(head + tail)  # remove


# -- the tools ------------------------------------------------------------

_manager: "Manager | None" = None

def bind(manager: "Manager") -> None:
    global _manager
    _manager = manager

def _search_memory(query, control=None):
    return _manager.search(query)

def _read_memory(path, control=None):
    if isinstance(path, str) and path.startswith("skills/"):  # read_skill runs the token check; this would hand over the raw file
        raise ValueError("a skill is read with read_skill")
    p = vault_path(_manager.home, path)  # ValueError propagates - the broker turns it into a tool error
    if not p.exists():
        pages = _manager.state.pages()
        key = path.replace("_", " ").lower()  # live: a title with spaces asked for, a file with underscores on disk
        same = next((vault_path(_manager.home, pg["path"]) for pg in pages
                     if pg["path"].startswith("wiki/") and pg["path"].replace("_", " ").lower() == key), None)
        if same is not None and same.exists():
            return tools_mod.clip(same.read_text(encoding="utf-8"))
        listing = ", ".join(pg["path"] for pg in pages) if pages else "(no pages yet)"
        return f"no such page: {path}. Pages: {listing}"
    return tools_mod.clip(p.read_text(encoding="utf-8"))

def _edit_memory(target, op, old="", new="", evidence=None, control=None):
    return _manager.edit(target, op, old, new, evidence or [], control)

TOOLS = {
    "search_memory": ToolSpec(
        name="search_memory",
        description="Search the memory vault (USER.md, RULES.md, wiki pages) for pages matching a query.",
        params={"query": {"type": "string", "description": "search text"}},
        required=["query"], handler=_search_memory,
    ),
    "read_memory": ToolSpec(
        name="read_memory",
        description="Read one file of the memory vault: wiki/<name>.md (exactly as search_memory "
                    "or the page list names it), USER.md or RULES.md.",
        params={"path": {"type": "string", "description": "wiki/<name>.md, USER.md or RULES.md"}},
        required=["path"], handler=_read_memory,
    ),
    "edit_memory": ToolSpec(
        name="edit_memory",
        description=(
            "Edit the memory vault one line at a time. Call search_memory first and edit the page it "
            "finds; create a new page only when none matches. target: wiki/<Title>.md (knowledge, a "
            "how-to, a number worth keeping), USER.md (who the user is) or RULES.md. op: add (append a "
            "line; creates a wiki page whose text starts with '# Title'), replace (old -> new, old must "
            "be exactly one existing line), remove. USER.md and RULES.md edits are queued for the "
            "user's approval - say so, do not say they are written."),
        params={
            "target": {"type": "string", "description": "wiki/<Title>.md, USER.md, RULES.md or skills/<name>.md "
                       "(a procedure the user asked to keep; always needs their approval); its text starts with a "
                       "frontmatter block: ---\nname: <name>\ndescription: <one line, when to use it>\n---\nthen the steps"},
            "op": {"type": "string", "description": "add, replace or remove"},
            "old": {"type": "string", "description": "the one existing line to replace or remove"},
            "new": {"type": "string", "description": "the line(s) to add or to replace old with"},
            "evidence": {"type": "array", "items": {"type": "integer"},
                        "description": "the #seq numbers you read that support this edit"},
        },
        required=["target", "op"], handler=_edit_memory,
        origins=frozenset({"user"}), write=True, reversible=True,
    ),
}
tools_mod.TOOLS.update(TOOLS)


class Manager:
    def __init__(self, home, record, senses):
        self.home = Path(home)
        self._record = record
        self.senses = senses            # list of signals.Sense - RULES.md's condition/kind vocabulary
        self.dispatch = lambda fn: fn()  # swapped for call_soon_threadsafe by the daemon
        self.state = State(self.home / "state.db")   # this Manager's own connection
        self.on_rules_changed = lambda: None           # the daemon sets this after an approved RULES.md edit
        self._edit_lock = threading.RLock()  # apply()/index() - two turns' tool threads, or one and an approval
        # a deleted state.db loses the page index; a wiki with pages on disk and no index means
        # it was never rebuilt on this start - the CLI's `memory undo` message promises this.
        wiki_dir = self.home / "wiki"
        if not self.state.pages() and wiki_dir.exists() and any(wiki_dir.glob("*.md")):
            self.reindex_pages()

    def close(self) -> None:
        self.state.close()

    def record(self, type, **fields):
        self.dispatch(lambda: self._record(type, **fields))

    # -- search, index, resident context --------------------------------------------------
    def _embed(self, cfg, texts):
        if not cfg["embed_model"]:
            return None
        try:
            return openai_compat.embed(resolve(cfg, cfg["embed_model"]), texts)
        except LLMError:
            return None  # FTS-only: a stale vector is a wrong value, never kept

    def search(self, query):
        cfg = config.load(self.home)
        vector = None
        if cfg["embed_model"]:
            vectors = self._embed(cfg, [query])
            vector = vectors[0] if vectors else None
        hits = self.state.search(query, vector, model=cfg["embed_model"])
        if not hits:
            return "(no pages match)"
        return "\n".join(f"{h['path']} — {h['title']}: {h['snippet']}" for h in hits)

    def index(self, target: str) -> None:
        with self._edit_lock:
            path = vault_path(self.home, target)
            text = path.read_text(encoding="utf-8")
            if not text.strip():  # an emptied page (an undone add) is gone, not a blank title on the map
                path.unlink()
                return self.state.remove_page(target)
            first = text.split("\n", 1)[0]
            title = first[2:].strip() if first.startswith("# ") else Path(target).stem
            cfg = config.load(self.home)
            vectors = self._embed(cfg, [text]) if cfg["embed_model"] else None
            vector = vectors[0] if vectors else None
            self.state.index_page(target, title, text, path.stat().st_mtime, vector,
                                  cfg["embed_model"] if vector else "")

    def reindex_pages(self) -> None:
        _reindex(self.home, config.load(self.home), self.state)

    def context(self) -> str:
        return context(self.home, self.state)

    # -- the gate --------------------------------------------------------------------------
    def _rule_vocab(self):
        """CORE_CONDITIONS plus every sense module's own CONDITIONS/KINDS, scanned from
        `pathfinder/senses/` directly - not `self.senses` (only the senses this home has turned
        on): a RULES.md row for a sense not yet enabled must still parse."""
        conditions = dict(rules_mod.CORE_CONDITIONS)
        kinds = set()
        for _name, mod in sense_modules():  # a broken sense must not kill every memory pass
            conditions.update(getattr(mod, "CONDITIONS", {}) or {})
            kinds.update((getattr(mod, "KINDS", {}) or {}).keys())
        return conditions, kinds

    def _check_file(self, target, before, after, new):
        if target == "RULES.md":
            if rules_mod._parse_frontmatter(before)[0] != rules_mod._parse_frontmatter(after)[0]:
                return "RULES.md frontmatter is not editable here"
            conditions, _kinds = self._rule_vocab()
            try:
                rules_mod.parse(after, conditions, validated_kinds=None)  # every section, full vocabulary
            except ValueError as e:
                return str(e)
            for line in new.split("\n"):
                stripped = line.strip()
                if not stripped.startswith("|"):
                    continue
                cells = [c.strip().strip("`") for c in stripped.strip("|").split("|")]
                if len(cells) >= 3 and cells[2] not in rules_mod.ACTIONS:
                    return f"unknown action: {cells[2]!r}"
        elif target.startswith("skills/"):
            if not after.strip():
                return "a skill cannot be emptied here; the owner deletes the file"
            try:
                skills_mod.check(target[len("skills/"):-len(".md")], after)
            except ValueError as e:
                return str(e)
        elif target == "USER.md":
            meta, _ = rules_mod._parse_frontmatter(after)
            for key in ("waiting_for", "subject_alerts", "noise_domains"):
                if not isinstance(meta.get(key), list):
                    return f"USER.md frontmatter must keep {key} as a list"
        return None

    def _vet_evidence(self, session):
        seq = self.state.last_user_seq(session)
        if seq is None:
            return None, "no user message to cite"
        return [seq], None

    def _vet_file(self, target, op, old, new):
        """The file-side half of `vet`: does the edit apply, and does the result still parse?
        No evidence, no session - what `decide` re-checks before an approved proposal lands,
        since the file may have moved under it since the proposal was made."""
        if not isinstance(target, str):
            return None, "target is required"
        try:
            path = vault_path(self.home, target)
        except ValueError as e:
            return None, str(e)
        if op not in ("add", "replace", "remove"):
            return None, f"unknown op: {op!r}"
        if op in ("add", "replace") and not new:
            return None, "new must not be empty"
        if op in ("replace", "remove") and not old:
            return None, "old must not be empty"
        if target.startswith("skills/") and path.is_symlink():
            return None, "it is a symlink; a skill is a file in the skills directory itself"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if target.startswith("wiki/") and not text and (op != "add" or not new.startswith("# ")):
            return None, "a new wiki page is created with add, and its text must start with '# '"
        old, new = old.rstrip("\n"), new.rstrip("\n")
        idx = None
        if op in ("replace", "remove"):
            lines = text.split("\n") if text else []
            matches = _find_block(lines, old.split("\n"))
            if not matches:
                return None, "old does not match any line in the file"
            if len(matches) > 1:
                return None, "old matches more than one place"
            idx = matches[0]
        after = _apply_edit(text, op, old, new, idx)
        reason = self._check_file(target, text, after, new)
        if reason:
            return None, reason
        return {"target": target, "op": op, "old": old, "new": new}, None

    def vet(self, target, op, old, new, session):
        proposal, reason = self._vet_file(target, op, old, new)
        if reason:
            return None, reason
        evidence_seqs, reason = self._vet_evidence(session)
        if reason:
            return None, reason
        proposal["evidence"] = evidence_seqs
        return proposal, None

    # -- routing and apply -------------------------------------------------------------------
    def edit(self, target, op, old, new, evidence, control):
        with self._edit_lock:  # vet reads the shared connection; another thread's write must not interleave
            return self._edit(target, op, old, new, evidence, control)

    def _edit(self, target, op, old, new, evidence, control):
        session = getattr(control, "session", "") or ""
        proposal, error = self.vet(target, op, old, new, session)
        if error:
            raise ValueError(error)  # a refusal is a failed call, not "ok" in the record
        if target.startswith("wiki/"):
            self.apply(proposal, origin="user")
            return "applied"
        cfg = config.load(self.home)
        # a skill is an instruction (D61): never granted, a user turn can be steered by a page it just read
        if cfg["grants"].get("edit_memory") == "allow" and not target.startswith("skills/"):
            self.apply(proposal, origin="user")
            return "applied"
        if any(r["args"].get("target") == target and r["args"].get("new") == proposal["new"]
               for r in self.state.pending_memory()):  # live: one pass proposed the same fact three times
            return "already queued for the user's approval - do not propose it again"
        id = uuid.uuid4().hex[:12]
        self.record("approval_pending", session=session, origin="user", id=id, call_id=id,
                    tool="edit_memory", args=proposal, messages=[])
        return f"queued for your approval as {id}"

    def apply(self, proposal, approval=None, origin="user"):
        with self._edit_lock:
            target, op = proposal["target"], proposal["op"]
            old, new, evidence = proposal["old"], proposal["new"], proposal["evidence"]
            path = vault_path(self.home, target)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            idx = None
            if op in ("replace", "remove"):
                lines = text.split("\n") if text else []
                matches = _find_block(lines, old.split("\n"))
                if len(matches) != 1:
                    return "old no longer matches exactly one place"
                idx = matches[0]
            after = _apply_edit(text, op, old, new, idx)
            if target.startswith("skills/") and not after.strip():  # an undone add: no 0-byte file for the index to warn about
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".mem-", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(after)
                os.replace(tmp_name, path)
            self.record("memory_apply", origin=origin, target=target, op=op, old=old, new=new,
                        evidence=evidence, approval=approval)
            if target.startswith("wiki/"):
                self.index(target)
            if target == "RULES.md":
                self.on_rules_changed()
            return ""

    def decide(self, approval_id: str, decision: str) -> None:
        row = self.state.approval(approval_id)
        if row is None:
            return
        if decision == "approved":
            args = row["args"]
            # re-vet the file side: the proposal was vetted against the file as it stood when it
            # was made, and the file may have moved since (another apply, a hand edit).
            proposal, reason = self._vet_file(args.get("target"), args.get("op"),
                                              args.get("old", ""), args.get("new", ""))
            if reason:
                self.record("error", origin="system", message=reason)
                return
            proposal["evidence"] = args.get("evidence") or []
            reason = self.apply(proposal, approval=approval_id)
            if reason:
                self.record("error", origin="system", message=reason)
        # denied: nothing to do - the daemon already recorded the `approval` event

    def undo(self, n: int = 1) -> list:
        results = []
        for row in self.state.memory_applies_after(limit=n, skip_undone=True):
            target, op, old, new = row["target"], row["op"], row["old"], row["new"]
            if op == "add":
                inverse = {"target": target, "op": "remove", "old": new, "new": "", "evidence": []}
            elif op == "replace":
                inverse = {"target": target, "op": "replace", "old": new, "new": old, "evidence": []}
            else:  # ponytail: an undone remove re-appends at the end, the original position is not kept
                inverse = {"target": target, "op": "add", "old": "", "new": old, "evidence": []}
            reason = self.apply(inverse, approval="undo", origin="user")
            results.append(f"#{row['seq']}: applied" if not reason else f"#{row['seq']}: {reason}")
        return results
