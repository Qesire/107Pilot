"""Tests for M2 repair ticket domain, store, service, and API routes."""

from __future__ import annotations

from pathlib import Path

import pytest

from pilot107.core.repair_ticket import (
    ArtifactManifest,
    RepairTicket,
    RepairTicketInvariantError,
    RepairTicketState,
    assert_ticket_transition,
)
from pilot107.core.repair_ticket_store import RepairTicketStore

# ---------------------------------------------------------------------------
# Domain model tests
# ---------------------------------------------------------------------------


class TestArtifactManifest:
    def test_valid_manifest(self) -> None:
        m = ArtifactManifest(
            manifest_id="manifest_abc",
            owner="alice",
            run_id=None,
            revision="abc123",
            disclosure="metadata_only",
        )
        assert m.manifest_id == "manifest_abc"
        assert m.disclosure == "metadata_only"

    def test_invalid_disclosure_raises(self) -> None:
        with pytest.raises(RepairTicketInvariantError, match="disclosure"):
            ArtifactManifest(
                manifest_id="m1",
                owner="alice",
                run_id=None,
                revision="r1",
                disclosure="full_code",
            )

    def test_to_payload(self) -> None:
        m = ArtifactManifest(
            manifest_id="m1",
            owner="bob",
            run_id="run_1",
            revision="sha256abc",
            local_test_summary="all pass",
            disclosure="summary",
            created_at="2026-01-01T00:00:00Z",
        )
        payload = m.to_payload()
        assert payload["manifest_id"] == "m1"
        assert payload["run_id"] == "run_1"
        assert payload["local_test_summary"] == "all pass"
        assert payload["disclosure"] == "summary"


class TestRepairTicketState:
    def test_valid_transitions(self) -> None:
        assert_ticket_transition(RepairTicketState.OPEN, RepairTicketState.RESOLVED)
        assert_ticket_transition(RepairTicketState.OPEN, RepairTicketState.ABANDONED)

    def test_invalid_transitions(self) -> None:
        with pytest.raises(RepairTicketInvariantError):
            assert_ticket_transition(RepairTicketState.RESOLVED, RepairTicketState.OPEN)
        with pytest.raises(RepairTicketInvariantError):
            assert_ticket_transition(RepairTicketState.ABANDONED, RepairTicketState.RESOLVED)

    def test_ticket_to_payload(self) -> None:
        t = RepairTicket(
            ticket_id="t1",
            owner="alice",
            state=RepairTicketState.OPEN,
            source_run_id="run_1",
            diagnosis_ids=("d1", "d2"),
            no_go_constraints=("no shell",),
        )
        payload = t.to_payload()
        assert payload["state"] == "open"
        assert payload["diagnosis_ids"] == ["d1", "d2"]
        assert payload["no_go_constraints"] == ["no shell"]


# ---------------------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> RepairTicketStore:
    return RepairTicketStore(tmp_path / "repair.db")


