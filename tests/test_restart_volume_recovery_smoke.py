from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_restart_volume_recovery.py"
SPEC = importlib.util.spec_from_file_location("smoke_restart_volume_recovery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke_restart_volume_recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke_restart_volume_recovery)


def test_restart_recovery_paths_do_not_collide_between_invocations() -> None:
    first = smoke_restart_volume_recovery._new_recovery_paths()
    second = smoke_restart_volume_recovery._new_recovery_paths()

    assert set(first).isdisjoint(second)
    for pre_path, post_path in (first, second):
        assert pre_path.endswith("/pre/result.txt")
        assert post_path.endswith("/post/result.txt")
        assert pre_path.split("/", 1)[0] == post_path.split("/", 1)[0]
