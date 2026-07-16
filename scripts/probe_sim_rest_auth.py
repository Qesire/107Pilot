"""Simulator REST auth probe — mints a REAL ``scontrol token`` JWT.

Lane 1 made slurmrestd enforce ``rest_auth/jwt``. The previous probe sent
``token="dev-token"`` which real JWT auth correctly rejects, so it always
reported ``blocked``. This rework:

* mints a real JWT via ``docker exec pilot107-sim-slurmctld-1 scontrol token``
  (as the target user, so the JWT ``sun`` matches ``X-SLURM-USER-NAME``);
* targets ``v0.0.41`` (Slurm 25.11 simulator target) — configurable
  via ``PILOT107_SIM_REST_API_VERSION``;
* tests no-token (expect 401) and real-token via ``SLURM_HEADERS`` on
  ``GET /slurm/v0.0.41/nodes`` (expect 200);
* reports ``supported`` when the real-token GET returns 200.

The token is NEVER written to the output JSON, logs, or error messages.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Allow importing the sibling helper module when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sim_rest_helpers import (  # noqa: E402
    DEFAULT_API_VERSION,
    DEFAULT_REST_USER,
    detect_sim_rest_url,
    mint_sim_token,
)

from pilot107.adapters.slurm import RestAuthStyle, SlurmTransportError, UrllibHttpTransport
from pilot107.core.rest_semantics import check_slurm_rest_semantics
from pilot107.core.run_store import utc_now_iso


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    output_path = root / "artifacts" / "probes" / "sim_rest_auth.json"
    base_url = detect_sim_rest_url()
    api_version = DEFAULT_API_VERSION
    rest_user = DEFAULT_REST_USER

    probes: list[dict[str, Any]] = []
    probes.append(
        _probe_http(
            name="no_token",
            base_url=base_url,
            api_version=api_version,
            token=None,
            auth_style=RestAuthStyle.SLURM_HEADERS,
            slurm_username=rest_user,
            expected_status=401,
        )
    )

    token_mint: dict[str, Any]
    real_token: str | None
    try:
        real_token = mint_sim_token(user=rest_user)
        token_mint = {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 — probe must surface any mint failure
        token_mint = {"status": "failed", "error": str(exc)}
        real_token = None

    if real_token is not None:
        probes.append(
            _probe_http(
                name="slurm_headers_real_token_nodes",
                base_url=base_url,
                api_version=api_version,
                token=real_token,
                auth_style=RestAuthStyle.SLURM_HEADERS,
                slurm_username=rest_user,
                expected_status=200,
            )
        )

    payload = {
        "observed_at": utc_now_iso(),
        "target": base_url,
        "api_version": api_version,
        "rest_user": rest_user,
        "slurmrestd_container": "pilot107-sim-slurmrestd-1",
        "slurmctld_container": "pilot107-sim-slurmctld-1",
        "token_mint": token_mint,
        "plugins": _plugin_inventory(),
        "probes": probes,
        "summary": _summary(probes, token_mint),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("sim rest auth probe " + payload["summary"]["status"])
    print("artifact=" + str(output_path))
    return 0 if payload["summary"]["status"] == "supported" else 1


def _probe_http(
    *,
    name: str,
    base_url: str,
    api_version: str,
    token: str | None,
    auth_style: RestAuthStyle,
    slurm_username: str | None,
    expected_status: int | None,
) -> dict[str, Any]:
    transport = UrllibHttpTransport(
        base_url=base_url,
        timeout_seconds=5.0,
        auth_style=auth_style,
        slurm_username=slurm_username,
    )
    # Use /nodes (not /ping) so a 200 confirms real read access with the
    # minted JWT, exactly matching Lane 1's manual verification.
    path = f"/slurm/{api_version}/nodes"
    try:
        response = transport.request("GET", path, token=token)
    except SlurmTransportError as exc:
        return {
            "name": name,
            "path": path,
            "auth_style": auth_style.value,
            "expected_status": expected_status,
            "supported": False,
            "status": "transport_error",
            "error": str(exc),
        }
    semantic = check_slurm_rest_semantics(response.payload, required_fields=[])
    supported = response.status < 400 and not semantic.errors
    return {
        "name": name,
        "path": path,
        "auth_style": auth_style.value,
        "expected_status": expected_status,
        "supported": supported,
        "status": "supported" if supported else "blocked",
        "http_status": response.status,
        "semantic_level": semantic.level.value,
        "errors": semantic.errors,
        "warnings": semantic.warnings,
        "node_count": _node_count(response.payload),
        # NOTE: payload is intentionally omitted — it can be large and the
        # probe only needs the status/counts to decide support.
    }


def _node_count(payload: dict[str, Any]) -> int | None:
    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        return len(nodes)
    return None


def _plugin_inventory() -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "pilot107-sim-login-node-sim-1",
            "bash",
            "-lc",
            "find /usr/local/lib/slurm /usr/lib/x86_64-linux-gnu/slurm-wlm "
            "\\( -name '*jwt*' -o -name 'auth_*' \\) 2>/dev/null | sort",
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    plugins = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    plugin_names = {Path(path).name for path in plugins}
    return {
        "command_returncode": result.returncode,
        "has_rest_auth_jwt": "rest_auth_jwt.so" in plugin_names,
        "has_auth_jwt": "auth_jwt.so" in plugin_names,
        "plugins": plugins,
        "stderr": result.stderr.strip(),
    }


def _summary(probes: list[dict[str, Any]], token_mint: dict[str, Any]) -> dict[str, Any]:
    supported = [probe["name"] for probe in probes if probe.get("supported")]
    status = "supported" if supported else "blocked"
    reason: str | None
    if supported:
        reason = None
    elif token_mint.get("status") != "ok":
        reason = "scontrol token mint failed; cannot probe real JWT auth"
    else:
        reason = (
            "simulator slurmrestd rejected real JWT; "
            "check rest_auth/jwt plugin and AuthAltTypes=auth/jwt in slurm.conf"
        )
    return {
        "status": status,
        "supported_probes": supported,
        "blocked_reason": reason,
    }


if __name__ == "__main__":
    raise SystemExit(main())
