import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pilot107.api.asgi_app import build_asgi_app, openapi_contract_snapshot
from pilot107.api.http_app import build_api


class AsgiAppTests(unittest.TestCase):
    def test_openapi_contract_matches_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = build_asgi_app(
                build_api(
                    db_path=root / "pilot107.db",
                    evidence_root=root / "evidence",
                )
            )

        actual = openapi_contract_snapshot(app)
        expected_path = Path(__file__).parent / "snapshots" / "openapi_phase3b.json"
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertEqual(actual, expected)

    def test_compatibility_routes_do_not_leak_into_openapi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = build_asgi_app(
                build_api(
                    db_path=root / "pilot107.db",
                    evidence_root=root / "evidence",
                )
            )

        schema = app.openapi()
        self.assertNotIn("/{path}", schema["paths"])
        operation_ids = [
            operation["operationId"]
            for path_item in schema["paths"].values()
            for method, operation in path_item.items()
            if method in {"get", "post", "patch", "put", "delete"}
        ]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))

    def test_asgi_forwarder_preserves_auth_request_id_and_etag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = build_asgi_app(
                build_api(
                    db_path=root / "pilot107.db",
                    evidence_root=root / "evidence",
                    auth_required=True,
                )
            )

            unauthorized = _asgi_request(app, "/api/v1/platform/capabilities")
            first = _asgi_request(
                app,
                "/api/v1/platform/capabilities",
                headers={"x-pilot107-user": "alice", "x-request-id": "req_test"},
            )
            cached = _asgi_request(
                app,
                "/api/v1/platform/capabilities",
                headers={
                    "x-pilot107-user": "alice",
                    "if-none-match": first[1]["etag"],
                },
            )

        self.assertEqual(unauthorized[0], 401)
        self.assertEqual(json.loads(unauthorized[2])["error"]["code"], "AUTH.MISSING")
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["x-request-id"], "req_test")
        self.assertIn("etag", first[1])
        self.assertEqual(cached[0], 304)
        self.assertEqual(cached[2], b"")

    def test_hidden_post_compatibility_route_forwards_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = build_asgi_app(
                build_api(
                    db_path=root / "pilot107.db",
                    evidence_root=root / "evidence",
                )
            )

            response = _asgi_request(
                app,
                "/api/v1/contracts/validate",
                method="POST",
                body=b"{}",
                headers={"content-type": "application/json"},
            )

        self.assertEqual(response[0], 422)
        self.assertEqual(
            json.loads(response[2])["error"]["code"],
            "CONTRACT.RECIPE_REQUIRED",
        )


def _asgi_request(
    app,
    target: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    async def invoke() -> tuple[int, dict[str, str], bytes]:
        path, _, query = target.partition("?")
        sent_request = False
        messages: list[dict] = []

        async def receive() -> dict:
            nonlocal sent_request
            if sent_request:
                return {"type": "http.disconnect"}
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": query.encode("ascii"),
                "root_path": "",
                "headers": [
                    (key.lower().encode("ascii"), value.encode("latin-1"))
                    for key, value in (headers or {}).items()
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in start["headers"]
        }
        response_body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return start["status"], response_headers, response_body

    return asyncio.run(invoke())


if __name__ == "__main__":
    unittest.main()