class TestRepairTicketStore:
    def test_create_and_get_manifest(self, store: RepairTicketStore) -> None:
        m = ArtifactManifest(
            manifest_id="manifest_1",
            owner="alice",
            run_id=None,
            revision="rev1",
        )
        created = store.create_manifest(m)
        assert created.created_at != ""
        fetched = store.get_manifest("manifest_1")
        assert fetched.revision == "rev1"
        assert fetched.owner == "alice"

    def test_get_manifest_not_found(self, store: RepairTicketStore) -> None:
        with pytest.raises(KeyError):
            store.get_manifest("nonexistent")

    def test_list_manifests_for_run(self, store: RepairTicketStore) -> None:
        store.create_manifest(
            ArtifactManifest(manifest_id="m1", owner="a", run_id="run_x", revision="r1")
        )
        store.create_manifest(
            ArtifactManifest(manifest_id="m2", owner="a", run_id="run_x", revision="r2")
        )
        store.create_manifest(
            ArtifactManifest(manifest_id="m3", owner="a", run_id="run_y", revision="r3")
        )
        items = store.list_manifests_for_run("run_x")
        assert len(items) == 2

    def test_create_and_get_ticket(self, store: RepairTicketStore) -> None:
        t = RepairTicket(
            ticket_id="t1",
            owner="alice",
            state=RepairTicketState.OPEN,
            source_run_id="run_1",
            source_contract_id="c1",
            session_id="sess_1",
            diagnosis_ids=("d1",),
            cited_facts=(
                {"rule_id": "RUNTIME.NONZERO_EXIT", "severity": "error",
                 "summary": "fail", "evidence_refs": []},
            ),
            requested_change="fix the bug",
            no_go_constraints=("no shell",),
        )
        created = store.create_ticket(t)
        assert created.state == RepairTicketState.OPEN
        assert created.created_at != ""
        fetched = store.get_ticket("t1")
        assert fetched.diagnosis_ids == ("d1",)
        assert fetched.cited_facts[0]["rule_id"] == "RUNTIME.NONZERO_EXIT"

    def test_transition_ticket_resolve(self, store: RepairTicketStore) -> None:
        store.create_manifest(
            ArtifactManifest(manifest_id="m1", owner="a", run_id=None, revision="r1")
        )
        store.create_ticket(
            RepairTicket(
                ticket_id="t1",
                owner="alice",
                state=RepairTicketState.OPEN,
                source_run_id="run_1",
            )
        )
        updated = store.transition_ticket(
            "t1",
            target_state=RepairTicketState.RESOLVED,
            resolution_manifest_id="m1",
            resolution_run_id="run_2",
            resolution_comparison={"improved": True},
        )
        assert updated.state == RepairTicketState.RESOLVED
        assert updated.resolution_manifest_id == "m1"
        assert updated.resolution_run_id == "run_2"
        assert updated.resolution_comparison == {"improved": True}

    def test_transition_ticket_abandon(self, store: RepairTicketStore) -> None:
        store.create_ticket(
            RepairTicket(
                ticket_id="t1",
                owner="alice",
                state=RepairTicketState.OPEN,
                source_run_id="run_1",
            )
        )
        updated = store.transition_ticket(
            "t1",
            target_state=RepairTicketState.ABANDONED,
            abandon_reason="not needed",
        )
        assert updated.state == RepairTicketState.ABANDONED
        assert updated.abandon_reason == "not needed"

    def test_invalid_transition_raises(self, store: RepairTicketStore) -> None:
        store.create_ticket(
            RepairTicket(
                ticket_id="t1",
                owner="alice",
                state=RepairTicketState.OPEN,
                source_run_id="run_1",
            )
        )
        store.transition_ticket("t1", target_state=RepairTicketState.RESOLVED)
        with pytest.raises(RepairTicketInvariantError):
            store.transition_ticket("t1", target_state=RepairTicketState.ABANDONED)

    def test_list_tickets_page(self, store: RepairTicketStore) -> None:
        for i in range(5):
            store.create_ticket(
                RepairTicket(
                    ticket_id=f"t{i}",
                    owner="alice",
                    state=RepairTicketState.OPEN,
                    source_run_id=f"run_{i}",
                )
            )
        items, next_pos = store.list_tickets_page(owner="alice", limit=3)
        assert len(items) == 3
        assert next_pos is not None

    def test_list_tickets_for_session(self, store: RepairTicketStore) -> None:
        store.create_ticket(
            RepairTicket(
                ticket_id="t1",
                owner="alice",
                state=RepairTicketState.OPEN,
                source_run_id="run_1",
                session_id="sess_a",
            )
        )
        store.create_ticket(
            RepairTicket(
                ticket_id="t2",
                owner="alice",
                state=RepairTicketState.OPEN,
                source_run_id="run_2",
                session_id="sess_b",
            )
        )
        items = store.list_tickets_for_session("sess_a")
        assert len(items) == 1
        assert items[0].ticket_id == "t1"

    def test_bind_manifest_to_run(self, store: RepairTicketStore) -> None:
        store.create_manifest(
            ArtifactManifest(manifest_id="m1", owner="a", run_id=None, revision="r1")
        )
        store.bind_manifest_to_run("m1", "run_new")
        fetched = store.get_manifest("m1")
        assert fetched.run_id == "run_new"
