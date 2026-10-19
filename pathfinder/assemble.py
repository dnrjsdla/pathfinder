"""The assembler (§7-1), the ONLY producer of model requests, and the fold (V5). A Request is built from the
ledger-derived state alone - a clone's context rows in one session since its last fold - never from an
in-memory history, so the same state gives the same bytes and a restart reassembles the same window. Five
pieces: the static system (frozen bytes), the tools, the item's resident copy (frozen in `turn_start.resident`),
the history window and the current step. The fold picks its cut here (`fold_point`), builds the summarize
request (`fold_request`) and the harness's handle lines (`handle_lines`); the clone records the `summary`."""
import json
from dataclasses import dataclass
from pathlib import Path

from . import results, tools
from .llm import THINK_TYPES, Request

_PROMPTS = Path(__file__).parent / "prompts"
SYSTEM = (_PROMPTS / "root.txt").read_text(encoding="utf-8")       # piece 1: read once, never formatted
FOLD_PROMPT = (_PROMPTS / "fold.txt").read_text(encoding="utf-8")
RESIDENT_BUDGET = 4000   # chars of the resident block (piece 3)
HISTORY_BUDGET = 24000   # chars of the window (piece 4); over it, the clone folds
FOLD_KEEP = 12000        # chars a fold leaves of the window
STEP_BUDGET = 8000       # chars of one step's results (piece 5); over it, later results shrink to their handle
FOLD_MAX_TOKENS = 2000
HEADER = "Earlier in this conversation — a record, not a request:"
RESTART_UNKNOWN = "outcome unknown: interrupted by a restart"  # a recorded call that has no result


@dataclass(frozen=True)
class View:
    """Whose window: clone `id` in `session` (`role` is its `models` key). `epoch` is the seq up to which
    thinking is stripped (the clone takes the later of this daemon's start and the last fold); `model` is a
    spawn override ("" = the role's chain)."""
    id: str
    role: str
    session: str
    epoch: int
    model: str = ""


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _window(state, view):
    """The last fold (None before the first) and the context rows since it: summary rows and every row of a
    turn that ended `refused` left out (nothing was sent after them)."""
    fold = state.last_fold(view.id, view.session)
    rows = state.context(view.id, view.session, fold["upto"] if fold else 0)
    refused = {r["turn"] for r in rows if r["type"] == "turn_end" and r.get("kind") == "refused"}
    return fold, [r for r in rows if r["type"] != "summary" and r["turn"] not in refused]


def _handles(fold) -> set[int]:
    return {int(h.removeprefix("r:")) for h in fold["handles"]} if fold else set()


def _answer(state, row) -> tuple[str, bool, str]:
    """(text, is_error, handle) for a `result` row: a `results.INLINE` tool's output in full, a text as itself
    (read_result's is cleaned by `results.read`; search_tools' is registry text, MCP's cleaned at load), anything
    else `results.render` of the row with its stored body (it cleans). No other body goes inline."""
    body = state.result(row["seq"])["body"]
    if row["tool"] in results.INLINE:
        text = body if isinstance(body, str) else _dumps(body)
    else:
        text = results.render({**row, "body": body})
    return text, not row.get("ok"), f"r:{row['seq']}"


def _step(calls: list[str], answers: dict) -> list[dict]:
    """One step's tool_results in call order. A call with no recorded answer was cut by a restart; once the
    step passes STEP_BUDGET chars, each later result is only its handle."""
    out, used = [], 0
    for call_id in calls:
        text, error, handle = answers.get(call_id, (RESTART_UNKNOWN, True, ""))
        if handle and used + len(text) > STEP_BUDGET:
            text = _dumps({"handle": handle, "note": f"left out: this step's results passed {STEP_BUDGET:,} "
                                                     "characters; open it with read_result"})
        used += len(text)
        block = {"type": "tool_result", "tool_use_id": call_id, "content": text}
        out.append({**block, "is_error": True} if error else block)
    return out


def _messages(state, view, fold, rows) -> list[dict]:
    """The model view of `rows`: the last summary (under HEADER) opens the first user message; assistant blocks
    are replayed verbatim (thinking stripped up to the epoch); a step's results and the texts that arrived
    since (an opening say, steers, a report, a budget note) form one user message, tool_results first; a gate
    answers its call with the waiting status, and a handed-off call's own `result` is never a tool_result; the
    item's resident copy follows every user message of the item. The resident and the summary (model text that
    may carry third-party text) are cleaned here; results were cleaned by `results`."""
    out, texts, answers, calls, item, resident = [], [], {}, [], "", ""
    if fold:
        texts.append(f"{HEADER}\n{tools.clean(fold['text'])}")
    for row in [*rows, None]:
        kind = row["type"] if row else None
        if kind in ("assistant", None) and (calls or texts):
            out.append({"role": "user", "content": _step(calls, answers) + [{"type": "text", "text": t}
                                                                           for t in texts]})
            if item:
                out.append({"role": "system", "content": resident, "item": item})
            texts, answers, calls = [], {}, []
        if kind == "turn_start":
            item, resident = row["turn"], tools.clean(row.get("resident", ""))
        elif kind == "user":
            texts.append(row["text"])
        elif kind == "assistant":
            blocks = [b for b in row["blocks"] if row["seq"] > view.epoch or b["type"] not in THINK_TYPES]
            out.append({"role": "assistant", "model": row["model"], "content": blocks})
            calls = [b["id"] for b in blocks if b["type"] == "tool_use"]
        elif kind == "result" and not row.get("gate"):
            answers[row["call_id"]] = _answer(state, row)
        elif kind == "gate":
            answers[row["call_id"]] = (_dumps({"status": "waiting for approval", "gate": row["seq"]}), False, "")
    return out


