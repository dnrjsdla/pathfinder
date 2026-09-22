"""Whether to speak - transplanted from Mark.5 `brain/rules.py` (English, `prepare` dropped,
`model` renamed `ask_model`). `RULES.md` in the home directory is one page a person can read:
frontmatter for the global gates, one `## <kind>` table per signal kind, first match wins.

    quiet hours / daily budget (global gates)  ->  the kind's table, first match  ->  ask_model

The point is that most signals cost nothing: a rule the code can evaluate settles them before
any model call, and the model only ever sees the boundary cases (D8, D13).

**The condition vocabulary is closed.** `CORE_CONDITIONS` plus whatever the loaded senses
register - an unknown word in a section that belongs to a loaded sense is a parse error, loudly,
so a typo in RULES.md never reads as "never fires" (Mark.5's own rule, kept)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import clock

ACTIONS = ("speak", "silence", "defer", "ask_model")
DEFAULT_RULES_PATH = Path(__file__).parent / "prompts" / "RULES.md"


def _minutes(fields: dict) -> float:
    # shell's fields carry `seconds`, not `minutes`; a future sense may carry `minutes`
    # directly - `minutes_over` reads whichever the sense gave it.
    return fields["minutes"] if "minutes" in fields else (fields.get("seconds") or 0) / 60


CORE_CONDITIONS = {
    "always": lambda signal, value, today, now: True,
    # occurrences of this kind in the last 10 min, including this one - N means the Nth speaks
    "count_over": lambda signal, value, today, now: today.recent_counts.get(signal.kind, 0) >= value,
    "minutes_until_under": lambda signal, value, today, now: (
        signal.fields.get("minutes_until") is not None and 0 <= signal.fields["minutes_until"] < value),
    "minutes_over": lambda signal, value, today, now: _minutes(signal.fields) > value,
}


@dataclass(frozen=True)
class Rules:
    quiet_hours: str = "23-8"
    daily_budget: int = 8
    min_interval_s: int = 30
    burst_limit: int = 5
    judgment_cap: int = 20
    idle_threshold_s: int = 20
    by_kind: dict = field(default_factory=dict)                       # kind -> [(cond, raw, action, note)]
    conditions: dict = field(default_factory=lambda: dict(CORE_CONDITIONS))


def _int(value, fallback: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return fallback


def _parse_frontmatter(text: str) -> tuple:
    """Flat `key: value` frontmatter, `[...]`/`{...}` values as JSON - no dependency."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n", 1)
    if len(parts) < 2:
        return {}, text
    rest = parts[1]
    end = rest.find("\n---")
    if end == -1:
        return {}, text
    block, body = rest[:end], rest[end + len("\n---"):].lstrip("\n")
    meta = {}
    for line in block.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("[") or value.startswith("{"):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        meta[key] = value
    return meta, body


def parse(text: str, conditions: dict | None = None, validated_kinds: set | None = None) -> Rules:
    """Parse one RULES.md. `validated_kinds` (None = every kind found) names the kinds checked
    against the closed vocabulary - a section for a kind with no loaded sense is kept, unchecked."""
    conditions = conditions if conditions is not None else CORE_CONDITIONS
    meta, body = _parse_frontmatter(text)
    by_kind: dict = {}
    kind = None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--") or stripped.endswith("-->"):
            continue  # blank lines and the step-3 kinds' commented-out placeholders
        if stripped.startswith("## "):
            kind = stripped[3:].strip()
            continue
        if kind is None or not stripped.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or cells[2] not in ACTIONS:
            continue  # the explanation table, or a header/separator row
        cond, raw, action = cells[0], cells[1], cells[2]
        if (validated_kinds is None or kind in validated_kinds) and cond not in conditions:
            raise ValueError(f"unknown condition word {cond!r} in RULES.md section {kind!r}")
        by_kind.setdefault(kind, []).append((cond, raw, action, cells[3] if len(cells) > 3 else ""))
    return Rules(quiet_hours=str(meta.get("quiet_hours") or "23-8"),
                 daily_budget=_int(meta.get("daily_budget"), 8),
                 min_interval_s=_int(meta.get("min_interval_s"), 30),
                 burst_limit=_int(meta.get("burst_limit"), 5),
                 judgment_cap=_int(meta.get("judgment_cap"), 20),
                 idle_threshold_s=_int(meta.get("idle_threshold_s"), 20),
                 by_kind=by_kind, conditions=dict(conditions))


