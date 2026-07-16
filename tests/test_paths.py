import unittest
from pathlib import Path

from pilot107.core.paths import PathPolicyError, authorize_path


class PathPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_authorize_path_inside_root(self) -> None:
        root = self.tmp_path / "public" / "home" / "alice"
        root.mkdir(parents=True)
        file_path = root / "out.txt"
        file_path.write_text("ok")

        safe = authorize_path(str(file_path), [root])

        self.assertEqual(safe.resolved, file_path.resolve())
        self.assertEqual(safe.root, root.resolve())

    def test_reject_path_outside_root(self) -> None:
        alice = self.tmp_path / "public" / "home" / "alice"
        bob = self.tmp_path / "public" / "home" / "bob"
        alice.mkdir(parents=True)
        bob.mkdir(parents=True)
        secret = bob / "secret.txt"
        secret.write_text("no")

        with self.assertRaises(PathPolicyError):
            authorize_path(str(secret), [alice])

    def test_reject_symlink_escape(self) -> None:
        alice = self.tmp_path / "public" / "home" / "alice"
        bob = self.tmp_path / "public" / "home" / "bob"
        alice.mkdir(parents=True)
        bob.mkdir(parents=True)
        secret = bob / "secret.txt"
        secret.write_text("no")
        link = alice / "link"
        link.symlink_to(secret)

        with self.assertRaises(PathPolicyError):
            authorize_path(str(link), [alice])


if __name__ == "__main__":
    unittest.main()
