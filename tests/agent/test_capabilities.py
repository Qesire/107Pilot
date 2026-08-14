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
    with pytest.raises(AgentCapabilityError):
        signer.sign(replace(_claims(now), expires_at=now + 121))


def test_capability_requires_a_32_byte_secret() -> None:
    _, _, AgentCapabilitySigner = _api()

    with pytest.raises(ValueError):
        AgentCapabilitySigner(b"short", clock=lambda: 0)
