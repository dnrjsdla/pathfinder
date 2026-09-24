import unittest
from unittest import mock

from pathfinder import keychain


class KeychainTest(unittest.TestCase):
    def test_secret_found(self):
        with mock.patch("pathfinder.keychain.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="s3cr3t\n")) as run:
            self.assertEqual(keychain.secret("pathfinder-mcp-gh", "bot"), "s3cr3t")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["security", "find-generic-password", "-s", "pathfinder-mcp-gh", "-a", "bot", "-w"])

    def test_secret_missing_returns_none(self):
        with mock.patch("pathfinder.keychain.subprocess.run",
                        return_value=mock.Mock(returncode=44, stdout="")):
            self.assertIsNone(keychain.secret("svc", "acct"))

    def test_no_security_binary_returns_none(self):
        with mock.patch("pathfinder.keychain.subprocess.run", side_effect=FileNotFoundError()):
            self.assertIsNone(keychain.secret("svc", "acct"))


if __name__ == "__main__":
    unittest.main()
