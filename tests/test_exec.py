import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import exec as pexec, tools
from pathfinder.loop import TurnControl


class ExecTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(os.environ, {"PATHFINDER_HOME": self._tmp.name})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    @unittest.skipUnless(pexec.available(), "needs sandbox-exec")
    def test_exec_blocks_write_outside_workspace(self):
        target = Path.home() / f"pathfinder-exec-test-{os.getpid()}.txt"
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        result = pexec.run_command(f"echo hi > {target}")
        self.assertFalse(target.exists())
        self.assertNotEqual(result["exit_code"], 0)

    @unittest.skipUnless(pexec.available(), "needs sandbox-exec")
    def test_exec_denies_secret_read(self):
        result = pexec.run_command("ls -la ~/Library/Keychains")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("hint", result)

    def test_exec_interrupt_kills_process_group(self):
        """Runs everywhere (no sandbox needed): a direct Popen via escalate=True,
        proving a `sleep 30` child is gone within 1 s of control.interrupt."""
        control = TurnControl()
        threading.Thread(target=lambda: (time.sleep(0.1), control.interrupt.set())).start()

        start = time.monotonic()
        result = pexec.run_command("sleep 30", escalate=True, control=control, timeout=10)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 1.0)
        self.assertTrue(result.get("killed"))

    @unittest.skipUnless(pexec.available(), "needs sandbox-exec")
    def test_exec_interrupt_kills_sandboxed_process_tree(self):
        """The sandboxed tree is sandbox-exec -> bash -> children: the group kill must reach
        a backgrounded grandchild too, and communicate() must return promptly."""
        control = TurnControl()
        threading.Thread(target=lambda: (time.sleep(0.2), control.interrupt.set())).start()
        start = time.monotonic()
        result = pexec.run_command("sleep 31 & sleep 30", control=control, timeout=10)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertTrue(result.get("killed"))
        time.sleep(0.2)
        import subprocess
        self.assertEqual(subprocess.run(["pgrep", "-f", "sleep 31"], capture_output=True).returncode, 1)

    def test_exec_timeout(self):
        start = time.monotonic()
        result = pexec.run_command("sleep 5", escalate=True, timeout=1)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3)
        self.assertTrue(result.get("killed"))
        self.assertNotEqual(result["exit_code"], 0)

    def test_read_file_refuses_secret_paths(self):
        with self.assertRaises(ValueError):
            tools._read_file(str(Path.home() / ".ssh" / "id_rsa"))
        with self.assertRaises(ValueError):
            tools._read_file(str(Path.home() / ".env"))
        with self.assertRaises(ValueError):
            tools._read_file(str(Path.home() / ".env.local"))

    def test_run_command_ok_on_nonzero_exit(self):
        """A tool that runs and fails is ok=true - the exit code IS the answer."""
        result = pexec.run_command("grep nothing-to-find-here /dev/null", escalate=True)
        self.assertEqual(result["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
