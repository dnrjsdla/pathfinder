import threading
import time
import unittest

from pathfinder.llm import slot as slot_mod
from pathfinder.llm.slot import slot


def _wait_until(pred, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


class SlotTest(unittest.TestCase):
    def test_slot_user_first(self):
        """A job holds the slot, a job is waiting, then a user asks: the user goes first,
        regardless of who asked earlier - D25's "the user first" is not FIFO order."""
        order = []
        release_job1 = threading.Event()

        def job1():
            with slot("job"):
                release_job1.wait(2)

        def job2():
            with slot("job"):
                order.append("job2")

        def user():
            with slot("user"):
                order.append("user")

        t1 = threading.Thread(target=job1)
        t1.start()
        self.assertTrue(_wait_until(lambda: slot_mod._holder == "job"))

        t2 = threading.Thread(target=job2)
        t2.start()
        time.sleep(0.05)  # let job2 actually block inside slot() before the user arrives

        t3 = threading.Thread(target=user)
        t3.start()
        self.assertTrue(_wait_until(lambda: slot_mod._users_waiting == 1))

        release_job1.set()
        t1.join(2)
        t2.join(2)
        t3.join(2)
        self.assertEqual(order, ["user", "job2"])

    def test_slot_released_between_steps(self):
        with slot("job"):
            self.assertEqual(slot_mod._holder, "job")
        self.assertIsNone(slot_mod._holder)

        with slot("user"):
            self.assertEqual(slot_mod._holder, "user")
        self.assertIsNone(slot_mod._holder)


if __name__ == "__main__":
    unittest.main()
