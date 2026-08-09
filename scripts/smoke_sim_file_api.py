"""Smoke test for the visual filesystem API (tus resumable upload, list, read, archive, delete).

Requires a running simulator stack (``scripts/start-sim-core.sh``) with the
command-gateway file endpoints available.  Exercises the full lifecycle through
the BFF on port 3000, driving the tus resumable-upload protocol end to end:
capability discovery (OPTIONS), creation with metadata, binary PATCH appends,
a HEAD resume probe, and explicit completion (sha256 verify + write to cluster).
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import urllib.error
import urllib.request
from http.client import HTTPResponse
from typing import Any, cast

BASE_URL = "http://127.0.0.1:3000/api/v1"
USER = "alice"
HOME = f"/public/home/{USER}"
TUS = "/files/tus"
_PATCH_CONTENT_TYPE = "application/offset+octet-stream"


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

    # 3. tus capability discovery
    options = _request("OPTIONS", TUS, headers={"X-Pilot107-User": USER})
    if options.headers.get("Tus-Version") != "1.0.0":
        print(f"[FAIL] OPTIONS missing Tus-Version: {dict(options.headers)}", file=sys.stderr)
        return 1
    print(f"[ok] tus OPTIONS (extensions: {options.headers.get('Tus-Extension')})")

    # 4. tus resumable upload (create -> PATCH x2 with HEAD resume probe -> complete)
    payload = b"hello 107pilot file smoke " * 100  # ~2.6 KiB
    sha256 = hashlib.sha256(payload).hexdigest()

    created = _request(
        "POST",
        TUS,
        headers={
            "X-Pilot107-User": USER,
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(len(payload)),
            "Upload-Metadata": _tus_metadata(
                filename="smoke.txt", target_path=test_dir, sha256=sha256
            ),
        },
    )
    if created.status != 201:
        print(f"[FAIL] tus create status {created.status}", file=sys.stderr)
        return 1
    location = created.headers.get("Location")
    if not location:
        print("[FAIL] tus create missing Location header", file=sys.stderr)
        return 1
    upload_id = location.rsplit("/", 1)[-1]
    upload_url = f"{BASE_URL}{TUS}/{upload_id}"
    print(f"[ok] tus create ({len(payload)} bytes) -> {location}")

    # Append the first half, then prove the resume probe reports the offset.
    half = len(payload) // 2
    _tus_patch(upload_url, 0, payload[:half])
    head = _request("HEAD", TUS + f"/{upload_id}", headers={"X-Pilot107-User": USER})
    if head.headers.get("Upload-Offset") != str(half):
        print(
            f"[FAIL] HEAD offset {head.headers.get('Upload-Offset')} != {half}",
            file=sys.stderr,
        )
        return 1
    if head.headers.get("Upload-Length") != str(len(payload)):
        print(f"[FAIL] HEAD length {head.headers.get('Upload-Length')}", file=sys.stderr)
        return 1
    print(f"[ok] tus PATCH first half + HEAD resume probe (offset={half})")

    final = _tus_patch(upload_url, half, payload[half:])
    if final.headers.get("Upload-Offset") != str(len(payload)):
        print(
            f"[FAIL] final offset {final.headers.get('Upload-Offset')} != {len(payload)}",
            file=sys.stderr,
        )
        return 1
    print(f"[ok] tus PATCH second half (offset={len(payload)})")

    completed = _post(f"/files/uploads/{upload_id}/complete", {})
    if completed["state"] not in ("completed", "written", "extracted"):
        print(f"[FAIL] upload not completed: {completed}", file=sys.stderr)
        return 1
    if completed.get("sha256_actual") != sha256:
        print(f"[FAIL] sha256 mismatch: {completed.get('sha256_actual')}", file=sys.stderr)
        return 1
    print(f"[ok] tus complete (state={completed['state']}, sha256 verified)")

    # 5. read content back
    content = _get(f"/files/content?path={test_dir}/smoke.txt&offset=0&length=2048")
    decoded = base64.b64decode(content["data_b64"])
    if decoded != payload[:2048]:
        print("[FAIL] content mismatch on read", file=sys.stderr)
        return 1
    if content["size"] != len(payload):
        print(f"[FAIL] size mismatch: {content['size']} != {len(payload)}", file=sys.stderr)
        return 1
    print(f"[ok] read content ({content['size']} bytes)")

    # 6. archive
    archive_resp = _post("/files/archive", {
        "paths": [f"{test_dir}/smoke.txt"],
        "dest_dir": test_dir,
        "archive_name": "smoke-bundle.tar.gz",
    })
    if archive_resp.get("status") != "ok":
        print(f"[FAIL] archive: {archive_resp}", file=sys.stderr)
        return 1
    print(f"[ok] archive -> {archive_resp['path']} ({archive_resp['size']} bytes)")

    # 7. verify archive visible in listing
    dir_listing = _get(f"/files?path={test_dir}")
    dir_names = {entry["name"] for entry in dir_listing.get("entries", [])}
    if "smoke-bundle.tar.gz" not in dir_names:
        print(f"[FAIL] archive not in listing: {dir_names}", file=sys.stderr)
        return 1
    print("[ok] archive visible in dir listing")

    # 8. delete the test tree
    _post("/files/delete", {"path": test_dir})
    after = _get(f"/files?path={HOME}")
    after_names = {entry["name"] for entry in after.get("entries", [])}
    if "pilot107-file-smoke" in after_names:
        print("[FAIL] delete did not remove test dir", file=sys.stderr)
        return 1
    print("[ok] delete test tree")

    print("file API smoke PASSED")
    return 0


def _tus_metadata(**fields: str) -> str:
    return ",".join(
        f"{key} {base64.b64encode(value.encode('utf-8')).decode('ascii')}"
        for key, value in fields.items()
    )


def _tus_patch(upload_url: str, offset: int, data: bytes) -> HTTPResponse:
    return _request(
        "PATCH",
        upload_url,
        absolute=True,
        headers={
            "X-Pilot107-User": USER,
            "Tus-Resumable": "1.0.0",
            "Content-Type": _PATCH_CONTENT_TYPE,
            "Upload-Offset": str(offset),
        },
        body=data,
    )


def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    absolute: bool = False,
) -> HTTPResponse:
    url = path if absolute else f"{BASE_URL}{path}"
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        return cast(HTTPResponse, urllib.request.urlopen(request, timeout=20))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"[FAIL] {method} {url} -> {exc.code}: {detail}", file=sys.stderr)
        raise


def _get(path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}",
        headers={"X-Pilot107-User": USER},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))


def _post(path: str, payload: dict[str, object]) -> dict[str, Any]:
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Pilot107-User": USER},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))


if __name__ == "__main__":
    raise SystemExit(main())
