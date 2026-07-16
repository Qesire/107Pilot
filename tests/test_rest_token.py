"""Tests for ``pilot107.adapters.rest_token``.

Covers parsing, caching, expiry-triggered re-mint, scontrol failure, and the
security invariant that the token is never present in raised error messages.

A fake executor scripts ``scontrol token`` output without touching Docker.
"""

from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass

from pilot107.adapters.rest_token import (
    RestTokenProvider,
    SimulatorRestTokenProvider,
    StaticTokenProvider,
    _parse_slurm_jwt,
)
from pilot107.adapters.slurm import CommandResult, SlurmTransportError


@dataclass
class _Call:
    argv: list[str]
    user: str | None


class FakeExecutor:
    """Scripts ``scontrol token`` runs and records calls."""

    def __init__(self, *, stdout: str = "", returncode: int = 0) -> None:
        self._stdout = stdout
        self._returncode = returncode
        self.calls: list[_Call] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        del cwd, stdin, timeout_seconds
        self.calls.append(_Call(argv=list(argv), user=user))
        return CommandResult(
            returncode=self._returncode,
            stdout=self._stdout,
            stderr="",
        )

    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        del timeout_seconds
        return path

    def write_text(
        self,
        *,
        path: str,
        content: str,
        owner: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        del path, content, owner, timeout_seconds


class CountingExecutor(FakeExecutor):
    """Returns a different token each call so we can count mints."""

    def __init__(self) -> None:
        super().__init__()
        self._n = 0

    def run(self, argv: list[str], **kwargs) -> CommandResult:  # type: ignore[override]
        self._n += 1
        self.calls.append(_Call(argv=list(argv), user=kwargs.get("user")))
        return CommandResult(
            returncode=0,
            stdout=f"SLURM_JWT=token-{self._n}\n",
            stderr="",
        )


class ManualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_VALID_OUTPUT = "SLURM_JWT=eyJabc.123.def\n"


class ParseSlurmJwtTests(unittest.TestCase):
    def test_parses_single_line(self) -> None:
        self.assertEqual(_parse_slurm_jwt(_VALID_OUTPUT), "eyJabc.123.def")

    def test_parses_when_prefixed_with_noise(self) -> None:
        out = "warning: stuff\nSLURM_JWT=abc.def.ghi\n"
        self.assertEqual(_parse_slurm_jwt(out), "abc.def.ghi")

    def test_returns_none_when_missing(self) -> None:
        self.assertIsNone(_parse_slurm_jwt("nothing here\n"))

    def test_returns_none_for_empty_value(self) -> None:
        self.assertIsNone(_parse_slurm_jwt("SLURM_JWT=\n"))


class SimulatorRestTokenProviderTests(unittest.TestCase):
    def test_mint_parses_token_and_runs_as_user(self) -> None:
        executor = FakeExecutor(stdout=_VALID_OUTPUT)
        provider = SimulatorRestTokenProvider(executor=executor)

        token = provider.mint_token(user="alice", lifespan_seconds=600)

        self.assertEqual(token, "eyJabc.123.def")
        self.assertEqual(len(executor.calls), 1)
        call = executor.calls[0]
        self.assertEqual(call.argv, ["scontrol", "token", "lifespan=600"])
        self.assertEqual(call.user, "alice")

    def test_caching_avoids_repeated_mints(self) -> None:
        executor = CountingExecutor()
        clock = ManualClock()
        provider = SimulatorRestTokenProvider(executor=executor, clock=clock)

        first = provider.mint_token(user="alice", lifespan_seconds=3600)
        second = provider.mint_token(user="alice", lifespan_seconds=3600)

        self.assertEqual(first, "token-1")
        self.assertEqual(second, "token-1")
        self.assertEqual(len(executor.calls), 1)

    def test_expiry_triggers_re_mint(self) -> None:
        executor = CountingExecutor()
        clock = ManualClock()
        provider = SimulatorRestTokenProvider(executor=executor, clock=clock)

        provider.mint_token(user="alice", lifespan_seconds=3600)
        # Cross the refresh threshold (>60s remaining is fresh; advance past it).
        clock.advance(3600 - 30)  # ~30s remaining -> re-mint
        second = provider.mint_token(user="alice", lifespan_seconds=3600)

        self.assertEqual(second, "token-2")
        self.assertEqual(len(executor.calls), 2)

    def test_per_user_caches_are_independent(self) -> None:
        executor = CountingExecutor()
        provider = SimulatorRestTokenProvider(executor=executor)

        a = provider.mint_token(user="alice", lifespan_seconds=3600)
        b = provider.mint_token(user="bob", lifespan_seconds=3600)

        self.assertEqual(a, "token-1")
        self.assertEqual(b, "token-2")
        self.assertEqual(len(executor.calls), 2)

    def test_scontrol_failure_raises_transport_error_without_token(self) -> None:
        # Even if stdout somehow contained a token, a non-zero rc must raise
        # and the error message must never include the token.
        executor = FakeExecutor(stdout="SLURM_JWT=SECRET.TOKEN.VALUE\n", returncode=1)
        provider = SimulatorRestTokenProvider(executor=executor)

        with self.assertRaises(SlurmTransportError) as ctx:
            provider.mint_token(user="alice", lifespan_seconds=600)

        self.assertNotIn("SECRET.TOKEN.VALUE", str(ctx.exception))

    def test_unparseable_output_raises_transport_error(self) -> None:
        executor = FakeExecutor(stdout="garbage\n", returncode=0)
        provider = SimulatorRestTokenProvider(executor=executor)

        with self.assertRaises(SlurmTransportError):
            provider.mint_token(user="alice", lifespan_seconds=600)

    def test_nonpositive_lifespan_rejected(self) -> None:
        executor = FakeExecutor(stdout=_VALID_OUTPUT)
        provider = SimulatorRestTokenProvider(executor=executor)

        with self.assertRaises(SlurmTransportError):
            provider.mint_token(user="alice", lifespan_seconds=0)

        self.assertEqual(len(executor.calls), 0)

    def test_concurrent_mints_share_one_scontrol_call(self) -> None:
        executor = CountingExecutor()
        clock = ManualClock()
        provider = SimulatorRestTokenProvider(executor=executor, clock=clock)
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            provider.mint_token(user="alice", lifespan_seconds=3600)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # One mint serves all threads; the cache lock serializes them.
        self.assertEqual(len(executor.calls), 1)

    def test_token_not_in_exceptions_or_repr(self) -> None:
        # The token legitimately lives in the in-process memory cache (by
        # design). The security invariant is that it must NOT leak into raised
        # error messages or into the provider's repr (which a logger might
        # print). A failing mint must not echo the token either.
        executor_ok = FakeExecutor(stdout="SLURM_JWT=NEVER_LOG_ME\n")
        provider = SimulatorRestTokenProvider(executor=executor_ok)

        token = provider.mint_token(user="alice", lifespan_seconds=600)
        self.assertEqual(token, "NEVER_LOG_ME")
        # repr must not print the cached token.
        self.assertNotIn("NEVER_LOG_ME", repr(provider))

        # A failing scontrol whose stdout accidentally contains a token must
        # not surface it in the exception.
        executor_fail = FakeExecutor(stdout="SLURM_JWT=SECRET.VALUE\n", returncode=1)
        failing = SimulatorRestTokenProvider(executor=executor_fail)
        with self.assertRaises(SlurmTransportError) as ctx:
            failing.mint_token(user="alice", lifespan_seconds=600)
        self.assertNotIn("SECRET.VALUE", str(ctx.exception))
        self.assertNotIn("SECRET.VALUE", repr(ctx.exception))


class StaticTokenProviderTests(unittest.TestCase):
    def test_returns_preset_token(self) -> None:
        provider = StaticTokenProvider("fixed.jwt.token")
        self.assertEqual(provider.mint_token(user="alice"), "fixed.jwt.token")
        self.assertEqual(
            provider.mint_token(user="bob", lifespan_seconds=10),
            "fixed.jwt.token",
        )


class ProtocolConformanceTests(unittest.TestCase):
    def test_simulator_provider_satisfies_protocol(self) -> None:
        executor = FakeExecutor(stdout=_VALID_OUTPUT)
        provider: RestTokenProvider = SimulatorRestTokenProvider(executor=executor)
        self.assertTrue(callable(provider.mint_token))

    def test_static_provider_satisfies_protocol(self) -> None:
        provider: RestTokenProvider = StaticTokenProvider("x")
        self.assertTrue(callable(provider.mint_token))


if __name__ == "__main__":
    unittest.main()
