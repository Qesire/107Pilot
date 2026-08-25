from __future__ import annotations

from dataclasses import replace

import pytest


def _api():
    from pilot107.agent.capabilities import (
        AgentCapabilityClaims,
        AgentCapabilityError,
        AgentCapabilitySigner,
    )

    return AgentCapabilityClaims, AgentCapabilityError, AgentCapabilitySigner


def _claims(now: int):
    AgentCapabilityClaims, _, _ = _api()
    return AgentCapabilityClaims(
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        state_version=3,
        fencing_token=7,
        profile_id="hpc-readonly-v1",
        tools=frozenset({"run_get", "evidence_read"}),
        max_invocations=8,
        max_bytes=262_144,
        expires_at=now + 60,
    )


def test_capability_round_trip_is_canonical_and_secret_safe() -> None:
    _, _, AgentCapabilitySigner = _api()
    now = 1_786_662_000
    signer = AgentCapabilitySigner(b"s" * 32, clock=lambda: now)

    token = signer.sign(_claims(now))
    verified = signer.verify(token)

    assert verified.owner == "alice"
    assert verified.tools == frozenset({"run_get", "evidence_read"})
    assert verified.issued_at == now
    assert token.count(".") == 1
    assert "alice" not in token
    assert "ssss" not in repr(signer)


def test_repair_profile_capability_is_bound_to_one_project_workspace() -> None:
    AgentCapabilityClaims, _, AgentCapabilitySigner = _api()
    now = 1_786_662_000
    signer = AgentCapabilitySigner(b"s" * 32, clock=lambda: now)
    claims = AgentCapabilityClaims(
        owner="alice",
        session_id="session-repair",
        turn_id="turn-repair",
        state_version=3,
        fencing_token=7,
        profile_id="run_diagnosis_repair",
        tools=frozenset({"project_get", "workspace_read", "workspace_patch"}),
        max_invocations=8,
        max_bytes=262_144,
        expires_at=now + 60,
        project_id="project-repair",
        workspace_id="workspace-repair",
        operations=frozenset({"read", "write"}),
        max_commands=0,
    )

    verified = signer.verify(signer.sign(claims))

    assert verified.profile_id == "run_diagnosis_repair"
    assert verified.project_id == "project-repair"
    assert verified.workspace_id == "workspace-repair"


def test_readonly_capability_rejects_workspace_tools() -> None:
    _, AgentCapabilityError, AgentCapabilitySigner = _api()
    now = 1_786_662_000
    signer = AgentCapabilitySigner(b"s" * 32, clock=lambda: now)

    with pytest.raises(AgentCapabilityError):
        signer.sign(replace(_claims(now), tools=frozenset({"workspace_read"})))


def test_blueprint_save_capability_requires_write_operation() -> None:
    AgentCapabilityClaims, AgentCapabilityError, AgentCapabilitySigner = _api()
    now = 1_786_662_000
    signer = AgentCapabilitySigner(b"s" * 32, clock=lambda: now)
    claims = AgentCapabilityClaims(
        owner="alice",
        session_id="session-builder",
        turn_id="turn-builder",
        state_version=3,
        fencing_token=7,
        profile_id="experiment_builder",
        tools=frozenset({"project_blueprint_save"}),
        max_invocations=8,
        max_bytes=262_144,
        expires_at=now + 60,
        project_id="project-builder",
        workspace_id="workspace-builder",
        operations=frozenset({"read"}),
        max_commands=0,
    )

    with pytest.raises(AgentCapabilityError):
        signer.sign(claims)
    writable = replace(claims, operations=frozenset({"write"}))
    assert signer.verify(signer.sign(writable)).tools == frozenset(
        {"project_blueprint_save"}
    )


def test_capability_rejects_signature_tampering_and_malformed_tokens() -> None:
    _, AgentCapabilityError, AgentCapabilitySigner = _api()
    now = 1_786_662_000
    signer = AgentCapabilitySigner(b"s" * 32, clock=lambda: now)
    token = signer.sign(_claims(now))

    with pytest.raises(AgentCapabilityError) as tampered:
        signer.verify(token[:-1] + ("A" if token[-1] != "A" else "B"))
    assert tampered.value.code == "AGENT.CAPABILITY.INVALID"

    with pytest.raises(AgentCapabilityError):
        signer.verify("not-a-token")


def test_capability_rejects_expiry_clock_skew_and_excess_lifetime() -> None:
    _, AgentCapabilityError, AgentCapabilitySigner = _api()
    now = 1_786_662_000
    current = [now]
    signer = AgentCapabilitySigner(b"s" * 32, clock=lambda: current[0])
    token = signer.sign(_claims(now))

    current[0] = now + 66
    with pytest.raises(AgentCapabilityError) as expired:
        signer.verify(token)
    assert expired.value.code == "AGENT.CAPABILITY.EXPIRED"

    current[0] = now
    long_turn = replace(_claims(now), expires_at=now + 300)
    assert signer.verify(signer.sign(long_turn)).expires_at == now + 300

    with pytest.raises(AgentCapabilityError):
        signer.sign(replace(_claims(now), expires_at=now + 301))


def test_capability_requires_a_32_byte_secret() -> None:
    _, _, AgentCapabilitySigner = _api()

    with pytest.raises(ValueError):
        AgentCapabilitySigner(b"short", clock=lambda: 0)
