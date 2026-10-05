"""The memory vault: `<home>/USER.md`, `<home>/RULES.md` and `<home>/wiki/*.md`. Files are the
truth; `state`'s page index, summaries and counters are a derived index over them.

Three things live here: the model's own memory tools (registered into `tools.TOOLS` at import,
like `jobs.py`), the code-only gate (`Manager.vet`) that decides whether a proposed edit may land
before any of it is written, and the consolidation pass (`Manager.run_pass`) - one `loop.run` turn,
off the user's own turn, that reads the ledger since the last pass and writes down what is worth
keeping. `Manager` is bound once (`memory.bind`), the same pattern as `jobs.bind`."""
from __future__ import annotations

import difflib
import importlib
import os
import pkgutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import clock, config, ledger, loop
from . import rules as rules_mod
from . import skills as skills_mod
from . import senses as senses_pkg
from . import tools as tools_mod
from .broker import Broker, Gate
from .jobs import default_job_step_factory as default_pass_step_factory  # identical: worker_model/model, priority "job"
from .llm import LLMError, openai_compat, resolve
from .loop import TurnControl
from .signals import Signal
from .state import State
from .tools import ToolSpec

CONSOLIDATE_PROMPT = (Path(__file__).parent / "prompts" / "consolidate.txt").read_text(encoding="utf-8")
SUMMARIZE_PROMPT = (Path(__file__).parent / "prompts" / "summarize.txt").read_text(encoding="utf-8")
_RETRY_S = 1800  # D39: after a pass the model never answered, the trigger waits this long to retry


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

def _coerce_seq(item):
    """Accept `12`, `"12"` or `"#12"` - what the model actually sends for a cited seq."""
    if isinstance(item, int) and not isinstance(item, bool):
        return item
    s = item.strip().lstrip("#") if isinstance(item, str) else ""
    return int(s) if s.isdigit() else None


# -- the tools (opt-in per origin, like jobs.py) -----------------------------------------------

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
        listing = ", ".join(pg["path"] for pg in pages) if pages else "(no pages yet)"
        return f"no such page: {path}. Pages: {listing}"
    return tools_mod.clip(p.read_text(encoding="utf-8"))

def _read_memory_ledger(after, limit=80, control=None):
    p = getattr(control, "memory_pass", None)
    if p is None:
        return "not in a consolidation pass"
    return _manager.read_ledger(p, after, limit)

def _edit_memory(target, op, old="", new="", evidence=None, control=None):
    return _manager.edit(target, op, old, new, evidence or [], control)

TOOLS = {
    "search_memory": ToolSpec(
        name="search_memory",
        description="Search the memory vault (USER.md, RULES.md, wiki pages) for pages matching a query.",
        params={"query": {"type": "string", "description": "search text"}},
        required=["query"], handler=_search_memory, origins=frozenset(ledger.ORIGINS),
    ),
    "read_memory": ToolSpec(
        name="read_memory",
        description="Read one file of the memory vault: wiki/<name>.md (exactly as search_memory "
                    "or the page list names it), USER.md or RULES.md.",
        params={"path": {"type": "string", "description": "wiki/<name>.md, USER.md or RULES.md"}},
        required=["path"], handler=_read_memory, origins=frozenset(ledger.ORIGINS),
    ),
    "read_ledger": ToolSpec(
        name="read_ledger",
        description="Read the ledger slice this consolidation pass is reading, paging with `after` "
                    "until it returns nothing.",
        params={
            "after": {"type": "integer", "description": "the #seq to read after"},
            "limit": {"type": "integer", "description": "how many lines to return (default 80)"},
        },
        required=["after"], handler=_read_memory_ledger, origins=frozenset({"memory"}),
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
                       "(a procedure the user asked to keep; always needs their approval)"},
            "op": {"type": "string", "description": "add, replace or remove"},
            "old": {"type": "string", "description": "the one existing line to replace or remove"},
            "new": {"type": "string", "description": "the line(s) to add or to replace old with"},
            "evidence": {"type": "array", "items": {"type": "integer"},
                        "description": "the #seq numbers you read that support this edit"},
        },
        required=["target", "op"], handler=_edit_memory,
        origins=frozenset({"user", "memory"}), write=True, reversible=True,
    ),
}
tools_mod.TOOLS.update(TOOLS)


