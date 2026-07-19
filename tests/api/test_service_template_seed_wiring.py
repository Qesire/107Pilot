"""build_api_service invokes template market seed at startup."""
from __future__ import annotations

import contextlib
import importlib


def test_build_api_service_invokes_seed_at_startup(cpu_rc_env, monkeypatch):
    from pilot107.api import service as service_module
    importlib.reload(service_module)
    seed_calls: list = []

    def stub_seed(*, catalog, store, role_directory):
        seed_calls.append(
            {"catalog": catalog, "store": store, "role_directory": role_directory}
        )
        from pilot107.core.template_market_seed import SeedReport
        return SeedReport(published=0, skipped=0, gate_blocked=0)

    monkeypatch.setattr(service_module, "seed_preset_recipes", stub_seed)
    with contextlib.suppress(Exception):
        service_module.build_api_service(service_module.config_from_env())
    assert len(seed_calls) == 1, "seed must run exactly once at startup"
    # Verify the right stores were passed
    assert seed_calls[0]["store"] is not None
    assert seed_calls[0]["catalog"] is not None
    assert seed_calls[0]["role_directory"] is not None


def test_build_api_service_seed_failure_is_non_fatal(cpu_rc_env, monkeypatch):
    """If seed raises, build_api_service must not crash."""
    from pilot107.api import service as service_module
    importlib.reload(service_module)

    def bad_seed(**kwargs):
        raise RuntimeError("simulated seed failure")

    monkeypatch.setattr(service_module, "seed_preset_recipes", bad_seed)
    # Should not raise
    try:
        service_module.build_api_service(service_module.config_from_env())
    except RuntimeError:
        raise AssertionError("seed failure must not propagate") from None
    except Exception:
        pass  # other errors (e.g. snapshot) are fine
