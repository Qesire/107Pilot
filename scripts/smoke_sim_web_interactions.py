from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE_URL = "http://127.0.0.1:3000/api/v1"


def main() -> int:
    recipes = _get("/recipes")
    if not any(
        item.get("recipe_id") == "recipe_python_cpu" for item in recipes.get("items", [])
    ):
        print(f"unexpected recipes: {recipes}", file=sys.stderr)
        return 1

    validated = _post("/contracts/validate", _contract())
    if validated["status"] != "OK":
        print(f"unexpected validation: {validated}", file=sys.stderr)
        return 1

    contract = _post("/contracts", _contract())
    prepared = _post("/runs/prepare", {"contract_id": contract["contract_id"]})
    submitted = _post(f"/runs/{prepared['run_id']}/submit", {})
    run = submitted
    for _ in range(40):
        run = _get(f"/runs/{prepared['run_id']}")
        if run["state"] == "SUCCEEDED" and run["collection_state"] == "succeeded":
            break
        time.sleep(1)

    evidence = _get(f"/runs/{prepared['run_id']}/evidence")
    object_paths = {item["logical_path"] for item in evidence["objects"]}
    expected = {
        "submission/slurm_submit_response.json",
        "slurm/accounting.json",
        "logs/stdout.tail.json",
        "logs/stderr.tail.json",
        "environment/summary.json",
        "outputs/inventory.json",
        "derived/result_summary.v1.json",
    }
    if (
        run["state"] != "SUCCEEDED"
        or run["collection_state"] != "succeeded"
        or not expected.issubset(object_paths)
    ):
        print(
            "unexpected web interaction result: "
            f"run={run}; missing={sorted(expected - object_paths)}; evidence={evidence}",
            file=sys.stderr,
        )
        return 1
    if run.get("publication", {}).get("status") != "eligible":
        print(f"successful Run was not publication-eligible: {run}", file=sys.stderr)
        return 1

    # The simulator is only the Slurm transport underneath this flow.  The
    # publication service receives a normal succeeded Run and must not branch
    # on simulator provenance.
    publication = _post(
        f"/runs/{prepared['run_id']}/publish",
        {
            "request_key": f"sim-web-publish-{prepared['run_id']}",
            "title": "Docker Slurm successful Run smoke",
            "description": "A completed simulator Run shared through the normal market path.",
            "visibility": "campus",
            "tags": ["simulator", "smoke"],
            "reproduction_note": "Adopt into a private Contract and verify its own workdir.",
            "confirm_share": True,
        },
    )
    market = _get_as(
        "/market/items?q=Docker%20Slurm&kind=run_publication",
        user="bob",
    )
    if not any(
        item.get("item_id") == publication.get("publication_id")
        and item.get("kind") == "run_publication"
        and "workdir" not in item
        and "script" not in item
        and "contract_payload" not in item
        for item in market.get("items", [])
    ):
        print(f"successful Run market item was missing or unsafe: {market}", file=sys.stderr)
        return 1
    adoption = _post_as(
        f"/market/items/{publication['publication_id']}/adopt",
        {"request_key": f"sim-web-adopt-{prepared['run_id']}"},
        user="bob",
    )
    if not adoption.get("target_contract_id"):
        print(f"successful Run adoption did not create a Contract: {adoption}", file=sys.stderr)
        return 1

    print(
        "web interactions smoke "
        f"contract={contract['contract_id']} run={run['run_id']} job={run['job_id']} "
        f"state={run['state']} collection={run['collection_state']} objects={len(object_paths)} "
        f"publication={publication['publication_id']} "
        f"adopted_contract={adoption['target_contract_id']}"
    )
    return 0


def _contract() -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {
            "command": (
                "hostname\n"
                "echo web-interaction-ok\n"
                "mkdir -p pilot107-web-output\n"
                "echo ok > pilot107-web-output/result.txt"
            )
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


def _get(path: str) -> dict:
    return _get_as(path, user="alice")


def _get_as(path: str, *, user: str) -> dict:
    request = urllib.request.Request(url=f"{BASE_URL}{path}", headers={"X-Pilot107-User": user})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    return _post_as(path, payload, user="alice")


def _post_as(path: str, payload: dict, *, user: str) -> dict:
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Pilot107-User": user},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
