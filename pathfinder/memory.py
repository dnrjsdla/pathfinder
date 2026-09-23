"""The memory vault: `<home>/USER.md`, `<home>/RULES.md` and `<home>/wiki/*.md`. Files are the
truth; `state`'s page index, summaries and counters are a derived index over them.

Three things live here: the model's own memory tools (registered into `tools.TOOLS` at import,
like `jobs.py`), the code-only gate (`Manager.vet`) that decides whether a proposed edit may land
before any of it is written, and the consolidation pass (`Manager.run_pass`) - one `loop.run` turn,
off the user's own turn, that reads the ledger since the last pass and writes down what is worth
keeping. `Manager` is bound once (`memory.bind`), the same pattern as `jobs.bind`."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import clock, config, ledger, loop
from . import rules as rules_mod
from . import tools as tools_mod
from .broker import Broker, Gate
from .jobs import default_job_step_factory as default_pass_step_factory  # identical: worker_model/model, priority "job"
from .llm import LLMError, openai_compat, resolve
from .loop import TurnControl
from .signals import Signal
from .state import State
from .tools import ToolSpec

CONSOLIDATE_PROMPT = (Path(__file__).parent / "prompts" / "consolidate.txt").read_text(encoding="utf-8")


# -- the vault -------------------------------------------------------------------------------

def vault_path(home, target: str) -> Path:
    """`target` is exactly `USER.md`, `RULES.md` or `wiki/<name>.md` (`<name>` has no `/`, no
    `..`, is not empty and ends with `.md`) - anything else is outside the vault."""
    home = Path(home)
    if target in ("USER.md", "RULES.md"):
        return home / target
    if target.startswith("wiki/"):
        name = target[len("wiki/"):]
        if name and "/" not in name and ".." not in name and name.endswith(".md"):
            return home / "wiki" / name
    raise ValueError(f"outside the vault: {target!r}")

def _cut_at_line(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut] if cut != -1 else text[:limit]

def context(home, state) -> str:
    """USER.md's body plus the wiki map (newest first, cut at 1,000 chars at a line boundary)."""
    body = ""
    user_path = Path(home) / "USER.md"
    if user_path.exists():
        _, body = rules_mod._parse_frontmatter(user_path.read_text(encoding="utf-8"))
        body = body.strip()
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
    """Every index where `block` occurs as consecutive lines of `lines`."""
    n = len(block)
    if n == 0 or n > len(lines):
        return []
    return [i for i in range(len(lines) - n + 1) if lines[i:i + n] == block]

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
    if isinstance(item, bool):
        return None
    if isinstance(item, int):
        return item
    if isinstance(item, str):
        s = item.strip().lstrip("#")
        if s.lstrip("-").isdigit():
            return int(s)
    return None


# -- the tools (opt-in per origin, like jobs.py) -----------------------------------------------

_manager: "Manager | None" = None

def bind(manager: "Manager") -> None:
    global _manager
    _manager = manager

def _search_memory(query, control=None):
    return _manager.search(query)

