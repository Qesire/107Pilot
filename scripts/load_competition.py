from __future__ import annotations

import argparse
import json
import ssl
import statistics
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

HEADERS = {"Content-Type": "application/json", "X-Pilot107-User": "alice"}


@dataclass(frozen=True)
class RequestResult:
    ok: bool
    seconds: float
    status: int | None
    error: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run 107Pilot competition profile load checks.")
    parser.add_argument("--base-url", default="https://127.0.0.1:8443")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--scenario",
        choices=["read", "validate", "prepare", "workflow", "all"],
        default="all",
    )
    parser.add_argument("--workflow-timeout", type=float, default=180.0)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    ssl_context = ssl._create_unverified_context() if base_url.startswith("https://") else None
    scenarios: list[tuple[str, Callable[[int], RequestResult]]] = []
    if args.scenario in {"read", "all"}:
        scenarios.append(
            ("read", lambda index: _read_scenario(base_url, ssl_context, args.timeout, index))
        )
    if args.scenario in {"validate", "all"}:
        scenarios.append(
            (
                "validate",
                lambda index: _validate_scenario(base_url, ssl_context, args.timeout, index),
            )
        )
    if args.scenario in {"prepare", "all"}:
        scenarios.append(
            ("prepare", lambda index: _prepare_scenario(base_url, ssl_context, args.timeout, index))
        )
    if args.scenario == "workflow":
        scenarios.append(
            (
                "workflow",
                lambda index: _workflow_scenario(
                    base_url,
                    ssl_context,
                    args.timeout,
                    args.workflow_timeout,
                    index,
                ),
            )
        )

    exit_code = 0
    for name, scenario in scenarios:
        summary = _run_scenario(name=name, concurrency=args.concurrency, scenario=scenario)
        print(_format_summary(summary))
        if summary["errors"] > 0:
            exit_code = 1
    return exit_code


def _run_scenario(
    *,
    name: str,
    concurrency: int,
    scenario: Callable[[int], RequestResult],
) -> dict[str, Any]:
    started = time.perf_counter()
    results: list[RequestResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(scenario, index) for index in range(concurrency)]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started
    latencies = [result.seconds for result in results]
    errors = [result for result in results if not result.ok]
    return {
        "scenario": name,
        "concurrency": concurrency,
        "elapsed_seconds": elapsed,
        "ok": len(results) - len(errors),
        "errors": len(errors),
        "rps": len(results) / elapsed if elapsed > 0 else 0.0,
        "latency_min": min(latencies) if latencies else 0.0,
        "latency_p50": statistics.median(latencies) if latencies else 0.0,
        "latency_p95": _percentile(latencies, 0.95),
        "latency_max": max(latencies) if latencies else 0.0,
        "first_error": None if not errors else errors[0].error,
    }


def _read_scenario(
    base_url: str, ssl_context: ssl.SSLContext | None, timeout: float, index: int
) -> RequestResult:
    if index % 3 == 0:
        return _request("GET", f"{base_url}/healthz", None, ssl_context, timeout)
    if index % 3 == 1:
        return _request("GET", f"{base_url}/api/v1/recipes", None, ssl_context, timeout)
    return _request("GET", f"{base_url}/", None, ssl_context, timeout)


def _validate_scenario(
    base_url: str, ssl_context: ssl.SSLContext | None, timeout: float, index: int
) -> RequestResult:
    return _request(
        "POST",
        f"{base_url}/api/v1/contracts/validate",
        _contract_payload(index),
        ssl_context,
        timeout,
    )


def _prepare_scenario(
    base_url: str, ssl_context: ssl.SSLContext | None, timeout: float, index: int
) -> RequestResult:
    contract = _request_json(
        "POST",
        f"{base_url}/api/v1/contracts",
        _contract_payload(index),
        ssl_context,
        timeout,
    )
    if isinstance(contract, RequestResult):
        return contract
    contract_id = contract.get("contract_id")
    if not isinstance(contract_id, str):
        return RequestResult(False, 0.0, None, "contract response missing contract_id")
    return _request(
        "POST",
        f"{base_url}/api/v1/runs/prepare",
        {"contract_id": contract_id},
        ssl_context,
        timeout,
    )