# -- rendering the ledger for the pass ----------------------------------------------------------

def render_line(event, state):
    """One readable line per event the pass (or a human) can make sense of, or None for tool
    chatter and other bookkeeping the pass need not read."""
    t, seq = event["type"], event["seq"]
    clip = tools_mod.clip_for_ledger
    if t == "user" and event.get("origin") == "user":
        if event.get("job"):
            return None  # a steer said to a worker, not this session's own chat
        tag = "user steer" if event.get("steer") else "user"
        return f"#{seq} [{tag}] {clip(event.get('text', ''), 400)}"
    if t == "assistant" and event.get("final") and event.get("origin") in ("user", "signal"):
        return f"#{seq} [assistant] {clip(event.get('text', ''), 400)}"
    if t == "signal":
        return f"#{seq} [signal {event.get('kind', '')}] {clip(event.get('summary', ''), 400)}"
    if t == "judgment":
        kind = state.signal_kind(event.get("signal_key", "")) or "unknown"
        return f"#{seq} [judgment {kind}] speak={event.get('speak')} {clip(event.get('reason', ''), 400)}"
    if t == "reaction":
        kinds = state.reaction_kinds(event.get("delivery_id", ""))
        return f"#{seq} [reaction {event.get('vote', '')}] on {kinds}"
    if t == "approval":
        row = state.approval(event.get("id", ""))
        tool = row["tool"] if row else ""
        args = row["args"] if row and isinstance(row.get("args"), dict) else {}
        label = f"{tool} {args['target']}" if args.get("target") else tool
        return f"#{seq} [approval {event.get('decision', '')}] {label}"
    if t == "job_done":
        return f"#{seq} [job_done] {clip(event.get('summary', ''), 400)}"
    if t == "job_failed":
        return f"#{seq} [job_failed] {clip(event.get('reason', ''), 400)}"
    if t == "memory_apply":
        return f"#{seq} [memory_apply] {event.get('target', '')} {event.get('op', '')}"
    return None


