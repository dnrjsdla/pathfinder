"""One model request at a time, the user first (D25). m7a measured that the free plan queues a
second overlapping request server-side (3/3, no 429/402): the slot is not error insurance, it is
an ordering device - without it, a job's next step requested a moment before a user's turn would
go first in the server's own FIFO queue. A job may hold the slot for one whole step (one
streaming request); it yields between steps, so a user turn waits at most one job step."""
import threading
from contextlib import contextmanager

from . import stream

_cond = threading.Condition()
_holder = None       # None | "user" | "job"
_users_waiting = 0    # users blocked on the current holder - a job's own wait checks this


@contextmanager
def slot(priority: str):
    """One streaming request at a time. A `user` caller waits only for the current holder; a
    `job` caller also waits while any user is waiting or holding, so a user's turn never queues
    behind a job that asked first."""
    global _holder, _users_waiting
    with _cond:
        if priority == "user":
            while _holder is not None:
                _users_waiting += 1
                try:
                    _cond.wait()
                finally:
                    _users_waiting -= 1
        else:
            while _holder is not None or _users_waiting > 0:
                _cond.wait()
        _holder = priority
    try:
        yield
    finally:
        with _cond:
            _holder = None
            _cond.notify_all()


def stream_step(endpoint, schemas, control, priority):
    """A `step(messages)` for `loop.run`: `llm.stream` on `endpoint` (picks the adapter by
    `endpoint.api`), wrapped in this module's slot at `priority` for the whole request - shared
    by the daemon's own step factory (priority "user") and a job's (priority "job")."""
    def step(messages):
        with slot(priority):
            yield from stream(endpoint, messages, tools=schemas, cancel=control)
    return step
