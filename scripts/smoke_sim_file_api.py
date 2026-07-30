"""Smoke test for the visual filesystem API (chunked upload, list, read, archive, delete).

Requires a running simulator stack (``scripts/start-sim-core.sh``) with the
command-gateway file endpoints available.  Exercises the full lifecycle through
the BFF on port 3000.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import urllib.request

BASE_URL = "http://127.0.0.1:3000/api/v1"
USER = "alice"
HOME = f"/public/home/{USER}"


def main() -> int:
    # 1. mkdir
    test_dir = f"{HOME}/pilot107-file-smoke"
    _post("/files/mkdir", {"path": test_dir})
    print(f"[ok] mkdir {test_dir}")

    # 2. list dir – should contain the new directory entry in parent
    listing = _get(f"/files?path={HOME}")
    names = {entry["name"] for entry in listing.get("entries", [])}
    if "pilot107-file-smoke" not in names:
        print(f"[FAIL] mkdir dir not visible in listing: {names}", file=sys.stderr)
        return 1
    print(f"[ok] list {HOME} ({len(names)} entries)")

    # 3. chunked upload
    payload = b"hello 107pilot file smoke " * 100  # ~2.6 KiB
    sha256 = hashlib.sha256(payload).hexdigest()
    session = _post("/files/uploads", {
        "target_path": test_dir,
        "filename": "smoke.txt",
        "total_size": len(payload),
        "sha256": sha256,
        "chunk_size": 1024,
    })
    upload_id = session["upload_id"]
    total_chunks = session["total_chunks"]
    for i in range(total_chunks):
        start = i * 1024
        end = min(start + 1024, len(payload))
        chunk_b64 = base64.b64encode(payload[start:end]).decode()
        _post(f"/files/uploads/{upload_id}/chunks", {"index": i, "data_b64": chunk_b64})
    completed = _post(f"/files/uploads/{upload_id}/complete", {})
    if completed["state"] not in ("completed", "written"):
        print(f"[FAIL] upload not completed: {completed}", file=sys.stderr)
        return 1
    print(f"[ok] upload smoke.txt ({len(payload)} bytes, {total_chunks} chunks)")

    # 4. read content back
    content = _get(f"/files/content?path={test_dir}/smoke.txt&offset=0&length=2048")
    decoded = base64.b64decode(content["data_b64"])
    if decoded != payload[:2048]:
        print("[FAIL] content mismatch on read", file=sys.stderr)
        return 1
    if content["size"] != len(payload):
        print(f"[FAIL] size mismatch: {content['size']} != {len(payload)}", file=sys.stderr)
        return 1
    print(f"[ok] read content ({content['size']} bytes)")

    # 5. archive
    archive_resp = _post("/files/archive", {
        "paths": [f"{test_dir}/smoke.txt"],
        "dest_dir": test_dir,
        "archive_name": "smoke-bundle.tar.gz",
    })
    if archive_resp.get("status") != "ok":
        print(f"[FAIL] archive: {archive_resp}", file=sys.stderr)
        return 1
    print(f"[ok] archive -> {archive_resp['path']} ({archive_resp['size']} bytes)")

    # 6. verify archive visible in listing
    dir_listing = _get(f"/files?path={test_dir}")
    dir_names = {entry["name"] for entry in dir_listing.get("entries", [])}
    if "smoke-bundle.tar.gz" not in dir_names:
        print(f"[FAIL] archive not in listing: {dir_names}", file=sys.stderr)
        return 1
    print("[ok] archive visible in dir listing")

    # 7. delete the test tree
    _post("/files/delete", {"path": test_dir})
    after = _get(f"/files?path={HOME}")
    after_names = {entry["name"] for entry in after.get("entries", [])}
    if "pilot107-file-smoke" in after_names:
        print("[FAIL] delete did not remove test dir", file=sys.stderr)
        return 1
    print("[ok] delete test tree")

    print("file API smoke PASSED")
    return 0


def _get(path: str) -> dict:
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}",
        headers={"X-Pilot107-User": USER},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Pilot107-User": USER},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
