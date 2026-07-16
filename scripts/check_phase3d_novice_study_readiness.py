from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from pilot107.core.novice_acceptance import evaluate_novice_study_readiness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check live Phase 3D facilitated-study prerequisites without mutation."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:3000/api/v1")
    parser.add_argument("--user", default="alice")
    parser.add_argument("--failure-run-id", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--insecure", action="store_true", help="Allow local self-signed TLS")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    context = ssl._create_unverified_context() if args.insecure else None
    try:
        session = _get(args, "/web/session", context)
        templates = _get(args, "/templates?gpu=false&limit=100", context)
        encoded_run_id = quote(args.failure_run_id, safe="")
        failure_run = _get(args, f"/runs/{encoded_run_id}", context)
        failure_evidence = _get(args, f"/runs/{encoded_run_id}/evidence", context)
        failure_diagnoses = _get(args, f"/runs/{encoded_run_id}/diagnoses", context)
    except (OSError, ValueError) as exc:
        print(f"novice study readiness request failed: {exc}", file=sys.stderr)
        return 1

    report = evaluate_novice_study_readiness(
        session=session,
        templates=templates,
        failure_run=failure_run,
        failure_evidence=failure_evidence,
        failure_diagnoses=failure_diagnoses,
    ).to_payload()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "ready" else 1


def _get(
    args: argparse.Namespace,
    path: str,
    context: ssl.SSLContext | None,
) -> dict:
    request = urllib.request.Request(
        url=f"{args.base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json", "X-Pilot107-User": args.user},
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=args.timeout,
            context=context,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OSError(f"GET {path} returned HTTP {exc.code}: {body[:500]}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"GET {path} returned non-object JSON")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