class Manager:
    def __init__(self, home, record, push, step_factory, rules, senses, is_busy):
        self.home = Path(home)
        self._record = record
        self._push = push
        self.step_factory = step_factory
        self.rules = rules              # the daemon rebinds this on a RULES.md reload
        self.senses = senses            # list of signals.Sense - RULES.md's condition/kind vocabulary
        self.is_busy = is_busy          # daemon callback: a session turn or a job holds the slot
        self.dispatch = lambda fn: fn()  # swapped for call_soon_threadsafe by the daemon, like jobs.py
        self.state = State(self.home / "state.db")   # this Manager's own connection
        self.ledger = ledger.Ledger(self.home)        # read() only - never append()
        self.on_rules_changed = lambda: None           # the daemon sets this after an approved RULES.md edit
        self._pass_lock = threading.Lock()
        self._edit_lock = threading.RLock()  # apply()/index() - the pass thread and a user turn's own thread
        self._stop = threading.Event()
        self._thread = None
        self._stalled_key = None  # last pushed memory_stalled key - once per local day, not every tick
        self._retry_at = 0.0  # D39: no pass before this after an LLMError
        # a deleted state.db loses the page index; a wiki with pages on disk and no index means
        # it was never rebuilt on this start - the CLI's `memory undo` message promises this.
        wiki_dir = self.home / "wiki"
        if not self.state.pages() and wiki_dir.exists() and any(wiki_dir.glob("*.md")):
            self.reindex_pages()

    def close(self) -> None:
        self.state.close()

    def record(self, type, **fields):
        self.dispatch(lambda: self._record(type, **fields))

    def push(self, signal):
        self.dispatch(lambda: self._push(signal))

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
        for _finder, name, _is_pkg in pkgutil.iter_modules(senses_pkg.__path__):
            if name.startswith("_"):
                continue
            mod = importlib.import_module(f"pathfinder.senses.{name}")
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

    def _vet_evidence(self, evidence, origin, allowed, session, target):
        if origin == "user":
            seq = self.state.last_user_seq(session)
            if seq is None:
                return None, "no user message to cite"
            seqs = [seq]
        else:
            seqs = []
            for item in (evidence or []):
                n = _coerce_seq(item)
                if n is None:
                    return None, f"bad evidence value: {item!r}"
                seqs.append(n)
            if not seqs:
                return None, "evidence is required"
            for n in seqs:
                if n not in allowed:
                    return None, f"evidence #{n} was not in the lines you read"
        if target == "USER.md" and origin == "memory":
            # every event whose text is the user's own: a chat line (steer included - a steer
            # is still the user's own words) or a reaction, never an assistant/approval line
            # just because it happens to carry origin "user" (B4: those three carry it too).
            for n in seqs:
                if allowed[n].get("type") not in ("user", "reaction"):
                    return None, "USER.md takes only the user's own words (a user or reaction line)"
        return seqs, None

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

    def vet(self, target, op, old, new, evidence, origin, allowed, session):
        proposal, reason = self._vet_file(target, op, old, new)
        if reason:
            return None, reason
        evidence_seqs, reason = self._vet_evidence(evidence, origin, allowed, session, target)
        if reason:
            return None, reason
        proposal["evidence"] = evidence_seqs
        return proposal, None

    # -- routing and apply -------------------------------------------------------------------
    def edit(self, target, op, old, new, evidence, control):
        with self._edit_lock:  # vet reads the shared connection; another thread's write must not interleave
            return self._edit(target, op, old, new, evidence, control)

    def _edit(self, target, op, old, new, evidence, control):
        p = getattr(control, "memory_pass", None)
        origin = "memory" if p else "user"
        session = getattr(control, "session", "") or ""
        allowed = p["allowed"] if p else {}
        if isinstance(target, str) and target.startswith("skills/") and origin != "user":
            raise ValueError("a skill is proposed only from the user's own turn, never by the memory pass")
        proposal, error = self.vet(target, op, old, new, evidence, origin, allowed, session)
        if error:
            raise ValueError(error)  # a refusal is a failed call, not "ok" in the record
        if target.startswith("wiki/"):
            self.apply(proposal, origin=origin)
            if p is not None:
                p["applied"] += 1
                p["pages"].append(target)
            return "applied"
        cfg = config.load(self.home)
        # a skill is an instruction (D61): never granted, a user turn can be steered by a page it just read
        if origin == "user" and cfg["grants"].get("edit_memory") == "allow" and not target.startswith("skills/"):
            self.apply(proposal, origin="user")
            return "applied"
        if any(r["args"].get("target") == target and r["args"].get("new") == proposal["new"]
               for r in self.state.pending_memory()):  # live: one pass proposed the same fact three times
            return "already queued for the user's approval - do not propose it again"
        id = uuid.uuid4().hex[:12]
        self.record("approval_pending", session=session, origin=origin, id=id, call_id=id,
                    tool="edit_memory", args=proposal, messages=[])
        if p is not None:
            p["proposed"] += 1
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
                self.record("error", origin="memory", message=reason)
                return
            proposal["evidence"] = args.get("evidence") or []
            reason = self.apply(proposal, approval=approval_id)
            if reason:
                self.record("error", origin="memory", message=reason)
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

    # -- the pass -----------------------------------------------------------------------------
    READ_LEDGER_CHAR_LIMIT = 7000

    def read_ledger(self, p, after, limit):
        """Pages by size, not `tools.clip`'s head+tail: a busy page must not lose its middle
        while every line still counts as read. Always at least one line; only what is actually
        returned enters `allowed`/`p["read"]`, so paging with the last returned seq is exact."""
        candidates = [(e, line) for e, line in p["lines"] if e["seq"] > after][:limit]
        picked, total = [], 0
        for e, line in candidates:
            if picked and total + len(line) > self.READ_LEDGER_CHAR_LIMIT:
                break
            picked.append((e, line))
            total += len(line)
        p["allowed"].update({e["seq"]: e for e, _line in picked})
        p["read"] += len(picked)
        return "\n".join(line for _e, line in picked) if picked else "(end of range)"

    def _emit(self):
        def emit(kind, payload):
            if kind not in ("tool_call", "tool_result"):
                return
            recorded = {k: (tools_mod.clip_for_ledger(v) if k in ("args", "result") else v)
                       for k, v in payload.items()}
            self.record(kind, origin="memory", **recorded)
        return emit

    def _readable_events(self, since, limit=400):
        """Stops once `limit` readable lines are found - `upto` (the last event examined) is
        where the next pass continues from, so a long-idle home's first pass reads one bounded
        slice instead of the whole ledger."""
        examined, readable = [], []
        for e in self.ledger.read(since):
            if e["type"] == "memory_pass":
                continue
            examined.append(e)
            line = render_line(e, self.state)
            if line is not None:
                readable.append((e, line))
                if len(readable) >= limit:
                    break
        return examined, readable

    def run_pass(self, force: bool = False) -> str:
        if not self._pass_lock.acquire(blocking=False):
            return "a pass is already running"
        try:
            return self._run_pass(force)
        finally:
            self._pass_lock.release()

    def _run_pass(self, force: bool) -> str:
        since = self.state.memory_cursor
        events, readable = self._readable_events(since)
        if not readable:
            return "nothing to read"
        if len(readable) < self.rules.consolidate_min_events and not force:
            return "not enough to read yet"
        upto = events[-1]["seq"]
        cfg = config.load(self.home)
        sid = self.state.latest_session(cfg["session_resume_s"])
        prev = self.state.summary(sid) if sid else None
        p = {"since": since, "upto": upto, "lines": readable, "allowed": {}, "read": 0,
             "applied": 0, "proposed": 0, "pages": [], "session": sid}
        conditions, kinds = self._rule_vocab()
        goal = (f"Consolidate ledger events #{since + 1}..#{upto} ({len(readable)} readable lines; "
                "use read_ledger, paging with `after` until it returns nothing).\n"
                f"RULES.md vocabulary — conditions: {', '.join(sorted(conditions))}; "
                f"actions: speak, silence, defer, ask_model; kinds: {', '.join(sorted(kinds))}.\n")
        goal += "\n" + self.context()  # USER.md and the page list: edit what exists (live: three pages, one topic)
        pend = self.state.pending_memory()  # a proposal from an earlier pass is still in the box
        if pend:
            goal += "\nAlready waiting for the user's approval - never propose these again:\n" + "\n".join(
                f"- {r['args']['target']} {r['args']['op']}: {r['args']['new'][:120]}" for r in pend)
        messages = [{"role": "system", "content": CONSOLIDATE_PROMPT},
                    {"role": "system", "content": clock.now_line()},
                    {"role": "user", "content": goal}]
        control = TurnControl()
        control.memory_pass = p
        control.session = sid or ""
        mem_tools = tools_mod.for_origin("memory")
        broker = Broker(mem_tools, "memory", {}, control=control, emit=self._emit(),
                        approved=None, denied=frozenset())
        step = self.step_factory(cfg, control, mem_tools)
        out, error, kept, steps, usage = None, None, False, 0, None
        try:
            out = loop.run(step, messages, call_tool=broker.call, emit=self._emit(),
                           budget=self.rules.consolidate_steps, control=control)  # D47: no final_step, the summary is its own call
            steps, usage = out.steps, out.usage
            if sid:
                summed = self._summarize(sid, cfg, prev, control)
                if summed:
                    steps, usage = steps + summed.steps, loop._sum_usage(usage, summed.usage)
        except Gate as g:
            self.record("error", origin="memory", message=f"memory pass gated: {g.name}")
        except LLMError as e:  # an outage keeps the slice; a 4xx the request caused (400, 413) moves on
            outage = e.status in (0, 401, 402, 408, 429) or e.status >= 500
            error, kept = e.message, outage and not (p["applied"] or p["proposed"]) and out is None
        except Exception as e:  # a bug in the step or a tool must not escape - the cursor still advances
            error = f"internal: {type(e).__name__}"
        fields = {"since": since, "upto": upto, "read": p["read"], "steps": steps,
                 "usage": usage, "applied": p["applied"], "proposed": p["proposed"]}
        if error:
            fields["error"] = error
        self._retry_at = time.time() + _RETRY_S if kept else 0.0  # a pass that got an answer clears the wait
        if kept:
            # D39: the model never read the slice (offline, out of credit) - keep it for the next
            # pass; live 2026-09-26 lost the owner's own instruction this way. An internal error
            # still moves on (a slice that breaks the step would otherwise loop forever).
            fields["upto"] = since
        self.record("memory_pass", origin="memory", **fields)  # `upto` becomes the cursor
        if p["proposed"] > 0:
            self.push(Signal(
                sense="memory", kind="memory_proposed", key=f"memory:proposed:{upto}",
                summary=f"Memory: {p['proposed']} proposal(s) waiting for your approval - `pathfinder approvals`",
                fields={"proposed": p["proposed"], "applied": p["applied"], "pages": p["pages"]}, trust="self"))
        report = f"read {p['read']}, applied {p['applied']}, proposed {p['proposed']}"
        return f"{report}, error: {error}" if error else report

    def _summarize(self, sid, cfg, prev, control):
        """D47: the session summary is its own request, no tools - the write call never sees it."""
        after = prev["upto"] if prev else 0
        upto_s = self.state.nth_newest_message_seq(sid, 5)
        if upto_s is None or upto_s <= after:
            return None
        lines = "\n".join(line for m in self.state.session_messages(sid, after, upto_s)
                          if (line := render_line(m, self.state)) is not None)
        body = (f"Previous summary:\n{prev['text']}\n\n" if prev else "") + "Conversation since:\n" \
            + tools_mod.clip(lines, 16000)
        messages = [{"role": "system", "content": SUMMARIZE_PROMPT},
                    {"role": "system", "content": clock.now_line()},
                    {"role": "user", "content": body}]
        def no_tools(*_):
            raise LLMError(400, "summary: the model asked for a tool")

        out = loop.run(self.step_factory(cfg, control, {}), messages, call_tool=no_tools,
                       emit=self._emit(), budget=1, control=control)
        if out.kind == "answer" and out.text.strip():
            self.record("session_summary", session=sid, origin="memory", upto=upto_s, text=out.text.strip())
        return out

    # -- the trigger ----------------------------------------------------------------------
    def stalled(self, now=None) -> bool:
        """Three local days in a row, each with a pass that read lines and applied nothing -
        the offline signal a wiki-only or dry pass alone must not raise."""
        now = now if now is not None else time.time()
        days = self.state.memory_stats(now)["days"]
        return len(days) == 3 and all(d["passes"] > 0 and d["read"] > 0 and d["applied"] == 0 for d in days)

    def _due(self) -> bool:
        if self._pass_lock.locked() or self.is_busy() or time.time() < self._retry_at:
            return False
        last = self.state.last_user_ts()
        if last is None or time.time() - last < self.rules.consolidate_idle_min * 60:
            return False
        # only needs to know whether enough exist, not to read the whole ledger every 60 s
        _events, readable = self._readable_events(self.state.memory_cursor, limit=self.rules.consolidate_min_events)
        return len(readable) >= self.rules.consolidate_min_events

    def tick(self) -> None:
        if self._due():
            threading.Thread(target=self.run_pass, daemon=True).start()
        now = time.time()
        if self.stalled(now):
            days = self.state.memory_stats(now)["days"]
            key = f"memory:stalled:{int(days[0]['start'])}"
            if key != self._stalled_key:  # else: still stalled, already pushed today - don't repeat every 60s
                self._stalled_key = key
                self.push(Signal(sense="memory", kind="memory_stalled", key=key,
                    summary="Memory: nothing learned in 3 days although there was something to read",
                    fields={"days": 3}, trust="self"))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(60):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad tick must not kill the trigger thread
                pass

    def stop(self) -> None:
        self._stop.set()
