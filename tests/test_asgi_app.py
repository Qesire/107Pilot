import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pilot107.api.asgi_app import build_asgi_app, openapi_contract_snapshot
from pilot107.api.http_app import build_api
from pilot107.api.security import FixedWindowRateLimiter
from pilot107.worker.telemetry import WorkerTelemetryStore


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

    def test_agent_events_resume_through_asgi_last_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = build_api(
                db_path=root / "pilot107.db",
                evidence_root=root / "evidence",
                auth_required=True,
            )
            app = build_asgi_app(api)
            headers = {"x-pilot107-user": "alice", "content-type": "application/json"}
            created = _asgi_request(
                app,
                "/api/v1/agent-sessions",
                method="POST",
                body=json.dumps(
                    {
                        "request_key": "asgi-session",
                        "model_profile_id": "faux-default",
                        "source": {"run_id": "run-1"},
                    }
                ).encode(),
                headers=headers,
            )
            session = json.loads(created[2])
            turn_response = _asgi_request(
                app,
                f"/api/v1/agent-sessions/{session['session_id']}/turns",
                method="POST",
                body=json.dumps(
                    {
                        "request_key": "asgi-turn",
                        "message": "inspect run-1",
                        "expected_state_version": session["state_version"],
                    }
                ).encode(),
                headers=headers,
            )
            turn = json.loads(turn_response[2])
            assert api.agent_session_routes is not None
            store = api.agent_session_routes.service.store
            claim = store.claim_turn(turn["turn_id"], worker_id="worker-1", lease_seconds=30)
            assert claim is not None
            for sequence in (1, 2):
                store.append_event(
                    turn["turn_id"],
                    claim=claim,
                    sequence=sequence,
                    event_type="message_delta",
                    payload={"delta": str(sequence)},
                )

            first = _asgi_request(
                app,
                f"/api/v1/agent-sessions/{session['session_id']}/events?limit=1",
                headers={"x-pilot107-user": "alice"},
            )
            first_payload = json.loads(first[2])
            resumed = _asgi_request(
                app,
                f"/api/v1/agent-sessions/{session['session_id']}/events",
                headers={
                    "x-pilot107-user": "alice",
                    "last-event-id": str(first_payload["page"]["last_event_id"]),
                },
            )

        self.assertEqual(created[0], 201)
        self.assertEqual(turn_response[0], 202)
        self.assertEqual(
            [item["sequence"] for item in json.loads(resumed[2])["items"]],
            [2],
        )

    def test_transport_enforces_body_response_and_rate_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = build_api(
                db_path=root / "pilot107.db",
                evidence_root=root / "evidence",
            )
            api.max_request_body_bytes = 3
            api.rate_limiter = FixedWindowRateLimiter(limit=1, window_seconds=60)
            app = build_asgi_app(api)

            oversized = _asgi_request(
                app,
                "/api/v1/contracts/validate",
                method="POST",
                body=b"1234",
            )
            first = _asgi_request(app, "/api/v1/recipes")
            limited = _asgi_request(app, "/api/v1/recipes")
            health = _asgi_request(app, "/api/v1/health/live")

        self.assertEqual(oversized[0], 413)
        self.assertEqual(json.loads(oversized[2])["error"]["code"], "HTTP.REQUEST_TOO_LARGE")
        # The rejected POST consumed the first request in this process-local window.
        self.assertEqual(first[0], 429)
        self.assertEqual(limited[0], 429)
        self.assertIn("retry-after", limited[1])
        self.assertEqual(health[0], 200)
        self.assertEqual(health[1]["x-content-type-options"], "nosniff")

    def test_transport_fails_closed_when_response_exceeds_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = build_api(
                db_path=root / "pilot107.db",
                evidence_root=root / "evidence",
            )
            api.max_response_body_bytes = 16
            app = build_asgi_app(api)
            response = _asgi_request(app, "/api/v1/health/live")

        self.assertEqual(response[0], 500)
        self.assertEqual(json.loads(response[2])["error"]["code"], "HTTP.RESPONSE_TOO_LARGE")

    def test_prometheus_metrics_cover_normalized_api_outbox_and_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = build_api(
                db_path=root / "pilot107.db",
                evidence_root=root / "evidence",
            )
            api.control_repository.enqueue(
                message_id="metrics-agent",
                topic="agent.execute",
                aggregate_id="session_metrics",
                payload={"session_id": "session_metrics"},
            )
            api.metrics.observe_llm_call(
                provider="local",
                model="test-model",
                outcome="success",
                duration_seconds=0.25,
                input_tokens=42,
                output_tokens=8,
            )
            api.metrics.observe_agent_tool(
                profile="experiment_builder",
                tool="builder_build_submit",
                outcome="no_progress",
            )
            api.metrics.observe_builder_submission(
                outcome="scheduled",
                phase="validation_scheduled",
            )
            api.metrics.observe_builder_turn(
                profile="experiment_builder",
                pi_steps=3,
                phase="validation_scheduled",
            )
            WorkerTelemetryStore(
                root=root / "worker-metrics",
                worker_id="worker-metrics-test",
            ).update(
                increments={"ticks_total": 2, "agent_execution_checked_total": 1},
                tick_duration_seconds=0.125,
                timestamp=100.0,
            )
            app = build_asgi_app(api)

            _asgi_request(app, "/api/v1/health/live")
            _asgi_request(app, "/api/v1/runs/run-sensitive-identifier")
            _asgi_request(app, "/api/v1/random-one/value-one")
            _asgi_request(app, "/api/v1/random-two/value-two")
            _asgi_request(app, "/api/v1/runs/run-one/random-action-one")
            _asgi_request(app, "/api/v1/runs/run-two/random-action-two")
            _asgi_request(app, "/api/v1/agent-sessions/session-sensitive/events")
            response = _asgi_request(app, "/metrics")

        self.assertEqual(response[0], 200)
        self.assertIn("text/plain", response[1]["content-type"])
        metrics = response[2].decode("utf-8")
        self.assertIn(
            'pilot107_api_requests_total{method="GET",route="/api/v1/health/live",status="200"} 1',
            metrics,
        )
        self.assertIn('route="/api/v1/runs/{run_id}"', metrics)
        self.assertNotIn("run-sensitive-identifier", metrics)
        self.assertIn(
            'pilot107_api_requests_total{method="GET",route="/api/v1/unmatched",status="404"} 2',
            metrics,
        )
        self.assertIn(
            'pilot107_api_requests_total{method="GET",'
            'route="/api/v1/runs/{run_id}/{action}",status="404"} 2',
            metrics,
        )
        self.assertNotIn("random-action-one", metrics)
        self.assertNotIn("random-two", metrics)
        self.assertIn('route="/api/v1/agent-sessions/{session_id}/events"', metrics)
        self.assertNotIn("session-sensitive", metrics)
        self.assertIn(
            'pilot107_outbox_messages{state="pending",topic="agent.execute"} 1',
            metrics,
        )
        self.assertIn(
            'pilot107_worker_ticks_total{worker_id="worker-metrics-test"} 2',
            metrics,
        )
        self.assertIn(
            'pilot107_llm_calls_total{model="test-model",outcome="success",provider="local"} 1',
            metrics,
        )
        self.assertIn(
            'pilot107_llm_tokens_total{direction="input",model="test-model",provider="local"} 42',
            metrics,
        )
        self.assertIn(
            'pilot107_agent_tool_invocations_total{outcome="no_progress",'
            'profile="experiment_builder",tool="builder_build_submit"} 1',
            metrics,
        )
        self.assertIn(
            'pilot107_builder_submissions_total{outcome="scheduled",'
            'phase="validation_scheduled"} 1',
            metrics,
        )
        self.assertIn(
            'pilot107_builder_no_progress_total{profile="experiment_builder"} 1',
            metrics,
        )
        self.assertIn(
            'pilot107_builder_pi_steps_bucket{le="3",phase="validation_scheduled",'
            'profile="experiment_builder"} 1',
            metrics,
        )
        self.assertNotIn("project-sensitive", metrics)
        self.assertIn("pilot107_metrics_scrape_error 0", metrics)

    def test_metrics_report_corrupt_durable_source_without_leaking_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metrics_root = root / "worker-metrics"
            metrics_root.mkdir()
            (metrics_root / "worker-corrupt.json").write_text(
                'password="should-never-appear"',
                encoding="utf-8",
            )
            app = build_asgi_app(
                build_api(
                    db_path=root / "pilot107.db",
                    evidence_root=root / "evidence",
                )
            )

            response = _asgi_request(app, "/metrics")

        metrics = response[2].decode("utf-8")
        self.assertEqual(response[0], 200)
        self.assertIn("pilot107_metrics_scrape_error 1", metrics)
        self.assertNotIn("should-never-appear", metrics)


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
            key.decode("latin-1"): value.decode("latin-1") for key, value in start["headers"]
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
