#!/usr/bin/env python3
"""End-to-end tus resumable-upload verification against the full deployed stack.

Targets the reverse-proxy (HTTPS) so the request path is:
    client -> reverse-proxy -> web BFF -> API (tus terminates here)

Covers: capability discovery, create/PATCH/HEAD/complete, resume-from-offset,
idempotent retried PATCH, 5-way parallel upload via concatenation, cancellation
(DELETE), sha256 mismatch rejection, and read-back of the written file.

Run on the VM:  python3 verify_tus_e2e.py [--large]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

HOST = "https://127.0.0.1:8443"
API = f"{HOST}/api/v1"
TUS = f"{API}/files/tus"
USER = "alice"  # BFF runs fixed_user=alice; header is informational
HOME = f"/public/home/{USER}"
PATCH_CT = "application/offset+octet-stream"
CTX = ssl._create_unverified_context()

_passed = 0


def ok(msg: str) -> None:
    global _passed
    _passed += 1
    print(f"  [ok] {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", file=sys.stderr)
    raise SystemExit(1)


def req(method: str, url: str, headers: dict | None = None, body: bytes | None = None):
    """Return (status, headers, body_bytes). Raises SystemExit on HTTP error."""
    r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=120, context=CTX) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"  [HTTP {exc.code}] {method} {url}\n    {detail}", file=sys.stderr)
        raise SystemExit(1)


def req_expect_error(method: str, url: str, headers: dict | None = None, body: bytes | None = None):
    """Like req but REQUIRE an HTTP error; return (status, error_json)."""
    r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=120, context=CTX) as resp:
            resp.read()
            fail(f"expected error but got {resp.status} for {method} {url}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            payload = {}
        return exc.code, payload


def b64meta(**fields: str) -> str:
    return ",".join(
        f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in fields.items()
    )


def tus_create(length: int | None, meta: dict | None = None, concat: str | None = None):
    # The final concatenation request must NOT carry Upload-Length (the server
    # derives it from the sum of the parts); pass length=None to omit it.
    headers = {"Tus-Resumable": "1.0.0"}
    if length is not None:
        headers["Upload-Length"] = str(length)
    if meta:
        headers["Upload-Metadata"] = b64meta(**meta)
    if concat:
        headers["Upload-Concat"] = concat
    status, hdrs, _ = req("POST", TUS, headers)  # no body -> no Content-Type
    if status != 201:
        fail(f"create status {status}")
    loc = hdrs.get("Location")
    if not loc:
        fail("create missing Location")
    return loc  # e.g. /api/v1/files/tus/<id>


def tus_patch(loc: str, offset: int, data: bytes) -> int:
    """PATCH bytes at offset; return the new Upload-Offset."""
    url = HOST + loc
    headers = {
        "Tus-Resumable": "1.0.0",
        "Content-Type": PATCH_CT,
        "Upload-Offset": str(offset),
    }
    status, hdrs, _ = req("PATCH", url, headers, data)
    if status != 204:
        fail(f"PATCH status {status}")
    return int(hdrs.get("Upload-Offset", "-1"))


def tus_head(loc: str) -> tuple[int, int]:
    status, hdrs, _ = req("HEAD", HOST + loc, {"Tus-Resumable": "1.0.0"})
    if status != 200:
        fail(f"HEAD status {status}")
    return int(hdrs.get("Upload-Offset", "-1")), int(hdrs.get("Upload-Length", "-1"))


def tus_delete(loc: str) -> None:
    status, _, _ = req("DELETE", HOST + loc, {"Tus-Resumable": "1.0.0"})
    if status != 204:
        fail(f"DELETE status {status}")


def complete(upload_id: str) -> dict:
    status, _, body = req(
        "POST",
        f"{API}/files/uploads/{upload_id}/complete",
        {"Content-Type": "application/json"},
        b"{}",
    )
    if status != 200:
        fail(f"complete status {status}")
    return json.loads(body)


def upload_id_of(loc: str) -> str:
    return loc.rstrip("/").rsplit("/", 1)[-1]


def read_file(path: str, length: int) -> tuple[bytes, int]:
    length = min(length, 2097152)  # server caps read length at 2 MiB
    status, _, body = req(
        "GET", f"{API}/files/content?path={path}&offset=0&length={length}"
    )
    payload = json.loads(body)
    return base64.b64decode(payload["data_b64"]), payload["size"]


def post_json(path: str, payload: dict) -> dict:
    status, _, body = req(
        "POST", f"{API}{path}", {"Content-Type": "application/json"},
        json.dumps(payload).encode(),
    )
    return json.loads(body)


# ---------------------------------------------------------------------------


def test_options() -> None:
    print("[test] tus capability discovery (OPTIONS)")
    status, hdrs, _ = req("OPTIONS", TUS, {"Tus-Resumable": "1.0.0"})
    if status != 204 or hdrs.get("Tus-Version") != "1.0.0":
        fail(f"OPTIONS status={status} Tus-Version={hdrs.get('Tus-Version')}")
    ext = hdrs.get("Tus-Extension", "")
    for needed in ("creation", "termination", "concatenation"):
        if needed not in ext:
            fail(f"OPTIONS missing extension {needed}: {ext}")
    ok(f"OPTIONS 204, extensions={ext}, max-size={hdrs.get('Tus-Max-Size')}")


def test_simple_upload(test_dir: str) -> None:
    print("[test] simple upload: create -> PATCH x2 (HEAD resume probe) -> complete")
    payload = os.urandom(3 * 1024 * 1024 + 12345)  # ~3 MiB, non-aligned
    sha = hashlib.sha256(payload).hexdigest()
    loc = tus_create(len(payload), {"filename": "simple.bin", "target_path": test_dir, "sha256": sha})
    half = len(payload) // 2
    off = tus_patch(loc, 0, payload[:half])
    if off != half:
        fail(f"first PATCH offset {off} != {half}")
    # resume probe
    hoff, hlen = tus_head(loc)
    if (hoff, hlen) != (half, len(payload)):
        fail(f"HEAD ({hoff},{hlen}) != ({half},{len(payload)})")
    off = tus_patch(loc, half, payload[half:])
    if off != len(payload):
        fail(f"final PATCH offset {off} != {len(payload)}")
    done = complete(upload_id_of(loc))
    if done["state"] not in ("written", "extracted"):
        fail(f"state {done['state']}")
    if done.get("sha256_actual") != sha:
        fail(f"sha256_actual {done.get('sha256_actual')} != {sha}")
    data, size = read_file(f"{test_dir}/simple.bin", 4 * 1024 * 1024)
    if size != len(payload) or data != payload[: len(data)]:
        fail("read-back mismatch")
    ok(f"simple upload {len(payload)} B, sha256 verified, read-back OK")


def test_resume_and_idempotent(test_dir: str) -> None:
    print("[test] resume from offset + idempotent retried PATCH")
    payload = os.urandom(2 * 1024 * 1024)
    sha = hashlib.sha256(payload).hexdigest()
    loc = tus_create(len(payload), {"filename": "resume.bin", "target_path": test_dir, "sha256": sha})
    third = len(payload) // 3
    tus_patch(loc, 0, payload[:third])
    # simulate interruption: probe then re-send an OVERLAPPING chunk (retry)
    hoff, _ = tus_head(loc)
    if hoff != third:
        fail(f"HEAD offset {hoff} != {third}")
    # retried PATCH re-sends from an earlier offset (overlap) -> truncate-then-write
    off = tus_patch(loc, third - 1000, payload[third - 1000: 2 * third])
    if off != 2 * third:
        fail(f"overlap PATCH offset {off} != {2 * third}")
    off = tus_patch(loc, 2 * third, payload[2 * third:])
    if off != len(payload):
        fail(f"final offset {off}")
    done = complete(upload_id_of(loc))
    if done.get("sha256_actual") != sha:
        fail(f"sha256 mismatch after overlap retry: {done.get('sha256_actual')}")
    ok("resume + overlapping retry produced intact file (sha256 OK)")


def test_parallel_concat(test_dir: str, parts: int = 5, part_size: int = 8 * 1024 * 1024) -> None:
    print(f"[test] parallel upload: {parts} partials x {part_size // (1024*1024)} MiB -> concatenation")
    blobs = [os.urandom(part_size) for _ in range(parts)]
    whole = b"".join(blobs)
    sha = hashlib.sha256(whole).hexdigest()
    total = len(whole)

    # create partials
    locs = [tus_create(part_size, concat="partial") for _ in range(parts)]

    # PATCH partials in parallel threads
    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            blob = blobs[i]
            off = tus_patch(locs[i], 0, blob[: part_size // 2])
            off = tus_patch(locs[i], off, blob[part_size // 2:])
            if off != part_size:
                errors.append(f"partial {i} final offset {off}")
        except SystemExit:
            errors.append(f"partial {i} failed")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(parts)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        fail("; ".join(errors))

    # concatenate (tus-js-client sends "final;<urls>")
    concat_header = "final;" + " ".join(HOST + loc for loc in locs)
    final_loc = tus_create(
        None,
        {"filename": "parallel.bin", "target_path": test_dir, "sha256": sha},
        concat=concat_header,
    )
    done = complete(upload_id_of(final_loc))
    elapsed = time.time() - t0
    if done["state"] not in ("written", "extracted"):
        fail(f"concat state {done['state']}")
    if done.get("sha256_actual") != sha:
        fail(f"concat sha256 {done.get('sha256_actual')} != {sha}")
    data, size = read_file(f"{test_dir}/parallel.bin", part_size * parts)
    if size != total or data != whole[: len(data)]:
        fail("concat read-back mismatch")
    ok(f"parallel {parts}x{part_size // (1024*1024)}MiB concat, sha256 OK, {elapsed:.1f}s")


def test_cancel(test_dir: str) -> None:
    print("[test] cancellation: DELETE terminates session + purges staging")
    payload = os.urandom(1024 * 1024)
    loc = tus_create(len(payload), {"filename": "cancel.bin", "target_path": test_dir})
    tus_patch(loc, 0, payload[: 512 * 1024])
    tus_delete(loc)
    # subsequent PATCH must fail (session aborted)
    status, payload_err = req_expect_error(
        "PATCH", HOST + loc,
        {"Tus-Resumable": "1.0.0", "Content-Type": PATCH_CT, "Upload-Offset": str(512 * 1024)},
        payload[512 * 1024:],
    )
    code = payload_err.get("error", {}).get("code", "")
    if status not in (400, 404, 409, 410):
        fail(f"PATCH after DELETE status {status}")
    ok(f"DELETE then PATCH rejected ({status} {code})")


def test_sha256_mismatch(test_dir: str) -> None:
    print("[test] sha256 mismatch rejected at complete")
    payload = os.urandom(512 * 1024)
    wrong = hashlib.sha256(b"not the payload").hexdigest()
    loc = tus_create(len(payload), {"filename": "bad.bin", "target_path": test_dir, "sha256": wrong})
    tus_patch(loc, 0, payload)
    status, body = req_expect_error(
        "POST", f"{API}/files/uploads/{upload_id_of(loc)}/complete",
        {"Content-Type": "application/json"}, b"{}",
    )
    code = body.get("error", {}).get("code", "")
    if status != 409 or "SHA256" not in code.upper():
        fail(f"mismatch complete status={status} code={code}")
    # clean up the failed session
    tus_delete(loc)
    ok(f"sha256 mismatch -> {status} {code}")


def test_large_parallel(test_dir: str, total: int = 2 * 1024 * 1024 * 1024, parts: int = 5) -> None:
    print(f"[test] LARGE parallel upload: {total / (1024**3):.1f} GiB across {parts} partials")
    chunk = 8 * 1024 * 1024
    base_size = total // parts
    sizes = [base_size + (total - base_size * parts) if i == parts - 1 else base_size for i in range(parts)]
    # deterministic per-partial data: random 1MiB seed block repeated
    seeds = [os.urandom(1024 * 1024) for _ in range(parts)]

    def gen(i: int, start: int, end: int) -> bytes:
        seed = seeds[i]
        out = bytearray()
        pos = start
        while pos < end:
            block_off = pos % len(seed)
            take = min(len(seed) - block_off, end - pos)
            out += seed[block_off: block_off + take]
            pos += take
        return bytes(out)

    # whole-file sha256 in concat order
    hasher = hashlib.sha256()
    for i in range(parts):
        for off in range(0, sizes[i], chunk):
            hasher.update(gen(i, off, min(off + chunk, sizes[i])))
    sha = hasher.hexdigest()

    locs = [tus_create(sizes[i], concat="partial") for i in range(parts)]
    errors: list[str] = []
    t0 = time.time()

    def worker(i: int) -> None:
        try:
            off = 0
            while off < sizes[i]:
                data = gen(i, off, min(off + chunk, sizes[i]))
                off = tus_patch(locs[i], off, data)
        except SystemExit:
            errors.append(f"partial {i} failed")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(parts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        fail("; ".join(errors))
    patch_elapsed = time.time() - t0

    concat_header = "final;" + " ".join(HOST + loc for loc in locs)
    final_loc = tus_create(
        None, {"filename": "large.bin", "target_path": test_dir, "sha256": sha},
        concat=concat_header,
    )
    done = complete(upload_id_of(final_loc))
    elapsed = time.time() - t0
    if done["state"] not in ("written", "extracted"):
        fail(f"large state {done['state']}")
    if done.get("sha256_actual") != sha:
        fail(f"large sha256 {done.get('sha256_actual')} != {sha}")
    rate = total / patch_elapsed / (1024 * 1024)
    ok(f"LARGE {total / (1024**3):.1f} GiB, PATCH {patch_elapsed:.1f}s ({rate:.1f} MiB/s), total {elapsed:.1f}s, sha256 OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--large", action="store_true", help="run the 2 GiB parallel upload test")
    parser.add_argument("--large-size", type=int, default=2 * 1024 * 1024 * 1024)
    args = parser.parse_args()

    test_dir = f"{HOME}/tus-e2e"
    post_json("/files/mkdir", {"path": test_dir})
    print(f"test dir: {test_dir}")

    test_options()
    test_simple_upload(test_dir)
    test_resume_and_idempotent(test_dir)
    test_parallel_concat(test_dir)
    test_cancel(test_dir)
    test_sha256_mismatch(test_dir)
    if args.large:
        test_large_parallel(test_dir, total=args.large_size)

    # cleanup
    post_json("/files/delete", {"path": test_dir})
    print(f"\nALL {_passed} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
