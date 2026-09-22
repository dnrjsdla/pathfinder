"""Configuration: one file (config.json), one writer (set()). A model name is
data, never code - DEFAULTS keeps it empty so nothing ships a hardcoded model."""
import importlib
import json
import os
import tempfile
from pathlib import Path

from . import clock


class ConfigError(Exception):
    pass


DEFAULTS = {
    "model": "",
    "worker_model": "",
    "embed_model": "",
    "providers": {"ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": ""}},
    "max_steps": 8,
    "session_resume_s": 7200,
    "grants": {},
    "senses": {},      # sense name -> options dict; present key = on (signals.load imports it)
    "schedules": [],   # [{id, at, repeat, text}], written only through schedule.py's tools
}

_SCHEDULE_KEYS = {"id", "at", "repeat", "text"}
_REPEATS = ("none", "daily", "weekly")


def _check_senses(value):
    if not isinstance(value, dict):
        raise ConfigError("senses must be an object of name -> options")
    for name, options in value.items():
        try:
            mod = importlib.import_module(f"pathfinder.senses.{name}")
        except ImportError:
            raise ConfigError(f"unknown sense: {name}")
        if not isinstance(options, dict):
            raise ConfigError(f"senses.{name} options must be an object")
        bad = frozenset(options) - frozenset(mod.OPTIONS)
        if bad:  # a typo here would otherwise silently run with the default - refuse at the door
            raise ConfigError(f"unknown option(s) for sense {name}: {sorted(bad)}")


def _check_schedules(value):
    if not isinstance(value, list):
        raise ConfigError("schedules must be a list")
    for row in value:
        if not isinstance(row, dict) or frozenset(row) != _SCHEDULE_KEYS:
            raise ConfigError(f"schedule row must have exactly {sorted(_SCHEDULE_KEYS)}: {row}")
        if row["repeat"] not in _REPEATS:
            raise ConfigError(f"schedule repeat must be one of {_REPEATS}: {row}")
        try:
            _, canonical = clock.parse_local(row["at"])
        except ValueError as e:
            raise ConfigError(f"schedule at: {e}")
        if canonical != row["at"]:
            raise ConfigError(f"schedule at must be the canonical local string: {row['at']!r} (want {canonical!r})")


def _check(key, value):
    """One validation for both doors: `config set` and a hand-edited config.json."""
    if key not in DEFAULTS:
        # a key nobody reads is how dead settings accumulate - refuse it at the source
        raise ConfigError(f"unknown config key: {key}")
    want = type(DEFAULTS[key])
    if type(value) is not want:  # `is not`: bool is an int, and True steps is not a budget
        raise ConfigError(f"{key} must be {want.__name__}, got {type(value).__name__}")
    if key in ("max_steps", "session_resume_s") and value < 1:
        # max_steps 0 runs the model zero times: no output, no error, a turn that "ends" silently
        raise ConfigError(f"{key} must be at least 1")
    if key == "grants":
        bad = {k: v for k, v in value.items() if v not in ("allow", "ask", "deny")}
        if bad:  # a typo here would otherwise read as "never ask" - fail closed at the door
            raise ConfigError(f"grants values must be allow, ask or deny: {bad}")
    if key == "senses":
        _check_senses(value)
    if key == "schedules":
        _check_schedules(value)


def home() -> Path:
    raw = os.environ.get("PATHFINDER_HOME")
    path = Path(raw) if raw else Path.home() / ".pathfinder"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)  # mkdir leaves an existing directory's mode alone; a pre-made world-readable home would expose the ledger
    return path


def load(home: Path) -> dict:
    cfg = dict(DEFAULTS)
    config_path = Path(home) / "config.json"
    if config_path.exists():
        overrides = json.loads(config_path.read_text())
        if not isinstance(overrides, dict):
            raise ConfigError("the config file must hold one JSON object")
        for key, value in overrides.items():
            _check(key, value)
        cfg.update(overrides)
    return cfg


def set(home: Path, key: str, value, ledger=None) -> dict:
    _check(key, value)
    cfg = load(home)
    cfg[key] = value
    config_path = Path(home) / "config.json"
    # atomic: write to a temp file in the same directory, then os.replace - a crash
    # mid-write can never leave config.json truncated or half-written
    fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, prefix=".config-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp_path, config_path)
    if ledger is not None:
        # record that a change happened, never the value - see config_set in DECISIONS/blueprint
        ledger.append("config_set", key=key)
    return cfg