def load(home, senses) -> Rules:
    """RULES.md and USER.md, created from the shipped default on first start. `senses`'
    combined CONDITIONS and KINDS decide what gets validated."""
    home = Path(home)
    rules_path = home / "RULES.md"
    if not rules_path.exists():
        rules_path.write_text(DEFAULT_RULES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    user_path = home / "USER.md"
    if not user_path.exists():  # the empty lists new_mail's rules read; partition 5 fills them in
        user_path.write_text("---\nwaiting_for: []\nsubject_alerts: []\nnoise_domains: []\n---\n", encoding="utf-8")
    conditions = dict(CORE_CONDITIONS)
    validated_kinds = set()
    for s in senses:
        conditions.update(s.conditions)
        validated_kinds.update(s.kinds)
    return parse(rules_path.read_text(encoding="utf-8"), conditions, validated_kinds)


def user_lists(home) -> dict:
    meta, _ = _parse_frontmatter((Path(home) / "USER.md").read_text(encoding="utf-8"))
    return meta


def resolve(value, lists: dict):
    """`USER.xxx` -> that list in USER.md; else `-`/empty -> None, `true`/`false` -> bool, digits -> int."""
    if value.startswith("USER."):
        return lists.get(value[len("USER."):], [])
    if value in ("-", ""):
        return None
    if value in ("true", "false"):
        return value == "true"
    return int(value) if value.lstrip("-").isdigit() else value


def _quiet(spec: str, hour: int) -> bool:
    """`23-8` wraps past midnight. A broken spec never blocks speaking (fail open, not silent)."""
    try:
        start, end = (int(p) for p in str(spec).split("-", 1))
    except ValueError:
        return False
    return (start <= hour or hour < end) if start > end else (start <= hour < end)


def decide(signal, rules: Rules, user_lists: dict, today, now: float) -> tuple:
    """(action, why). Quiet hours, then the daily budget - a signal the user started (trust
    `user`) is exempt from both: an alarm they set for 3 a.m. is theirs to hear, and their own
    reminder spends no budget. Then the kind's table, first match wins; no row -> `ask_model`."""
    if signal.trust != "user":
        if _quiet(rules.quiet_hours, clock.hour(now)):
            return "defer", "quiet hours"
        if rules.daily_budget and today.spoken >= rules.daily_budget:
            return "defer", "daily budget spent"
    for cond, raw, action, _note in rules.by_kind.get(signal.kind, ()):
        predicate = rules.conditions.get(cond)
        if predicate is not None and predicate(signal, resolve(raw, user_lists), today, now):
            return action, f"{cond}={raw}"
    return "ask_model", "no rule matched"


_JUDGE_SYSTEM = ('You decide whether a resident assistant should interrupt its user now. Answer only '
                'JSON: {"speak": true|false, "reason": "..."}')

def judge(signal, cfg: dict) -> tuple:
    """D13: one request, no tools, no history, strict JSON - the only llm import here (see test_invariants)."""
    from . import signals as _signals
    from .llm import LLMError, openai_compat, resolve
    try:
        endpoint = resolve(cfg, cfg.get("worker_model") or cfg.get("model") or "")
        messages = [{"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": _signals.render_envelope([signal])}]
        text = "".join(p for k, p in openai_compat.stream(endpoint, messages, tools=None) if k == "text")
    except LLMError:
        return False, "model unavailable"
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False, "unparseable"
    if not (isinstance(data, dict) and isinstance(data.get("speak"), bool) and isinstance(data.get("reason"), str)):
        return False, "unparseable"
    return data["speak"], data["reason"]