def _workflow_scenario(
    base_url: str,
    ssl_context: ssl.SSLContext | None,
    timeout: float,
    workflow_timeout: float,
    index: int,
) -> RequestResult:
    started = time.perf_counter()
    contract = _request_json(
        "POST",
        f"{base_url}/api/v1/contracts",
        _contract_payload(index),
        ssl_context,
        timeout,
    )
    if isinstance(contract, RequestResult):
        return contract
    contract_id = contract.get("contract_id")
    if not isinstance(contract_id, str):
        return RequestResult(
            False, time.perf_counter() - started, None, "contract response missing contract_id"
        )

    prepared = _request_json(
        "POST",
        f"{base_url}/api/v1/runs/prepare",
        {"contract_id": contract_id},
        ssl_context,
        timeout,
    )
    if isinstance(prepared, RequestResult):
        return prepared
    run_id = prepared.get("run_id")
    if not isinstance(run_id, str):
        return RequestResult(
            False, time.perf_counter() - started, None, "prepare response missing run_id"
        )

    submitted = _request_json(
        "POST",
        f"{base_url}/api/v1/runs/{run_id}/submit",
        {},
        ssl_context,
        timeout,
    )
    if isinstance(submitted, RequestResult):
        return submitted
    if str(submitted.get("job_id", "")).startswith("demo-"):
        return RequestResult(
            False, time.perf_counter() - started, None, "workflow used demo backend"
        )

    deadline = time.perf_counter() + workflow_timeout
    last: dict[str, Any] = {}
    while time.perf_counter() < deadline:
        current = _request_json(
            "GET", f"{base_url}/api/v1/runs/{run_id}", None, ssl_context, timeout
        )
        if isinstance(current, RequestResult):
            return current
        last = current
        if current.get("state") == "SUCCEEDED" and current.get("collection_state") == "succeeded":
            capsule = _request_json(
                "POST",
                f"{base_url}/api/v1/runs/{run_id}/capsule",
                {},
                ssl_context,
                timeout,
            )
            if isinstance(capsule, RequestResult):
                return capsule
            if capsule.get("capsule_state") != "ready":
                return RequestResult(
                    False,
                    time.perf_counter() - started,
                    200,
                    f"capsule not ready: {capsule}",
                )
            if not capsule.get("capsule", {}).get("manifest_sha256"):
                return RequestResult(
                    False,
                    time.perf_counter() - started,
                    200,
                    f"capsule missing manifest sha256: {capsule}",
                )
            return RequestResult(True, time.perf_counter() - started, 200)
        if current.get("state") in {"FAILED", "CANCELLED"}:
            return RequestResult(
                False, time.perf_counter() - started, 200, f"unexpected terminal run: {current}"
            )
        time.sleep(1.0)
    return RequestResult(False, time.perf_counter() - started, 200, f"workflow timeout: {last}")


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    ssl_context: ssl.SSLContext | None,
    timeout: float,
) -> dict[str, Any] | RequestResult:
    started = time.perf_counter()
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url=url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            body = response.read()
            parsed = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(parsed, dict):
                return RequestResult(
                    False, time.perf_counter() - started, response.status, "non-object JSON"
                )
            return parsed
    except urllib.error.HTTPError as exc:
        return RequestResult(
            False,
            time.perf_counter() - started,
            exc.code,
            exc.read().decode("utf-8", errors="replace"),
        )
    except Exception as exc:
        return RequestResult(False, time.perf_counter() - started, None, str(exc))


def _request(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    ssl_context: ssl.SSLContext | None,
    timeout: float,
) -> RequestResult:
    started = time.perf_counter()
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url=url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            response.read()
            return RequestResult(True, time.perf_counter() - started, response.status)
    except urllib.error.HTTPError as exc:
        return RequestResult(
            False,
            time.perf_counter() - started,
            exc.code,
            exc.read().decode("utf-8", errors="replace"),
        )
    except Exception as exc:
        return RequestResult(False, time.perf_counter() - started, None, str(exc))


def _contract_payload(index: int) -> dict[str, Any]:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {
            "command": (
                "hostname\n"
                f"echo load-{index}\n"
                "mkdir -p pilot107-load-output\n"
                f"echo {index} > pilot107-load-output/result-{index}.txt\n"
            ),
            "expected_outputs": [f"pilot107-load-output/result-{index}.txt"],
        },
        "resources": {
            "partition": "Students",
            "qos": "qos_stu_medium_2gpu",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def _format_summary(summary: dict[str, Any]) -> str:
    return (
        f"load {summary['scenario']} concurrency={summary['concurrency']} "
        f"ok={summary['ok']} errors={summary['errors']} "
        f"elapsed={summary['elapsed_seconds']:.3f}s rps={summary['rps']:.1f} "
        f"latency_ms=min:{summary['latency_min'] * 1000:.1f} "
        f"p50:{summary['latency_p50'] * 1000:.1f} "
        f"p95:{summary['latency_p95'] * 1000:.1f} "
        f"max:{summary['latency_max'] * 1000:.1f} "
        f"first_error={summary['first_error']}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