def _chars(messages) -> int:
    """A window's size: chars of each message's serialized content, thinking excluded, one resident copy per
    item (the same on both providers, so a fallback mid-session never moves a fold point)."""
    items, n = set(), 0
    for m in messages:
        if m["role"] != "system":
            n += len(_dumps([b for b in m["content"] if b["type"] not in THINK_TYPES]))
        elif m["item"] not in items:
            items.add(m["item"])
            n += len(m["content"])
    return n


def assemble(state, view: View, tools: list[dict]) -> Request:
    """The request for `view`'s next step: SYSTEM, read_result then `tools` (neutral specs) by name, and the
    window's messages (the history, then the current item). Same state, same bytes."""
    fold, rows = _window(state, view)
    turn = next((r["turn"] for r in reversed(rows) if r["type"] == "turn_start"), "")
    return Request(role=view.role, system=SYSTEM,
                   tools=(results.READ_RESULT, *sorted(tools, key=lambda t: t["name"])),
                   messages=tuple(_messages(state, view, fold, rows)), model=view.model, clone=view.id, turn=turn)


def window_handles(state, view: View) -> set[int]:
    """The seen set for `results.read`: every `result` in the window plus the last summary's handles."""
    fold, rows = _window(state, view)
    return {r["seq"] for r in rows if r["type"] == "result"} | _handles(fold)


def resident(parts: list[str]) -> str:
    """The resident block the clone freezes in `turn_start.resident`: the non-empty parts in order, cut at
    RESIDENT_BUDGET chars (the later parts are cut first). Raw: `assemble` cleans it on the way out."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())[:RESIDENT_BUDGET]


def fold_point(state, view: View) -> int | None:
    """None while the window is within HISTORY_BUDGET; else the seq of the `turn_end` up to which the oldest
    whole turns fold, leaving at most FOLD_KEEP chars (or every turn but the newest, when even that leaves
    more). A turn's end is never inside a tool round, and the newest item never folds."""
    fold, rows = _window(state, view)
    if _chars(_messages(state, view, fold, rows)) <= HISTORY_BUDGET:
        return None
    newest = max((r["seq"] for r in rows if r["type"] == "turn_start"), default=0)
    ends = [r["seq"] for r in rows if r["type"] == "turn_end" and r["seq"] < newest]
    for end in ends:
        if _chars(_messages(state, view, None, [r for r in rows if r["seq"] > end])) <= FOLD_KEEP:
            return end
    return ends[-1] if ends else None


def fold_request(state, view: View, upto: int) -> Request:
    """The summarize request for the window's rows up to `upto`: FOLD_PROMPT, then the previous summary and the
    folded turns as text - user and assistant text, each call with its args, each answer as the model saw it."""
    fold, rows = _window(state, view)
    lines = [f"{HEADER}\n{tools.clean(fold['text'])}"] if fold else []
    for m in _messages(state, view, None, [r for r in rows if r["seq"] <= upto]):
        for b in m["content"] if m["role"] != "system" else ():
            if b["type"] == "text":
                lines.append(f"{m['role']}: {b['text']}")
            elif b["type"] == "tool_use":
                lines.append(f"call {b['name']} {_dumps(b['input'])}")
            elif b["type"] == "tool_result":
                lines.append(f"result: {b['content']}")
    return Request(role="summarize", system=FOLD_PROMPT, max_tokens=FOLD_MAX_TOKENS,
                   messages=({"role": "user", "content": [{"type": "text", "text": "\n\n".join(lines)}]},))


def handle_lines(state, view: View, upto: int) -> list[str]:
    """The harness's lines for the fold's `summary.text`, one per handle of the folded rows and of the previous
    summary, oldest first: `r:712 read_file list 100` - handle, tool, summary type and size, and no third-party
    string, so a handle survives whatever the model wrote. `summary.handles` is each line's first word."""
    fold, rows = _window(state, view)
    seqs = {r["seq"] for r in rows if r["type"] == "result" and r["seq"] <= upto} | _handles(fold)
    lines = []
    for seq in sorted(seqs):
        row = state.result(seq)
        found = results.summary(row["body"])
        size = {"list": f" {found.get('count')}", "text": f" {found.get('chars')} chars"}.get(found["type"], "")
        lines.append(f"r:{seq} {row['tool']} {found['type'] + size if row['ok'] else 'error'}")
    return lines
