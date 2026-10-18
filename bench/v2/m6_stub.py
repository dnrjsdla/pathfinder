"""m6_stub.py -- shared by m6_day.py and m8_fold.py (split for the 200-line cap, as m2_stub): the temp home wired
as the daemon wires a clone (config through `config.set`, ledger first then state), the key lookup, the ledger's
request usage, and the dry run's rule-based fake router (no network, no key). Stdlib only.
"""
import json, os, subprocess, sys, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # this tree's pathfinder, whichever interpreter runs the bench
from pathfinder import config, daemon, ledger, state  # noqa: E402 - daemon: its imports register the product's tools
from pathfinder.clone import Clone  # noqa: E402
from pathfinder.llm import Response  # noqa: E402
from pathfinder.router import Router  # noqa: E402

USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def key() -> None:
    """ANTHROPIC_API_KEY, else the Keychain item `llm.resolve` reads, put in the env every bench provider names."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        found = subprocess.run(["security", "find-generic-password", "-s", "pathfinder-anthropic", "-a", "api", "-w"],
                               capture_output=True, text=True, timeout=10)
        os.environ["ANTHROPIC_API_KEY"] = found.stdout.strip()
    if not os.environ["ANTHROPIC_API_KEY"]:
        sys.exit("no Anthropic key: set ANTHROPIC_API_KEY or Keychain service pathfinder-anthropic, account api")


def configure(path: Path, refs: dict, effort: str) -> None:
    """`models` = one ref per role; every provider a ref names is api anthropic at `effort` ("" = API default)."""
    providers = {ref.partition(":")[0]: {"api": "anthropic", "base_url": "https://api.anthropic.com",
                                         "api_key_env": "ANTHROPIC_API_KEY", **({"effort": effort} if effort else {})}
                 for ref in refs.values()}
    config.set(path, "providers", providers)
    config.set(path, "models", {role: [ref] for role, ref in refs.items()})


class Home:
    """A PATHFINDER_HOME under a temp dir: the bench's tools (the workspace) resolve here, never the real home."""

    def __init__(self, path: Path, router=None):
        self.path = Path(path)
        os.environ["PATHFINDER_HOME"] = str(self.path)
        self.ledger, self.state = ledger.Ledger(self.path), state.State(self.path / "state.db")
        self.router = router or Router(lambda: config.load(self.path))
        self.queue = []  # the lane: items a clone enqueues (gate reports, late steers)

    def record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        return event

    def clone(self, id, role, resident) -> Clone:
        c = Clone(id, role, home=self.path, state=self.state, record=self.record, router=self.router,
                  send=lambda session, msg: None, enqueue=self.queue.append, resident=resident)
        c.restore()
        return c

    def events(self, after=0) -> list[dict]:
        return list(self.ledger.read(after))


def requests(events) -> list[list[int]]:
    """[input, output, cache_read, cache_creation] of every model request the events record (a step's
    `assistant`, a fold's `summary`; an interrupted or failed step that carries no usage is not one)."""
    return [[e["usage"].get(k, 0) for k in USAGE] for e in events
            if e["type"] in ("assistant", "summary") and e.get("usage")]


def text(t):
    return {"type": "text", "text": t}


def use(name, **args):
    return {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:16]}", "name": name, "input": args}


def said(req) -> str:
    """Every text block of the request's user messages, joined (where the dry run's rules look)."""
    return "\n".join(b["text"] for m in req.messages if m["role"] == "user" for b in m["content"] if b["type"] == "text")


def answered(req) -> bool:
    """True when the last user message carries a tool_result: the model has had its tool round."""
    last = next(m for m in reversed(req.messages) if m["role"] == "user")
    return any(b["type"] == "tool_result" for b in last["content"])


class FakeRouter:
    """The dry run's model. A fold gets a summary; a step gets `rule(req)`'s blocks. Usage mimics a warm cache in
    chars / 4: a clone's request reads its previous request's prompt and writes the rest."""

    def __init__(self, refs: dict, rule):
        self.refs, self.rule, self.last, self.count = refs, rule, {}, 0

    async def complete(self, req, on_text=None, cancel=None):
        self.count += 1
        size = len(json.dumps([req.system, req.tools, req.messages], ensure_ascii=False, default=str)) // 4
        who = req.clone or req.role
        read = min(self.last.get(who, 0), size)
        self.last[who] = size
        blocks = [text("Folded: the user asked about pages; ids are in the handles.")] if req.role == "summarize" \
            else self.rule(req)
        if on_text:
            on_text("".join(b["text"] for b in blocks if b["type"] == "text"))
        usage = dict(zip(USAGE, (3, 40, read, size - read)))
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return Response(blocks, stop, usage, self.refs.get(req.role, ""), [])


def day_rule(req):
    """M6's dry-run step: read the file a fresh item names (write it when the item says 저장), else answer."""
    last = next(m for m in reversed(req.messages) if m["role"] == "user")
    asked = " ".join(b["text"] for b in last["content"] if b["type"] == "text")
    paths = [w.strip("'\".,?") for w in asked.split() if w.startswith(("notion/", "notes/"))]
    if answered(req) or not paths or asked.startswith("gate "):
        return [text("Done: here is what I found. " + "Details follow, line by line, as a real answer would. " * 10)]
    return [use("write_file", path=paths[0], content="fake note") if paths[0].startswith("notes/") and "저장" in asked
            else use("read_file", path=paths[0])]
