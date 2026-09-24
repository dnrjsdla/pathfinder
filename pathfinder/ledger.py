"""The one ledger. append() is the only function in the codebase that writes
under events/; partition() is the only place that turns a timestamp into a
file name (two modules disagreeing on "the day" once lost 24 days of history).
append() is also safe across processes: an flock on events/.lock serializes
the daemon and any CLI process (e.g. `pathfinder config set`) that append
while the daemon is running."""
import fcntl
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

TYPES = frozenset({
    "session_start", "user", "assistant", "tool_call", "tool_result",
    "turn_start", "turn_end", "error", "config_set",
    "approval_pending", "approval", "interrupted",
    "signal", "decision", "judgment", "delivery", "reaction", "presence",
    "job_start", "job_done", "job_failed",
    "memory_pass", "memory_apply", "session_summary",
    "pin_accept", "pin_revoke",
    "install_step", "install_done", "install_failed", "uninstalled",
})
ORIGINS = frozenset({"user", "signal", "job", "system", "memory"})
TRUST = frozenset({"user", "self", "third_party"})
RESERVED = frozenset({"seq", "ts", "type", "session", "turn", "origin", "trust"})


def partition(ts: float) -> str:
    """UTC calendar date for a timestamp - the only place a time becomes a file name."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class Ledger:
    def __init__(self, root: Path):
        self.dir = Path(root) / "events"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()  # guards this instance's threads
        self._lock_path = self.dir / ".lock"
        self._lock_path.touch(exist_ok=True)
        self.last_seq, self._known_tail = self._recover_last_seq()

    def _files(self):
        return sorted(self.dir.glob("*.jsonl"))  # YYYY-MM-DD names sort chronologically

    def _recover_last_seq(self):
        """The last good seq on disk. Returns (last_seq, tail) where tail = (filename, size)
        of the newest partition, so append() can tell a sibling process wrote since.

        Walks partitions newest-first until one yields a seq. Trusting only the newest file
        is fatal: a crash on the first append after UTC midnight leaves it empty or holding
        one truncated line, last_seq restarts at 0, new events reuse old seqs, and the state
        drops them all as "already applied" - silently, with every test green."""
        files = self._files()
        if not files:
            return 0, (None, 0)
        tail = (files[-1].name, files[-1].stat().st_size)
        for path in reversed(files):
            last = 0
            for line in path.read_text().splitlines():
                try:
                    last = json.loads(line)["seq"]
                except (ValueError, KeyError, TypeError):
                    continue  # truncated line from a crash mid-write - not fatal, just skipped
            if last:
                return last, tail
        return 0, tail

    def append(self, type, *, session="", turn="", origin="system", trust="self", **data) -> dict:
        if type not in TYPES:
            raise ValueError(f"unknown event type: {type}")
        if origin not in ORIGINS:
            raise ValueError(f"unknown origin: {origin}")
        if trust not in TRUST:
            raise ValueError(f"unknown trust: {trust}")
        collide = RESERVED & data.keys()
        if collide:
            raise ValueError(f"data keys collide with reserved fields: {sorted(collide)}")
        with self._thread_lock, self._lock_path.open("r+") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)  # cross-process: serializes daemon + CLI appends
            try:
                # a sibling process (e.g. the CLI) may have appended since we last checked -
                # re-derive last_seq from disk whenever the newest file's identity/size moved
                files = self._files()
                tail = (files[-1].name, files[-1].stat().st_size) if files else (None, 0)
                if tail != self._known_tail:
                    self.last_seq, self._known_tail = self._recover_last_seq()
                ts = time.time()
                self.last_seq += 1
                event = {
                    "seq": self.last_seq, "ts": ts, "type": type, "session": session,
                    "turn": turn, "origin": origin, "trust": trust, **data,
                }
                path = self.dir / f"{partition(ts)}.jsonl"
                line = json.dumps(event) + "\n"
                # A crash mid-write leaves a fragment without a newline. Appending straight
                # after it would glue this event onto the fragment and lose BOTH on read.
                if path.exists() and path.stat().st_size:
                    with path.open("rb") as f:
                        f.seek(-1, 2)
                        if f.read(1) != b"\n":
                            line = "\n" + line
                with path.open("a") as f:
                    f.write(line)
                    f.flush()
                    # ponytail: no fsync here - a crash can still lose the last buffered line;
                    # add fsync if a real crash-loss is observed
                self._known_tail = (path.name, path.stat().st_size)
                return event
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

    def read(self, after: int = 0):
        for path in self._files():
            for line in path.read_text().splitlines():
                try:
                    event = json.loads(line)
                    seq = event["seq"]
                except (ValueError, KeyError, TypeError):
                    continue  # truncated line - skipped, not fatal
                if seq > after:
                    yield event
