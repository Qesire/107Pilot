#!/usr/bin/env bash
set -euo pipefail

if (( $# != 1 )); then
  echo "usage: $0 IMAGE_REF" >&2
  exit 2
fi

image="$1"

docker run --rm -i \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --security-opt seccomp=unconfined \
  --security-opt apparmor=bwrap \
  --security-opt systempaths=unconfined \
  --user 10700:10700 \
  "$image" python3 - <<'PY'
from pathlib import Path

from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspaceSnapshot


root = Path("/tmp/workspace")
root.mkdir()
workspace = AgentWorkspaceRecord(
    workspace_id="workspace-image-smoke",
    project_id="project-image-smoke",
    owner="alice",
    local_root=str(root),
    snapshot=WorkspaceSnapshot(
        source_ref="/public/home/alice",
        digest="a" * 64,
        entries=(),
        captured_at="2026-08-25T00:00:00Z",
    ),
    created_at="2026-08-25T00:00:00Z",
    updated_at="2026-08-25T00:00:00Z",
)
result = SandboxExecutor().execute(
    workspace,
    argv=("python", "-c", "import socket; print('sandbox-ok')"),
    timeout=5,
)
assert result.status == "succeeded", result
assert result.stdout == "sandbox-ok\n", result
PY

echo "sandbox-image=PASS image=$image"