def _read_memory(path, control=None):
    p = vault_path(_manager.home, path)  # ValueError propagates - the broker turns it into a tool error
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
        description="Read one file of the memory vault: wiki/<name>.md, USER.md or RULES.md.",
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
            "Edit the memory vault one line at a time. target: wiki/<Title>.md, USER.md or RULES.md. "
            "op: add (append a line; creates a wiki page whose text starts with '# Title'), replace "
            "(old -> new, old must be exactly one existing line), remove. USER.md and RULES.md edits "
            "are queued for the user's approval."),
        params={
            "target": {"type": "string", "description": "wiki/<Title>.md, USER.md or RULES.md"},
            "op": {"type": "string", "description": "add, replace or remove"},
            "old": {"type": "string", "description": "the one existing line to replace or remove"},
            "new": {"type": "string", "description": "the line(s) to add or to replace old with"},
            "evidence": {"type": "array", "items": {"type": "integer"},
                        "description": "the #seq numbers you read that support this edit"},
        },
        required=["target", "op"], handler=_edit_memory,
        origins=frozenset({"user", "memory"}), reversible=True,
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
        self._stop = threading.Event()
        self._thread = None
        self._stalled_key = None  # last pushed memory_stalled key - once per local day, not every tick

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
        path = vault_path(self.home, target)
        text = path.read_text(encoding="utf-8")
        first = text.split("\n", 1)[0] if text else ""
        title = first[2:].strip() if first.startswith("# ") else Path(target).stem
        cfg = config.load(self.home)
        model = cfg["embed_model"]
        vector = None
        if model:
            vectors = self._embed(cfg, [text])
            vector = vectors[0] if vectors else None
            if vector is None:
                model = ""
        self.state.index_page(target, title, text, path.stat().st_mtime, vector, model)

    def reindex_pages(self) -> None:
        _reindex(self.home, config.load(self.home), self.state)

    def context(self) -> str:
        return context(self.home, self.state)

    # -- the gate --------------------------------------------------------------------------
    def _rule_vocab(self):
        conditions = dict(rules_mod.CORE_CONDITIONS)
        kinds = set()
        for s in self.senses:
            conditions.update(s.conditions)
            kinds.update(s.kinds)
        return conditions, kinds

    def _check_file(self, target, after, new):
        if target == "RULES.md":
            conditions, kinds = self._rule_vocab()
            try:
                rules_mod.parse(after, conditions, kinds)
            except ValueError as e:
                return str(e)
            for line in new.split("\n"):
                stripped = line.strip()
                if not stripped.startswith("|"):
                    continue
                cells = [c.strip().strip("`") for c in stripped.strip("|").split("|")]
                if len(cells) >= 3 and cells[2] not in rules_mod.ACTIONS:
                    return f"unknown action: {cells[2]!r}"
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
            for n in seqs:
                if allowed[n].get("origin") != "user":
                    return None, "USER.md takes only the user's own words (a user or reaction line)"
        return seqs, None

    def vet(self, target, op, old, new, evidence, origin, allowed, session):
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
        reason = self._check_file(target, after, new)
        if reason:
            return None, reason
        evidence_seqs, reason = self._vet_evidence(evidence, origin, allowed, session, target)
        if reason:
            return None, reason
        return {"target": target, "op": op, "old": old, "new": new, "evidence": evidence_seqs}, None

    # -- routing and apply -------------------------------------------------------------------
    def edit(self, target, op, old, new, evidence, control):
        p = getattr(control, "memory_pass", None)
        origin = "memory" if p else "user"
        session = getattr(control, "session", "") or ""
        allowed = p["allowed"] if p else {}
        proposal, error = self.vet(target, op, old, new, evidence, origin, allowed, session)
        if error:
            return error
        if target.startswith("wiki/"):
            self.apply(proposal, origin=origin)
            if p is not None:
                p["applied"] += 1
                p["pages"].append(target)
            return "applied"
        cfg = config.load(self.home)
        if origin == "user" and cfg["grants"].get("edit_memory") == "allow":
            self.apply(proposal, origin="user")
            return "applied"
        id = uuid.uuid4().hex[:12]
        self.record("approval_pending", session=session, origin=origin, id=id, call_id=id,
                    tool="edit_memory", args=proposal, messages=[])
        if p is not None:
            p["proposed"] += 1
        return f"queued for your approval as {id}"

    def apply(self, proposal, approval=None, origin="user"):
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
        path.parent.mkdir(parents=True, exist_ok=True)
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
            reason = self.apply(row["args"], approval=approval_id)
            if reason:
                self.record("error", origin="memory", message=reason)
        # denied: nothing to do - the daemon already recorded the `approval` event

    def undo(self, n: int = 1) -> list:
        results = []
        for row in self.state.memory_applies_after(limit=n):
            target, op, old, new = row["target"], row["op"], row["old"], row["new"]
            if op == "add":
                inverse = {"target": target, "op": "remove", "old": new, "new": "", "evidence": []}
            elif op == "replace":
                inverse = {"target": target, "op": "replace", "old": new, "new": old, "evidence": []}
            else:  # ponytail: an undone remove re-appends at the end, the original position is not kept
                inverse = {"target": target, "op": "add", "old": "", "new": old, "evidence": []}
            reason = self.apply(inverse, origin="user")
            results.append(f"#{row['seq']}: applied" if not reason else f"#{row['seq']}: {reason}")
        return results

    # -- the pass -----------------------------------------------------------------------------
    def read_ledger(self, p, after, limit):
        lines = [(e, line) for e, line in p["lines"] if e["seq"] > after][:limit]
        p["allowed"].update({e["seq"]: e for e, _line in lines})
        p["read"] += len(lines)
        return tools_mod.clip("\n".join(line for _e, line in lines)) if lines else "(end of range)"

    def _emit(self):
        def emit(kind, payload):
            if kind not in ("tool_call", "tool_result"):
                return
            recorded = {k: (tools_mod.clip_for_ledger(v) if k in ("args", "result") else v)
                       for k, v in payload.items()}
            self.record(kind, origin="memory", **recorded)
        return emit

    def _readable_events(self, since):
        events = [e for e in self.ledger.read(since) if e["type"] != "memory_pass"]
        readable = [(e, line) for e in events if (line := render_line(e, self.state)) is not None]
        return events, readable

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
        if sid and prev:
            goal += f"Current session: {sid}. Previous summary of its earlier part:\n{prev['text']}"
        elif sid:
            goal += f"Current session: {sid}. No previous summary."
        else:
            goal += "No current session."
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
        out, error = None, None
        try:
            out = loop.run(step, messages, call_tool=broker.call, emit=self._emit(),
                           budget=self.rules.consolidate_steps, control=control)
        except Gate as g:
            self.record("error", origin="memory", message=f"memory pass gated: {g.name}")
        except LLMError as e:
            error = e.message
        else:
            if out.kind == "answer" and out.text.strip() and sid:
                upto_s = self.state.nth_newest_message_seq(sid, 5)
                if upto_s is not None:
                    self.record("session_summary", session=sid, origin="memory", upto=upto_s,
                               text=out.text.strip())
        fields = {"since": since, "upto": upto, "read": p["read"], "steps": out.steps if out else 0,
                 "usage": out.usage if out else None, "applied": p["applied"], "proposed": p["proposed"]}
        if error:
            fields["error"] = error
        self.record("memory_pass", origin="memory", **fields)  # the cursor advances even on error
        if p["proposed"] > 0:
            self.push(Signal(
                sense="memory", kind="memory_proposed", key=f"memory:proposed:{upto}",
                summary=f"Memory: {p['proposed']} proposal(s) waiting for your approval - `pathfinder approvals`",
                fields={"proposed": p["proposed"], "applied": p["applied"], "pages": p["pages"]}, trust="self"))
        report = f"read {p['read']}, applied {p['applied']}, proposed {p['proposed']}"
        return f"{report}, error: {error}" if error else report

    # -- the trigger ----------------------------------------------------------------------
    def stalled(self, now=None) -> bool:
        """Three local days in a row, each with a pass that read lines and applied nothing -
        the offline signal a wiki-only or dry pass alone must not raise."""
        now = now if now is not None else time.time()
        days = self.state.memory_stats(now)["days"]
        return len(days) == 3 and all(d["passes"] > 0 and d["read"] > 0 and d["applied"] == 0 for d in days)

    def _due(self) -> bool:
        if self._pass_lock.locked() or self.is_busy():
            return False
        last = self.state.last_user_ts()
        if last is None or time.time() - last < self.rules.consolidate_idle_min * 60:
            return False
        _events, readable = self._readable_events(self.state.memory_cursor)
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
