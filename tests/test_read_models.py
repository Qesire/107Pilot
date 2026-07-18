import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi, make_handler
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.platform_snapshot import (
    CommandObservation,
    ObservationSourceType,
    PlatformSnapshot,
    PlatformSnapshotScope,
)
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.proxy_auth import ProxyRequestAuthenticator, signed_proxy_headers
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.core.user_entitlement import (
    EntitlementDataQuality,
    UserAssociation,
    UserEntitlementSnapshot,
)
from pilot107.core.user_entitlement_store import UserEntitlementStore
from pilot107.worker.evidence import EvidenceStore


class ProductReadModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "pilot107.db"
        self.run_store = RunStore(self.db_path)
        self.contract_store = ContractStore(self.db_path)
        self.platform_snapshot_store = PlatformSnapshotStore(self.db_path)
        self.user_entitlement_store = UserEntitlementStore(self.db_path)
        self.contract_service = ContractService(
            catalog=RecipeCatalog(store=self.contract_store),
            store=self.contract_store,
        )
        self.run_service = RunService(
            store=self.run_store,
            backend=InMemorySlurmBackend(),
        )
        self.api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            run_service=self.run_service,
            contract_service=self.contract_service,
            platform_snapshot_store=self.platform_snapshot_store,
            user_entitlement_store=self.user_entitlement_store,
            auth_required=True,
        )
        self.alice_headers = {"X-Pilot107-User": "alice"}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_keyset_pagination_filters_and_owner_boundary(self) -> None:
        contract = self.contract_service.create(owner="alice", payload=_contract_payload())
        run_ids = [
            self.run_store.create_run(
                run_id=f"run_alice_{index}",
                contract_id=contract.contract_id,
                owner="alice",
                workdir="/public/home/alice/course-a",
                script="echo page",
            ).run_id
            for index in range(5)
        ]
        self.run_store.update_state(
            run_ids[0],
            RunState.FAILED,
            event_type="test.failed",
        )
        self.run_store.create_run(
            run_id="run_bob_hidden",
            owner="bob",
            workdir="/public/home/bob",
            script="echo hidden",
        )
        bob_contract = self.contract_service.create(owner="bob", payload=_contract_payload())
        self.run_store.create_run(
            run_id="run_alice_corrupt_contract_link",
            contract_id=bob_contract.contract_id,
            owner="alice",
            workdir="/public/home/alice/corrupt-link",
            script="echo corrupt",
        )

        first = self.api.handle_get(
            "/api/v1/runs?limit=2",
            headers=self.alice_headers,
        )
        cursor = first.payload["page"]["next_cursor"]
        second = self.api.handle_get(
            f"/api/v1/runs?limit=2&cursor={cursor}",
            headers=self.alice_headers,
        )
        third_cursor = second.payload["page"]["next_cursor"]
        third = self.api.handle_get(
            f"/api/v1/runs?limit=2&cursor={third_cursor}",
            headers=self.alice_headers,
        )
        listed = [
            item["run_id"] for page in (first, second, third) for item in page.payload["items"]
        ]

        self.assertEqual(first.status, 200)
        self.assertEqual(len(listed), 6)
        self.assertEqual(len(set(listed)), 6)
        self.assertNotIn("run_bob_hidden", listed)

        filtered = self.api.handle_get(
            "/api/v1/runs?state=FAILED&q=course-a&recipe_version_id=recipe_python_cpu%401.0.0",
            headers=self.alice_headers,
        )
        self.assertEqual(
            [item["run_id"] for item in filtered.payload["items"]],
            [run_ids[0]],
        )

        corrupt_recipe_link = self.api.handle_get(
            "/api/v1/runs?recipe_version_id=recipe_python_cpu%401.0.0&q=corrupt-link",
            headers=self.alice_headers,
        )
        self.assertEqual(corrupt_recipe_link.payload["items"], [])

        mismatched_cursor = self.api.handle_get(
            f"/api/v1/runs?limit=2&state=FAILED&cursor={cursor}",
            headers=self.alice_headers,
        )
        cross_owner = self.api.handle_get(
            "/api/v1/runs?owner=bob",
            headers=self.alice_headers,
        )
        invalid = self.api.handle_get(
            "/api/v1/runs?limit=101&unknown=value",
            headers=self.alice_headers,
        )
        self.assertEqual(mismatched_cursor.status, 400)
        self.assertEqual(cross_owner.status, 403)
        self.assertEqual(invalid.status, 400)

    def test_contract_pagination_and_derived_filter(self) -> None:
        source = self.contract_service.create(owner="alice", payload=_contract_payload())
        derived = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id=source.recipe_version_id,
            payload=source.payload,
            contract_id="contract_derived_read",
            parent_contract_id=source.contract_id,
            derivation_reason="manual_copy",
        )
        self.contract_service.create(owner="bob", payload=_contract_payload())

        response = self.api.handle_get(
            "/api/v1/contracts?derived=true&limit=1",
            headers=self.alice_headers,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [item["contract_id"] for item in response.payload["items"]],
            [derived.contract_id],
        )
        self.assertNotIn("contract", response.payload["items"][0])
        self.assertEqual(response.payload["items"][0]["parent_contract_id"], source.contract_id)

    def test_event_cursor_and_lineage_graph_include_workflow_edges(self) -> None:
        dependency = self.run_store.create_run(
            run_id="run_dependency",
            owner="alice",
            workdir="/public/home/alice",
            script="echo dependency",
        )
        root = self.run_store.create_run(
            run_id="run_graph_root",
            owner="alice",
            workdir="/public/home/alice",
            script="echo root",
        )
        child = self.run_store.create_run(
            run_id="run_graph_child",
            owner="alice",
            workdir="/public/home/alice",
            script="echo child",
            parent_run_id=root.run_id,
            lineage_reason="manual_retry",
            workflow={
                "dependencies": [dependency.run_id],
                "retry": {"max_attempts": 1, "backoff_seconds": 0},
                "automation": {"level": "explain", "require_approval": True},
            },
        )
        for index in range(4):
            self.run_store.append_event(
                run_id=child.run_id,
                event_type="test.progress",
                payload={"state": f"step-{index}", "raw_response": {"secret": "hidden"}},
            )

        first = self.api.handle_get(
            f"/api/v1/runs/{child.run_id}/events?type=test.progress&limit=2",
            headers=self.alice_headers,
        )
        cursor = first.payload["page"]["next_cursor"]
        second = self.api.handle_get(
            f"/api/v1/runs/{child.run_id}/events?type=test.progress&limit=2&cursor={cursor}",
            headers=self.alice_headers,
        )
        lineage = self.api.handle_get(
            f"/api/v1/runs/{child.run_id}/lineage",
            headers=self.alice_headers,
        )

        event_ids = [item["event_id"] for page in (first, second) for item in page.payload["items"]]
        self.assertEqual(len(event_ids), 4)
        self.assertEqual(event_ids, sorted(event_ids))
        edges = {
            (edge["source_run_id"], edge["target_run_id"], edge["type"])
            for edge in lineage.payload["edges"]
        }
        self.assertIn((root.run_id, child.run_id, "lineage"), edges)
        self.assertIn((dependency.run_id, child.run_id, "workflow_dependency"), edges)
        self.assertEqual(lineage.payload["root_run_id"], root.run_id)

    def test_agent_pending_and_execution_read_models(self) -> None:
        run = self.run_store.create_run(
            run_id="run_agent_queue",
            owner="alice",
            workdir="/public/home/alice",
            script="echo queue",
        )
        ready, _ = self.run_store.create_agent_advice(
            advice_id="advice_ready",
            run_id=run.run_id,
            owner="alice",
            request_key="ready",
            state="ready",
            source_run_updated_at=run.updated_at,
            evidence_bundle_sha256="evidence",
            provider="none",
            model=None,
            payload={"summary": "ready for approval", "actions": []},
        )
        approved, _ = self.run_store.create_agent_advice(
            advice_id="advice_approved",
            run_id=run.run_id,
            owner="alice",
            request_key="approved",
            state="approved",
            source_run_updated_at=run.updated_at,
            evidence_bundle_sha256="evidence",
            provider="none",
            model=None,
            payload={"summary": "already approved", "actions": []},
        )
        execution, claimed = self.run_store.claim_agent_action_execution(
            execution_id="agentexec_read",
            advice_id=approved.advice_id,
            action_id="action_read",
            owner="alice",
            submit_requested=False,
        )
        self.assertTrue(claimed)
        self.run_store.update_agent_action_execution(execution.execution_id, state="prepared")

        pending = self.api.handle_get(
            "/api/v1/agent/advice?pending=true",
            headers=self.alice_headers,
        )
        executions = self.api.handle_get(
            "/api/v1/agent/executions?state=prepared",
            headers=self.alice_headers,
        )

        self.assertEqual(
            [item["advice_id"] for item in pending.payload["items"]],
            [ready.advice_id],
        )
        self.assertEqual(
            [item["execution_id"] for item in executions.payload["items"]],
            [execution.execution_id],
        )
        self.run_store.decide_agent_advice(
            advice_id=ready.advice_id,
            expected_version=1,
            expected_state="ready",
            new_state="rejected",
            decision="reject",
            actor="alice",
            action_ids=[],
            note=None,
        )
        event_types = [event.event_type for event in self.run_store.list_events(run.run_id)]
        self.assertEqual(event_types.count("agent.advice_created"), 2)
        self.assertIn("agent.advice_decided", event_types)

    def test_etag_and_request_id_are_transport_stable(self) -> None:
        self.run_store.create_run(
            run_id="run_etag",
            owner="alice",
            workdir="/public/home/alice",
            script="echo etag",
        )
        headers = {**self.alice_headers, "X-Request-ID": "request-read-model"}

        first = self.api.handle_get("/api/v1/runs", headers=headers)
        second = self.api.handle_get(
            "/api/v1/runs",
            headers={**headers, "If-None-Match": first.headers["ETag"]},
        )

        self.assertEqual(first.headers["X-Request-ID"], "request-read-model")
        self.assertTrue(first.headers["ETag"].startswith('"'))
        self.assertEqual(second.status, 304)
        self.assertEqual(second.payload, {})

    def test_platform_snapshot_read_models_are_stable_and_owner_scoped(self) -> None:
        for index in range(2):
            self.platform_snapshot_store.create(
                owner="alice",
                snapshot=_platform_snapshot(
                    f"snapshot_api_alice_{index}",
                    f"2026-07-15T00:0{index}:00+00:00",
                ),
                source_type=ObservationSourceType.SIMULATOR,
                source_name="docker-sim",
                expires_at=None,
            )
        self.platform_snapshot_store.create(
            owner="bob",
            snapshot=_platform_snapshot(
                "snapshot_api_bob",
                "2026-07-15T00:10:00+00:00",
            ),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at=None,
        )
        as_of = "2026-07-15T01%3A00%3A00%2B00%3A00"

        first = self.api.handle_get(
            "/api/v1/platform/snapshots?freshness=unknown&limit=1&as_of=" + as_of,
            headers=self.alice_headers,
        )
        cursor = first.payload["page"]["next_cursor"]
        second = self.api.handle_get(
            f"/api/v1/platform/snapshots?freshness=unknown&limit=1&as_of={as_of}&cursor={cursor}",
            headers=self.alice_headers,
        )
        mismatched = self.api.handle_get(
            "/api/v1/platform/snapshots?freshness=unknown&limit=1&as_of="
            f"2026-07-15T02%3A00%3A00%2B00%3A00&cursor={cursor}",
            headers=self.alice_headers,
        )
        cross_owner = self.api.handle_get(
            "/api/v1/platform/snapshots?owner=bob",
            headers=self.alice_headers,
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(
            [item["snapshot_id"] for item in first.payload["items"]],
            ["snapshot_api_alice_1"],
        )
        self.assertEqual(
            [item["snapshot_id"] for item in second.payload["items"]],
            ["snapshot_api_alice_0"],
        )
        self.assertEqual(mismatched.status, 400)
        self.assertEqual(cross_owner.status, 403)

    def test_platform_snapshot_detail_is_safe_and_capabilities_reference_latest(self) -> None:
        record = self.platform_snapshot_store.create(
            owner="alice",
            snapshot=_platform_snapshot(
                "snapshot_api_detail",
                "2026-07-15T00:00:00+00:00",
            ),
            source_type=ObservationSourceType.CLI,
            source_name="login-node",
            expires_at="2026-07-16T00:00:00+00:00",
        )

        detail = self.api.handle_get(
            f"/api/v1/platform/snapshots/{record.snapshot_id}",
            headers=self.alice_headers,
        )
        latest = self.api.handle_get(
            "/api/v1/platform/snapshots/latest?scope=login_node",
            headers=self.alice_headers,
        )
        capabilities = self.api.handle_get(
            "/api/v1/platform/capabilities",
            headers=self.alice_headers,
        )

        command = detail.payload["snapshot"]["command_results"][0]
        self.assertEqual(detail.status, 200)
        self.assertNotIn("stdout", command)
        self.assertNotIn("stderr", command)
        self.assertEqual(latest.payload["snapshot_id"], record.snapshot_id)
        self.assertEqual(
            capabilities.payload["latest_snapshot"]["snapshot_id"],
            record.snapshot_id,
        )

    def test_user_entitlement_read_models_are_safe_and_owner_scoped(self) -> None:
        alice = self.user_entitlement_store.create(
            owner="alice",
            snapshot=_entitlement_snapshot("entitlement_api_alice", "alice"),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at=None,
        )
        bob = self.user_entitlement_store.create(
            owner="bob",
            snapshot=_entitlement_snapshot("entitlement_api_bob", "bob"),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at=None,
        )

        listed = self.api.handle_get(
            "/api/v1/platform/entitlements?freshness=unknown",
            headers=self.alice_headers,
        )
        detail = self.api.handle_get(
            f"/api/v1/platform/entitlements/{alice.snapshot_id}",
            headers=self.alice_headers,
        )
        latest = self.api.handle_get(
            "/api/v1/platform/entitlements/latest",
            headers=self.alice_headers,
        )
        capabilities = self.api.handle_get(
            "/api/v1/platform/capabilities",
            headers=self.alice_headers,
        )
        cross_owner = self.api.handle_get(
            f"/api/v1/platform/entitlements/{bob.snapshot_id}",
            headers=self.alice_headers,
        )

        self.assertEqual(
            [item["snapshot_id"] for item in listed.payload["items"]],
            [alice.snapshot_id],
        )
        command = detail.payload["snapshot"]["command_results"][0]
        self.assertNotIn("argv", command)
        self.assertNotIn("stdout", command)
        self.assertNotIn("stderr", command)
        self.assertEqual(latest.payload["snapshot_id"], alice.snapshot_id)
        self.assertEqual(
            capabilities.payload["latest_entitlement"]["snapshot_id"],
            alice.snapshot_id,
        )
        self.assertEqual(cross_owner.status, 404)

    def test_sse_replay_emits_summary_without_raw_payload(self) -> None:
        run = self.run_store.create_run(
            run_id="run_sse",
            owner="alice",
            workdir="/public/home/alice",
            script="echo stream",
        )
        event = self.run_store.append_event(
            run_id=run.run_id,
            event_type="run.stream_test",
            payload={"state": "FAILED", "raw_response": {"token": "must-not-stream"}},
        )
        secret = b"0123456789abcdef0123456789abcdef"
        self.api.proxy_authenticator = ProxyRequestAuthenticator(secret)
        target = (
            f"/api/v1/runs/{run.run_id}/events/stream?once=true&after_event_id={event.event_id - 1}"
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.api))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}{target}",
                headers={
                    **signed_proxy_headers(
                        secret=secret,
                        method="GET",
                        target=target,
                        user="alice",
                    ),
                    "If-None-Match": '"stale-client-etag"',
                },
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode()
                content_type = response.headers["Content-Type"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertIn("text/event-stream", content_type)
        self.assertIn(f"id: {event.event_id}", body)
        self.assertIn('"state":"FAILED"', body)
        self.assertNotIn("must-not-stream", body)
        metrics = self.api.metrics.render()
        self.assertIn("pilot107_sse_active 0", metrics)
        self.assertIn('pilot107_sse_streams_total{outcome="complete"} 1', metrics)
        self.assertIn("pilot107_sse_events_total 1", metrics)
        traces = self.api.control_repository.list_traces(run_id=run.run_id)
        self.assertEqual(traces[0].route, "/api/v1/runs/{run_id}/events/stream")


class ReadModelScaleTests(unittest.TestCase):
    def test_ten_thousand_run_page_uses_owner_keyset_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "pilot107.db"
            store = RunStore(db_path)
            rows = [
                (
                    f"run_scale_{index:05d}",
                    "alice",
                    "VALIDATED",
                    "pending",
                    "pending",
                    "pending",
                    "UNKNOWN",
                    "/public/home/alice",
                    "echo scale",
                    f"2026-07-15T00:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
                    f"2026-07-15T00:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
                )
                for index in range(10_000)
            ]
            with sqlite3.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO runs (
                        run_id, owner, state, collection_state, diagnosis_state,
                        capsule_state, result_status, workdir, script, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                plan = conn.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT * FROM runs
                    WHERE owner = ?
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT ?
                    """,
                    ("alice", 51),
                ).fetchall()

            items, next_position = store.list_runs_page(owner="alice", limit=50)

            self.assertEqual(len(items), 50)
            self.assertIsNotNone(next_position)
            self.assertTrue(
                any("idx_runs_owner_created" in str(row) for row in plan),
                plan,
            )


def _contract_payload() -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {"command": "echo read-model"},
        "resources": {
            "partition": "debug",
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


def _platform_snapshot(snapshot_id: str, captured_at: str) -> PlatformSnapshot:
    return PlatformSnapshot(
        snapshot_id=snapshot_id,
        scope=PlatformSnapshotScope.LOGIN_NODE,
        captured_at=captured_at,
        collector_version="test.api.v1",
        command_results=(
            CommandObservation(
                name="hostname",
                argv=("hostname",),
                returncode=0,
                stdout="login-node\n",
                stderr="",
            ),
        ),
    )


def _entitlement_snapshot(snapshot_id: str, username: str) -> UserEntitlementSnapshot:
    return UserEntitlementSnapshot(
        snapshot_id=snapshot_id,
        captured_at="2026-07-15T00:00:00+00:00",
        collector_version="test.entitlement.v1",
        data_quality=EntitlementDataQuality.AUTHORITATIVE,
        default_account="students",
        associations=(
            UserAssociation(
                account="students",
                partition=None,
                qos=("normal",),
                default_qos="normal",
            ),
        ),
        command_results=(
            CommandObservation(
                name="sacctmgr_user_assoc_pipe",
                argv=("sacctmgr", "show", "user", f"name={username}", "WithAssoc"),
                returncode=0,
                stdout=f"{username}|students|students||normal|normal\n",
                stderr="",
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
