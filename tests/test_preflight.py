"""Tests for ``pilot107.core.preflight`` (WorkDirPreflight, Lane 4b-i)."""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from pathlib import Path

from pilot107.core.preflight import (
    LocalPathChecker,
    preflight_workdir,
    preflight_workdir_fs,
    preflight_workdir_paths,
)
from pilot107.core.resources import PreflightFinding, PreflightSeverity

SHARED_ROOTS: Sequence[str] = ("/public",)
LOCAL_ROOTS: Sequence[str] = ("/tmp", "/usr", "/var", "/opt")
ALLOWED_ROOTS: Sequence[str] = ("/public/home/alice",)


def _codes(findings: Sequence[PreflightFinding]) -> list[str]:
    return [f.code for f in findings]


def _has_block(findings: Sequence[PreflightFinding], code: str) -> bool:
    return any(f.code == code and f.severity == PreflightSeverity.BLOCK for f in findings)


class _FakeChecker:
    """In-memory PathChecker: decides based on path string membership."""

    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        dirs: set[str] | None = None,
        readable: set[str] | None = None,
        executable: set[str] | None = None,
        writable: set[str] | None = None,
    ) -> None:
        self._existing = existing or set()
        self._dirs = dirs or set()
        self._readable = readable or set()
        self._executable = executable or set()
        self._writable = writable or set()

    def _key(self, path: str | Path) -> str:
        return str(path)

    def exists(self, path: str | Path) -> bool:
        return self._key(path) in self._existing

    def is_dir(self, path: str | Path) -> bool:
        return self._key(path) in self._dirs

    def readable(self, path: str | Path) -> bool:
        return self._key(path) in self._readable

    def executable(self, path: str | Path) -> bool:
        return self._key(path) in self._executable

    def writable(self, path: str | Path) -> bool:
        return self._key(path) in self._writable


# --------------------------------------------------------------------------- #
# Pure-path variant
# --------------------------------------------------------------------------- #


