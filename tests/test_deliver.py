"""Outbox unit tests - no daemon, no socket (daemon-integration delivery tests live in
tests/test_daemon.py's DeliverTest)."""
import unittest

from pathfinder.deliver import Notice, Outbox
from pathfinder.rules import Rules


class FakeState:
    def presence(self):
        return None


class ReleaseOrderingTest(unittest.TestCase):
    def test_release_skips_held_keeps_order(self):
        """Note 10: a notice held for its own session must not block a different session's
        releasable notice behind it - release() used to stop the whole FIFO at the first held
        entry."""
        running = {"A": True, "B": False}
        sent = []
        ob = Outbox(FakeState(), Rules(idle_threshold_s=20), is_running=lambda s: running[s],
                   attached=lambda s: s == "B", send=lambda s, m: sent.append((s, m["text"])),
                   record=lambda t, **f: None, banner=lambda text: True)
        ob.queue = [Notice("n1", "A", "for A (held)", "w", []), Notice("n2", "B", "for B (free)", "w", [])]
        ob.release()
        self.assertEqual(sent, [("B", "for B (free)")])
        self.assertEqual([n.id for n in ob.queue], ["n1"])  # A's notice stays queued, in place

    def test_release_then_running_session_frees_up(self):
        running = {"A": True}
        ob = Outbox(FakeState(), Rules(), is_running=lambda s: running[s], attached=lambda s: False,
                   send=lambda s, m: None, record=lambda t, **f: None, banner=lambda text: True)
        ob.queue = [Notice("n1", "A", "text", "w", [])]
        ob.release()
        self.assertEqual(len(ob.queue), 1)  # still held
        running["A"] = False
        ob.release()
        self.assertEqual(ob.queue, [])  # released once the session frees up


if __name__ == "__main__":
    unittest.main()