class PreflightWorkdirPathsTests(unittest.TestCase):
    def test_shared_workdir_passes(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/public/home/alice/run-1",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertEqual(findings, [])

    def test_non_absolute_workdir_blocks(self) -> None:
        findings = preflight_workdir_paths(
            workdir="relative/run-1",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_NOT_ABSOLUTE"))
        # Early return: no further checks.
        self.assertEqual(len(findings), 1)

    def test_tmp_workdir_blocks(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/tmp/alice-run",
            allowed_roots=("/tmp", *ALLOWED_ROOTS),
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_LOCAL_TMP"))
        self.assertEqual(len(findings), 1)

    def test_tmp_workdir_blocks_even_if_listed_as_allowed(self) -> None:
        # Matrix #4: /tmp is rejected regardless of allowed_roots misconfig.
        findings = preflight_workdir_paths(
            workdir="/tmp",
            allowed_roots=("/tmp",),
            shared_roots=("/tmp",),  # nonsense, but must still block
            local_roots=(),
        )
        self.assertTrue(_has_block(findings, "WORKDIR_LOCAL_TMP"))

    def test_local_only_root_blocks_not_shared(self) -> None:
        # /usr is in local_roots and also (perversely) allowed; must BLOCK
        # because compute nodes will not see it.
        findings = preflight_workdir_paths(
            workdir="/usr/local/alice",
            allowed_roots=("/usr/local/alice",),
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_NOT_SHARED"))

    def test_path_outside_allowed_roots_blocks(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/public/home/bob/run-1",
            allowed_roots=ALLOWED_ROOTS,  # only alice
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_NOT_ALLOWED"))
        self.assertEqual(len(findings), 1)

    def test_windows_drive_path_blocks(self) -> None:
        findings = preflight_workdir_paths(
            workdir="C:\\Users\\alice\\run-1",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        # Windows drive is caught by the local-path detector before the
        # absolute check (Path("C:\\...") on POSIX is not absolute).
        self.assertTrue(
            _has_block(findings, "WORKDIR_LOCAL_PATH")
            or _has_block(findings, "WORKDIR_NOT_ABSOLUTE")
        )

    def test_macos_users_path_blocks(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/Users/alice/run-1",
            allowed_roots=("/Users/alice",),
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_LOCAL_PATH"))

    def test_home_path_not_under_shared_blocks(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/home/alice/run-1",
            allowed_roots=("/home/alice",),
            shared_roots=SHARED_ROOTS,  # /public only
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_LOCAL_PATH"))

    def test_home_path_under_shared_passes(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/home/alice/run-1",
            allowed_roots=("/home/alice",),
            shared_roots=("/home",),
            local_roots=LOCAL_ROOTS,
        )
        self.assertEqual(findings, [])

    def test_allowed_but_unclassified_root_warns(self) -> None:
        # /scratch is allowed and not in shared or local — ambiguous.
        findings = preflight_workdir_paths(
            workdir="/scratch/alice/run-1",
            allowed_roots=("/scratch/alice",),
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(
            any(
                f.code == "WORKDIR_SHARED_UNKNOWN" and f.severity == PreflightSeverity.WARN
                for f in findings
            )
        )

    def test_output_parent_tmp_blocks(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/public/home/alice/run-1",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            output_parent="/tmp/alice-out",
        )
        self.assertTrue(_has_block(findings, "WORKDIR_OUTPUT_LOCAL_TMP"))

    def test_output_parent_outside_allowed_blocks(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/public/home/alice/run-1",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            output_parent="/public/home/bob/out",
        )
        self.assertTrue(_has_block(findings, "WORKDIR_OUTPUT_NOT_ALLOWED"))

    def test_output_parent_shared_passes(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/public/home/alice/run-1",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            output_parent="/public/home/alice/out",
        )
        self.assertEqual(findings, [])

    def test_default_entry_alias_matches_paths_variant(self) -> None:
        a = preflight_workdir_paths(
            workdir="/tmp/x",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        b = preflight_workdir(
            workdir="/tmp/x",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertEqual(a, b)


# --------------------------------------------------------------------------- #
# FS-backed variant
# --------------------------------------------------------------------------- #


class PreflightWorkdirFsTests(unittest.TestCase):
    def test_existing_readable_executable_workdir_passes(self) -> None:
        workdir = "/public/home/alice/run-1"
        out = "/public/home/alice/out"
        checker = _FakeChecker(
            existing={workdir, out, "/public", "/public/home/alice"},
            dirs={workdir, out, "/public", "/public/home/alice"},
            readable={workdir},
            executable={workdir},
            writable={out},
        )
        findings = preflight_workdir_fs(
            workdir=workdir,
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            path_checker=checker,
            output_parent=out,
        )
        self.assertEqual(findings, [])

    def test_unwritable_output_parent_blocks(self) -> None:
        workdir = "/public/home/alice/run-1"
        out = "/public/home/alice/out"
        checker = _FakeChecker(
            existing={workdir, out},
            dirs={workdir, out},
            readable={workdir},
            executable={workdir},
            writable={workdir},  # output not writable
        )
        findings = preflight_workdir_fs(
            workdir=workdir,
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            path_checker=checker,
            output_parent=out,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_OUTPUT_NOT_WRITABLE"))

    def test_missing_workdir_with_writable_parent_warns(self) -> None:
        workdir = "/public/home/alice/run-1"
        parent = "/public/home/alice"
        checker = _FakeChecker(
            existing={parent},
            dirs={parent},
            writable={parent},
        )
        findings = preflight_workdir_fs(
            workdir=workdir,
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            path_checker=checker,
        )
        self.assertTrue(
            any(
                f.code == "WORKDIR_WILL_BE_CREATED" and f.severity == PreflightSeverity.WARN
                for f in findings
            )
        )

    def test_missing_workdir_with_missing_parent_blocks(self) -> None:
        workdir = "/public/home/alice/deep/run-1"
        checker = _FakeChecker(existing=set(), dirs=set())
        findings = preflight_workdir_fs(
            workdir=workdir,
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            path_checker=checker,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_PARENT_NOT_FOUND"))

    def test_non_readable_workdir_blocks(self) -> None:
        workdir = "/public/home/alice/run-1"
        checker = _FakeChecker(
            existing={workdir},
            dirs={workdir},
            readable=set(),  # not readable
            executable={workdir},
        )
        findings = preflight_workdir_fs(
            workdir=workdir,
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            path_checker=checker,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_NOT_READABLE"))

    def test_non_executable_workdir_blocks(self) -> None:
        workdir = "/public/home/alice/run-1"
        checker = _FakeChecker(
            existing={workdir},
            dirs={workdir},
            readable={workdir},
            executable=set(),  # cannot enter
        )
        findings = preflight_workdir_fs(
            workdir=workdir,
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            path_checker=checker,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_NOT_EXECUTABLE"))

    def test_tmp_workdir_skips_fs_checks(self) -> None:
        # /tmp is BLOCKed by the pure variant; FS checks should be skipped.
        workdir = "/tmp/alice"
        checker = _FakeChecker(
            existing={workdir},
            dirs={workdir},
            readable={workdir},
            executable={workdir},
        )
        findings = preflight_workdir_fs(
            workdir=workdir,
            allowed_roots=("/tmp",),
            shared_roots=("/tmp",),
            local_roots=LOCAL_ROOTS,
            path_checker=checker,
        )
        self.assertTrue(_has_block(findings, "WORKDIR_LOCAL_TMP"))
        self.assertFalse(_has_block(findings, "WORKDIR_NOT_READABLE"))

    def test_missing_output_with_writable_parent_warns(self) -> None:
        workdir = "/public/home/alice/run-1"
        out = "/public/home/alice/out"
        parent = "/public/home/alice"
        checker = _FakeChecker(
            existing={workdir, parent},
            dirs={workdir, parent},
            readable={workdir},
            executable={workdir},
            writable={parent},
        )
        findings = preflight_workdir_fs(
            workdir=workdir,
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
            path_checker=checker,
            output_parent=out,
        )
        self.assertTrue(
            any(
                f.code == "WORKDIR_OUTPUT_WILL_BE_CREATED" and f.severity == PreflightSeverity.WARN
                for f in findings
            )
        )

    def test_local_path_checker_is_a_path_checker(self) -> None:
        # Smoke: LocalPathChecker satisfies the PathChecker protocol.
        checker: LocalPathChecker = LocalPathChecker()
        # Use the current process cwd which must exist.
        self.assertTrue(checker.exists(Path.cwd()))
        self.assertTrue(checker.is_dir(Path.cwd()))


class PreflightFindingShapeTests(unittest.TestCase):
    def test_findings_use_resources_preflight_finding(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/tmp/x",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(all(isinstance(f, PreflightFinding) for f in findings))
        # Every finding carries severity + code + message + source_authority.
        for f in findings:
            self.assertIsInstance(f.severity, PreflightSeverity)
            self.assertIsInstance(f.code, str)
            self.assertIsInstance(f.message, str)
            self.assertEqual(f.source_authority, "submission_strategy.md#4")

    def test_codes_are_preflight_namespaced(self) -> None:
        findings = preflight_workdir_paths(
            workdir="/tmp/x",
            allowed_roots=ALLOWED_ROOTS,
            shared_roots=SHARED_ROOTS,
            local_roots=LOCAL_ROOTS,
        )
        self.assertTrue(all(f.code.startswith("WORKDIR_") for f in findings))


if __name__ == "__main__":
    unittest.main()
