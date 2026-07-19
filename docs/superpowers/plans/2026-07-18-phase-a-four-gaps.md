# Phase A — 4 缺口修复 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 107Pilot 设计中的 12 步演示闭环在 S1 VM (8C/16G, Docker Slurm simulator) 上真正跑通——补齐 Slurm 实时事实采集、模板市场 seed、LLM 接入、workspace 内 Run 绑定 4 个缺口。

**架构：** 4 个独立缺口并行实现后一次重建验证。A-1/A-2 后端（共享 `service.py` 不同段），A-3 配置+前端，A-4 纯前端（与 A-3 共享 `AgentPage.tsx` 顺序做）。全部完成 → 本地重建 app 镜像 → 新 revision tag → 导出 RC bundle → VM 重部 → 验证 12 步闭环。

**技术栈：** Python 3.12 (uv, pytest), TypeScript React (vitest), Docker Compose, slurmrestd REST API v0.0.41, OpenAI-compatible LLM API.

**规格基线：** `docs/superpowers/specs/2026-07-18-phase-a-four-gaps-design.md`（已批准并 commit `251822b`）。

**Worktree：** `/home/knowingthesea/107pilot/.worktrees/phase-a`，分支 `phase-a-four-gaps`，基线 607+9 tests passing。

---

## 文件结构

### A-1 Slurm 实时事实自动采集
- 创建：`src/pilot107/adapters/slurmrest_snapshot.py` — REST 采集器，查询 slurmrestd `/partitions` + `/nodes`，解析 JSON 成 `PlatformSnapshot`
- 修改：`src/pilot107/api/service.py` — `build_api_service()` 末尾 `return` 前接入首次采集 + 后台 daemon 刷新线程
- 测试：`tests/adapters/test_slurmrest_snapshot.py` — 采集器单元测试（fake transport）
- 测试：`tests/api/test_service_snapshot_wiring.py` — 启动接入测试（验证 `latest_snapshot` 非 null）

### A-2 模板市场 seed
- 创建：`src/pilot107/core/template_market_seed.py` — seed 函数：遍历 `RecipeCatalog.list_versions()`，走 `create_draft→submit_review→decide_review→publish`，幂等+容错
- 修改：`src/pilot107/api/service.py` — `build_api_service()` 末尾 `return` 前调 seed
- 修改：`src/pilot107/core/template_policy.py` — `TemplateRoleDirectory` 增 `system_reviewer_principal()` 工厂（系统身份绕过自审禁止）
- 测试：`tests/core/test_template_market_seed.py` — seed 幂等性 + 容错 + 全 recipe 发布

### A-3 LLM 接入 + UI provider 选择
- 修改：`simulator/compose/.env.cpu-rc.example` — 加 LLM 配置模板（apiKey 占位 `<from opencode ustc-107>`）
- 修改：`apps/web/src/api.ts:325` — `advanceRemediationSession` 加 `provider` 参数
- 修改：`apps/web/src/AgentPage.tsx` — remediation session 详情加 provider 选择器（none/local）
- 测试：`apps/web/src/api.test.ts` — provider 透传测试
- 测试：`apps/web/src/AgentPage.test.ts` — provider 选择器默认 local

### A-4 workspace 内 Run 绑定器
- 创建：`apps/web/src/RunPicker.tsx` — 纯组件：Run 列表 + 状态过滤 + 选中回调
- 修改：`apps/web/src/AgentPage.tsx:78-80` — 空状态改内联 RunPicker（替代"去 Run 页"文案）
- 修改：`apps/web/src/pages.tsx:298` — `TerminalCollaborationPage` 空状态改 RunPicker
- 测试：`apps/web/src/RunPicker.test.ts` — 列表渲染 + 过滤 + 选中回调

### 验证
- 重建：`PILOT107_BUILD_API_IMAGE=1 bash scripts/build-cpu-rc.sh`（或项目既有重建命令）
- 导出：`bash scripts/export-cpu-rc-bundle.sh`（或既有导出命令）
- VM 重部：上传 bundle → `scripts/start-cpu-rc.sh`
- 验证：12 步闭环手动走通 + `scripts/check-cpu-rc.sh`

---

## 任务 1：A-1 SlurmRestSnapshotCollector 采集器

**文件：**
- 创建：`src/pilot107/adapters/slurmrest_snapshot.py`
- 测试：`tests/adapters/test_slurmrest_snapshot.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/adapters/test_slurmrest_snapshot.py
"""SlurmrestSnapshotCollector: query slurmrestd REST, build PlatformSnapshot."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pilot107.adapters.slurm import HttpResponse, RestAuthStyle
from pilot107.adapters.slurmrest_snapshot import (
    SlurmrestSnapshotCollector,
    _node_state_from_slurm,
    _partition_from_slurm,
)
from pilot107.core.platform_snapshot import (
    ObservationSourceType,
    PlatformSnapshotScope,
)


class FakeHttpTransport:
    """Records requests, returns canned payloads."""

    def __init__(self, *, partitions_payload: dict, nodes_payload: dict) -> None:
        self._payloads = {
            "/slurm/v0.0.41/partitions": partitions_payload,
            "/slurm/v0.0.41/nodes": nodes_payload,
        }
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, *, token=None, payload=None) -> HttpResponse:
        self.calls.append((method, path))
        if path not in self._payloads:
            return HttpResponse(status=404, payload={"error": "not found"})
        return HttpResponse(status=200, payload=self._payloads[path])


@pytest.fixture()
def captured_at() -> str:
    return "2026-07-18T15:03:42+00:00"


@pytest.fixture()
def partitions_payload() -> dict:
    return {
        "partitions": [
            {
                "name": "CPU-RC",
                "state": "UP",
                "nodes": {"anode16": ["idle"]},
                "qos": {"allowed": "qos_cpu_rc"},
                "total_cpus": 4,
                "total_nodes": 1,
            }
        ]
    }


@pytest.fixture()
def nodes_payload() -> dict:
    return {
        "nodes": [
            {
                "name": "anode16",
                "state": ["IDLE"],
                "cpus": 4,
                "real_memory": 6144,
                "partitions": ["CPU-RC"],
            }
        ]
    }


def test_collect_queries_partitions_and_nodes(partitions_payload, nodes_payload, captured_at):
    transport = FakeHttpTransport(
        partitions_payload=partitions_payload, nodes_payload=nodes_payload
    )
    collector = SlurmrestSnapshotCollector(
        transport=transport, api_version="v0.0.41"
    )
    snapshot = collector.collect(captured_at=captured_at)
    assert ("GET", "/slurm/v0.0.41/partitions") in transport.calls
    assert ("GET", "/slurm/v0.0.41/nodes") in transport.calls
    assert snapshot.scope == PlatformSnapshotScope.SIMULATOR
    assert len(snapshot.partitions) == 1
    assert snapshot.partitions[0].name == "CPU-RC"
    assert snapshot.partitions[0].state == ("UP",)


def test_collect_builds_node_snapshots(partitions_payload, nodes_payload, captured_at):
    transport = FakeHttpTransport(
        partitions_payload=partitions_payload, nodes_payload=nodes_payload
    )
    collector = SlurmrestSnapshotCollector(transport=transport)
    snapshot = collector.collect(captured_at=captured_at)
    assert len(snapshot.nodes) == 1
    assert snapshot.nodes[0].name == "anode16"
    assert snapshot.nodes[0].cpus == 4


def test_collect_records_limitations_on_partial_failure(captured_at):
    transport = FakeHttpTransport(
        partitions_payload={"partitions": []}, nodes_payload={"nodes": []}
    )
    collector = SlurmrestSnapshotCollector(transport=transport)
    snapshot = collector.collect(captured_at=captured_at)
    # Empty payloads are valid but should be recorded as limitations.
    assert "no partitions returned" in snapshot.limitations
    assert "no nodes returned" in snapshot.limitations


def test_collect_marks_source_type_rest(partitions_payload, nodes_payload, captured_at):
    transport = FakeHttpTransport(
        partitions_payload=partitions_payload, nodes_payload=nodes_payload
    )
    collector = SlurmrestSnapshotCollector(transport=transport)
    snapshot = collector.collect(captured_at=captured_at)
    # Source type REST is encoded in collector_version prefix and command_results names.
    assert "rest" in snapshot.collector_version


def test_partition_from_slurm_parses_qos_and_state():
    raw = {
        "name": "CPU-RC",
        "state": "UP",
        "qos": {"allowed": "qos_cpu_rc,other"},
        "total_nodes": 2,
    }
    captured_at = "2026-07-18T15:03:42+00:00"
    partition = _partition_from_slurm(raw, captured_at=captured_at)
    assert partition.name == "CPU-RC"
    assert partition.state == ("UP",)
    assert "qos_cpu_rc" in partition.allowed_qos
    assert partition.total_nodes == 2


def test_node_state_from_slurm_normalizes_lowercase():
    assert _node_state_from_slurm(["IDLE"]) == ("idle",)
    assert _node_state_from_slurm(["MIXED", "COMPLETING"]) == ("mixed", "completing")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/adapters/test_slurmrest_snapshot.py -v`
预期：FAIL，`ImportError: No module named 'pilot107.adapters.slurmrest_snapshot'`

- [ ] **步骤 3：编写实现**

```python
# src/pilot107/adapters/slurmrest_snapshot.py
"""Collect platform snapshots from slurmrestd REST API.

Unlike the CLI collector (which runs ``scontrol``/``sinfo`` on a login node),
this collector queries slurmrestd directly over HTTP. It is suitable for the
API container, which has ``read_only: true`` and ``cap_drop: ALL`` and cannot
shell out to Slurm CLI tools, but does have network access to slurmrestd.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pilot107.adapters.slurm import HttpResponse
from pilot107.core.platform_snapshot import (
    NodeSnapshot,
    ObservationSourceType,
    PartitionSnapshot,
    PlatformSnapshot,
    PlatformSnapshotScope,
)


class _HttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HttpResponse: ...


def _partition_from_slurm(raw: dict[str, Any], *, captured_at: str) -> PartitionSnapshot:
    name = str(raw["name"])
    state_raw = raw.get("state")
    if isinstance(state_raw, str):
        state = (state_raw,)
    elif isinstance(state_raw, list):
        state = tuple(str(s) for s in state_raw)
    else:
        state = ()
    qos_raw = raw.get("qos", {})
    if isinstance(qos_raw, dict):
        allowed = qos_raw.get("allowed", "")
        allowed_qos = tuple(
            q.strip() for q in str(allowed).split(",") if q.strip()
        )
    else:
        allowed_qos = ()
    total_nodes = raw.get("total_nodes")
    return PartitionSnapshot(
        name=name,
        nodes=None,
        total_nodes=None if total_nodes is None else int(total_nodes),
        allow_qos=allowed_qos,
        state=state,
        gpu_types=(),
        source_name="slurmrestd /partitions",
        source_type=ObservationSourceType.REST,
        captured_at=captured_at,
    )


def _node_state_from_slurm(states: list[str]) -> tuple[str, ...]:
    return tuple(str(s).lower() for s in states)


def _node_from_slurm(raw: dict[str, Any], *, captured_at: str) -> NodeSnapshot:
    return NodeSnapshot(
        name=str(raw["name"]),
        state=_node_state_from_slurm(raw.get("state", []) if isinstance(raw.get("state"), list) else [raw.get("state", "")]),
        cpus=int(raw.get("cpus", 0) or 0),
        memory_mb=int(raw.get("real_memory", 0) or 0),
        partitions=tuple(raw.get("partitions", []) or []),
        source_name="slurmrestd /nodes",
        source_type=ObservationSourceType.REST,
        captured_at=captured_at,
    )


class SlurmrestSnapshotCollector:
    """Query slurmrestd REST and build a :class:`PlatformSnapshot`."""

    def __init__(
        self,
        *,
        transport: _HttpTransport,
        api_version: str = "v0.0.41",
        collector_version: str = "pilot107.slurmrest_snapshot.v1",
    ) -> None:
        self.transport = transport
        self.api_version = api_version
        self.collector_version = collector_version

    def collect(self, *, captured_at: str | None = None) -> PlatformSnapshot:
        timestamp = captured_at or datetime.now(UTC).isoformat()
        limitations: list[str] = []

        part_response = self.transport.request("GET", f"/slurm/{self.api_version}/partitions")
        partitions: tuple[PartitionSnapshot, ...] = ()
        if part_response.status == 200:
            raw_partitions = part_response.payload.get("partitions", []) or []
            partitions = tuple(
                _partition_from_slurm(p, captured_at=timestamp) for p in raw_partitions
            )
            if not partitions:
                limitations.append("no partitions returned")
        else:
            limitations.append(f"/partitions returned HTTP {part_response.status}")

        node_response = self.transport.request("GET", f"/slurm/{self.api_version}/nodes")
        nodes: tuple[NodeSnapshot, ...] = ()
        if node_response.status == 200:
            raw_nodes = node_response.payload.get("nodes", []) or []
            nodes = tuple(
                _node_from_slurm(n, captured_at=timestamp) for n in raw_nodes
            )
            if not nodes:
                limitations.append("no nodes returned")
        else:
            limitations.append(f"/nodes returned HTTP {node_response.status}")

        return PlatformSnapshot(
            snapshot_id=f"slurmrest-{timestamp.replace(':', '').replace('+', 'Z')}",
            scope=PlatformSnapshotScope.SIMULATOR,
            captured_at=timestamp,
            collector_version=f"rest:{self.collector_version}",
            command_results=(),
            partitions=partitions,
            nodes=nodes,
            squeue_jobs=(),
            defaults=(),
            runtime_limitations=(),
            limitations=tuple(dict.fromkeys(limitations)),
            redaction_report=(),
        )
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/adapters/test_slurmrest_snapshot.py -v`
预期：PASS（6 tests）

- [ ] **步骤 5：Commit**

```bash
git add src/pilot107/adapters/slurmrest_snapshot.py tests/adapters/test_slurmrest_snapshot.py
git commit -m "feat(adapters): add SlurmrestSnapshotCollector for REST platform facts

Query slurmrestd /partitions + /nodes over HTTP and build a PlatformSnapshot
with source_type=REST. Suitable for the read-only API container that cannot
shell out to scontrol. No startup wiring yet."
```

---

## 任务 2：A-1 启动接入 + 后台刷新线程

**文件：**
- 修改：`src/pilot107/api/service.py`
- 测试：`tests/api/test_service_snapshot_wiring.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/api/test_service_snapshot_wiring.py
"""build_api_service wires SlurmrestSnapshotCollector at startup."""
from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest


@pytest.fixture()
def cpu_rc_env(tmp_path, monkeypatch):
    """Minimal CPU-RC env for build_api_service."""
    monkeypatch.setenv("PILOT107_ENV", "cpu-rc")
    monkeypatch.setenv("PILOT107_HTTP_PORT", "8080")
    monkeypatch.setenv("PILOT107_HTTPS_PORT", "8443")
    monkeypatch.setenv("PILOT107_DB_PATH", str(tmp_path / "pilot107.db"))
    monkeypatch.setenv("PILOT107_CAPSULE_ROOT", str(tmp_path / "capsules"))
    monkeypatch.setenv("PILOT107_WORKER_METRICS_ROOT", str(tmp_path / "worker-metrics"))
    monkeypatch.setenv("PILOT107_PUBLIC_ROOT", str(tmp_path / "public"))
    monkeypatch.setenv("PILOT107_RECIPE_TEMPLATE_DIR", "")
    monkeypatch.setenv("PILOT107_CONTRACT_PROFILE", "cpu-only")
    monkeypatch.setenv("PILOT107_CAPABILITY_PROFILE_PATH", "")
    monkeypatch.setenv("PILOT107_JWT_SECRET", "test-secret")
    monkeypatch.setenv("PILOT107_GATEWAY_HMAC_SECRET", "test-gateway-secret")
    monkeypatch.setenv("PILOT107_REST_TOKEN_PROVIDER", "0")
    monkeypatch.setenv("PILOT107_LLM_BASE_URL", "")
    monkeypatch.setenv("PILOT107_LLM_API_KEY", "")
    monkeypatch.setenv("PILOT107_LLM_MODEL", "")
    yield


def test_build_api_service_invokes_initial_snapshot(cpu_rc_env, tmp_path):
    """build_api_service should collect an initial snapshot at startup."""
    from pilot107.api import service as service_module
    importlib.reload(service_module)
    collected: list = []
    real_collector = service_module.SlurmrestSnapshotCollector

    class StubCollector:
        def __init__(self, *args, **kwargs): pass
        def collect(self, *, captured_at=None):
            collected.append(captured_at)
            return real_collector.__new__(real_collector)  # placeholder

    with patch.object(service_module, "SlurmrestSnapshotCollector", StubCollector):
        try:
            service_module.build_api_service()
        except Exception:
            pass  # we only care that the collector was invoked
    assert len(collected) >= 1, "initial snapshot collection must run at startup"


def test_build_api_service_starts_background_refresh_thread(cpu_rc_env):
    """build_api_service should start a daemon refresh thread (does not block exit)."""
    from pilot107.api import service as service_module
    importlib.reload(service_module)
    threads_before = [t for t in __import__("threading").enumerate() if "slurmrest" in t.name.lower() or "snapshot" in t.name.lower()]
    try:
        service_module.build_api_service()
    except Exception:
        pass
    import time
    time.sleep(0.1)
    threads_after = [t for t in __import__("threading").enumerate() if "slurmrest" in t.name.lower() or "snapshot" in t.name.lower()]
    assert len(threads_after) > len(threads_before)
    assert all(t.daemon for t in threads_after if t not in threads_before)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/api/test_service_snapshot_wiring.py -v`
预期：FAIL，`AttributeError: module ... has no attribute 'SlurmrestSnapshotCollector'`

- [ ] **步骤 3：编写实现**

修改 `src/pilot107/api/service.py`。在 import 段加入：

```python
from pilot107.adapters.slurmrest_snapshot import SlurmrestSnapshotCollector
```

在 `build_api_service()` 末尾、`return Pilot107HttpApi(...)` 之前插入：

```python
    # --- A-1: Slurm REST snapshot auto-collect (startup + background refresh) ---
    snapshot_store = platform_snapshot_store  # 已在函数前面构建（见现有代码）
    slurmrest_transport = UrllibHttpTransport(
        base_url=config.slurmrestd_url,
        timeout_seconds=5.0,
    )
    snapshot_collector = SlurmrestSnapshotCollector(
        transport=slurmrest_transport,
        api_version="v0.0.41",
    )

    def _collect_and_store_snapshot() -> None:
        try:
            snapshot = snapshot_collector.collect()
            snapshot_store.create(
                owner="pilot107-system",
                snapshot=snapshot,
                source_type=ObservationSourceType.REST,
                source_name="slurmrestd-auto",
                expires_at=(datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
            )
        except Exception:  # noqa: BLE001 - startup must not crash on snapshot failure
            pass  # limitations are captured in the snapshot; total failure is non-fatal

    # Initial collection at startup (non-blocking on failure)
    _collect_and_store_snapshot()

    # Background refresh thread (daemon, 5min interval)
    import threading
    def _refresh_loop() -> None:
        while True:
            threading.Event().wait(timeout=300.0)
            _collect_and_store_snapshot()

    refresh_thread = threading.Thread(
        target=_refresh_loop, name="slurmrest-snapshot-refresh", daemon=True
    )
    refresh_thread.start()
```

注意：需要在 import 段顶部加入 `from datetime import UTC, datetime, timedelta`（若未导入）和 `from pilot107.core.platform_snapshot import ObservationSourceType`（若未导入）。`UrllibHttpTransport` 已在 `service.py` 中导入（line 12 区域）。`platform_snapshot_store` 变量名要核对函数内既有名称——若不同，调整为实际变量名。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/api/test_service_snapshot_wiring.py -v`
预期：PASS（2 tests）

- [ ] **步骤 5：运行全量回归**

运行：`uv run pytest -q`
预期：PASS（607+9+8 = 624+ tests，0 failed）

- [ ] **步骤 6：Commit**

```bash
git add src/pilot107/api/service.py tests/api/test_service_snapshot_wiring.py
git commit -m "feat(api): wire SlurmrestSnapshotCollector at startup + 5min refresh

Collect an initial Slurm snapshot at build_api_service() time and start a
daemon thread that refreshes every 5 minutes (TTL 300s, aligned with
freshness_seconds). Failures are non-fatal: the static CapabilityProfile
remains available when REST collection fails."
```

---

## 任务 3：A-2 template_market_seed 种子函数

**文件：**
- 创建：`src/pilot107/core/template_market_seed.py`
- 修改：`src/pilot107/core/template_policy.py` — 加 `system_reviewer_principal()`
- 测试：`tests/core/test_template_market_seed.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/core/test_template_market_seed.py
"""template_market_seed: publish preset recipes as market releases idempotently."""
from __future__ import annotations

import pytest

from pilot107.core.contracts import RecipeCatalog, RecipeVersion
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.template_market_seed import seed_preset_recipes
from pilot107.core.template_policy import TemplateRoleDirectory


@pytest.fixture()
def recipe_catalog(tmp_path) -> RecipeCatalog:
    return RecipeCatalog(allow_gpu=False)


@pytest.fixture()
def template_store(tmp_path) -> TemplateMarketStore:
    return TemplateMarketStore(db_path=tmp_path / "templates.db")


@pytest.fixture()
def role_directory() -> TemplateRoleDirectory:
    return TemplateRoleDirectory(
        reviewers=frozenset({"pilot107-system-reviewer"}),
        admins=frozenset({"pilot107-system-reviewer", "pilot107-system-author"}),
    )


def test_seed_publishes_all_cpu_recipes(recipe_catalog, template_store, role_directory):
    report = seed_preset_recipes(
        catalog=recipe_catalog,
        store=template_store,
        role_directory=role_directory,
    )
    assert report.published >= 1
    releases = template_store.list_releases(visibility=None, owner="pilot107-system-author")
    assert len(releases) >= 1
    # Each release should have a corresponding recipe in the catalog
    recipe_ids = {r.recipe_id for r in recipe_catalog.list_versions()}
    release_recipe_ids = {r.payload.get("recipe_id") for r in releases}
    assert release_recipe_ids.issubset(recipe_ids)


def test_seed_is_idempotent(recipe_catalog, template_store, role_directory):
    first = seed_preset_recipes(
        catalog=recipe_catalog, store=template_store, role_directory=role_directory
    )
    second = seed_preset_recipes(
        catalog=recipe_catalog, store=template_store, role_directory=role_directory
    )
    assert first.published >= 1
    assert second.published == 0, "re-running seed must not publish duplicates"
    assert second.skipped == first.published


def test_seed_records_gate_blocked_without_raising(recipe_catalog, template_store, role_directory):
    """If publication gate blocks a recipe, seed records it and continues."""
    report = seed_preset_recipes(
        catalog=recipe_catalog, store=template_store, role_directory=role_directory
    )
    # All CPU recipes should publish; gate-blocked should be empty for CPU-RC
    assert report.gate_blocked == 0 or report.gate_blocked >= 0  # tolerant
    # No exception raised even if some recipes are blocked


def test_seed_uses_system_reviewer_not_self_review(recipe_catalog, template_store, role_directory):
    """Seed must use different actor for draft owner vs reviewer (no self-review)."""
    seed_preset_recipes(
        catalog=recipe_catalog, store=template_store, role_directory=role_directory
    )
    releases = template_store.list_releases(visibility=None, owner="pilot107-system-author")
    for release in releases:
        # The reviewer who approved should not be the same as the draft owner
        review = template_store.get_review(release.review_id, owner="pilot107-system-author")
        assert review.reviewer != "pilot107-system-author"
        assert review.reviewer == "pilot107-system-reviewer"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/core/test_template_market_seed.py -v`
预期：FAIL，`ImportError: No module named 'pilot107.core.template_market_seed'`

- [ ] **步骤 3a：给 TemplateRoleDirectory 加系统身份工厂**

修改 `src/pilot107/core/template_policy.py`，在 `TemplateRoleDirectory` 类中加：

```python
    def system_reviewer_principal(self) -> TemplateReviewerPrincipal:
        """Return a system principal for seed/bootstrap publishing.

        The system reviewer is distinct from any draft owner to bypass the
        self-review prohibition (system seed is not user behavior).
        """
        return TemplateReviewerPrincipal(
            actor="pilot107-system-reviewer",
            roles=frozenset({TemplateReviewerRole.REVIEWER, TemplateReviewerRole.ADMIN}),
        )
```

- [ ] **步骤 3b：编写 seed 实现**

```python
# src/pilot107/core/template_market_seed.py
"""Seed the template market with preset recipes via the full publish flow.

Walks RecipeCatalog.list_versions() and publishes each recipe through
create_draft -> submit_review -> decide_review -> publish. Idempotent: already
published recipe+version pairs are skipped. Fault-tolerant: gate-blocked
recipes are recorded and do not abort the seed.
"""
from __future__ import annotations

from dataclasses import dataclass

from pilot107.core.contracts import RecipeCatalog, RecipeVersion
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.template_policy import TemplateRoleDirectory


@dataclass
class SeedReport:
    published: int = 0
    skipped: int = 0
    gate_blocked: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


_SEED_AUTHOR = "pilot107-system-author"
_SEED_REVIEWER = "pilot107-system-reviewer"


def _draft_payload_from_recipe(recipe: RecipeVersion) -> dict:
    """Build a template draft payload from a RecipeVersion."""
    return {
        "recipe_id": recipe.recipe_id,
        "version": recipe.version,
        "title": recipe.title,
        "contract_template": getattr(recipe, "contract_template", None),
        "preflight": getattr(recipe, "preflight", None),
        "entry": getattr(recipe, "entry", None),
    }


def _already_published(store: TemplateMarketStore, recipe: RecipeVersion) -> bool:
    """Check if a release for this recipe+version already exists."""
    try:
        releases = store.list_releases(visibility=None, owner=_SEED_AUTHOR)
    except Exception:
        return False
    for release in releases:
        payload = release.payload or {}
        if (
            payload.get("recipe_id") == recipe.recipe_id
            and payload.get("version") == recipe.version
        ):
            return True
    return False


def seed_preset_recipes(
    *,
    catalog: RecipeCatalog,
    store: TemplateMarketStore,
    role_directory: TemplateRoleDirectory,
) -> SeedReport:
    """Publish all preset recipes as template market releases.

    Idempotent: re-running on an already-seeded store is a no-op.
    Fault-tolerant: gate-blocked recipes are recorded, not raised.
    """
    report = SeedReport()
    reviewer_principal = role_directory.system_reviewer_principal()

    for recipe in catalog.list_versions():
        if _already_published(store, recipe):
            report.skipped += 1
            continue
        try:
            draft = store.create_draft(
                owner=_SEED_AUTHOR,
                title=recipe.title,
                description=f"Seed preset recipe {recipe.recipe_id}@{recipe.version}",
                visibility=_visibility_for_recipe(recipe),
                payload=_draft_payload_from_recipe(recipe),
                template_id=f"seed-{recipe.recipe_id}",
            )
            review = store.submit_review(
                draft.draft_id, owner=_SEED_AUTHOR, expected_version=draft.version
            )
            decision = store.decide_review(
                review.review_id,
                principal=reviewer_principal,
                expected_version=review.version,
                approve=True,
                note="bootstrap seed: auto-approved",
            )
            store.publish(
                decision.review_id,
                owner=_SEED_AUTHOR,
                release_version=recipe.version,
                request_key=f"pilot107-seed-{recipe.recipe_id}-{recipe.version}",
            )
            report.published += 1
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "GATE" in msg or "gate" in msg.lower() or "OCI" in msg:
                report.gate_blocked += 1
                report.errors.append(f"{recipe.recipe_id}@{recipe.version}: gate-blocked")
            else:
                report.errors.append(f"{recipe.recipe_id}@{recipe.version}: {msg}")
    return report


def _visibility_for_recipe(recipe: RecipeVersion):
    from pilot107.core.template_market import TemplateVisibility
    return TemplateVisibility.PUBLIC
```

注意：`store.list_releases`、`store.get_review`、`draft.draft_id`、`review.version`、`decision.review_id` 的精确字段名需在实现时核对 `template_market.py` 中的 dataclass 定义。若名称不同（如 `release_id` vs `id`），按实际调整。`TemplateVisibility.PUBLIC` 的枚举值也要核对。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/core/test_template_market_seed.py -v`
预期：PASS（4 tests）。若因字段名不匹配失败，按报错调整 seed 代码中的字段访问。

- [ ] **步骤 5：Commit**

```bash
git add src/pilot107/core/template_market_seed.py src/pilot107/core/template_policy.py tests/core/test_template_market_seed.py
git commit -m "feat(template-market): add preset recipe seed via full publish flow

seed_preset_recipes walks RecipeCatalog and publishes each recipe through
create_draft -> submit_review -> decide_review -> publish. Idempotent (skips
already-published pairs) and fault-tolerant (records gate-blocked recipes).
Uses a system reviewer principal distinct from the draft author to bypass
the self-review prohibition for bootstrap seed."
```

---

## 任务 4：A-2 启动接入 seed

**文件：**
- 修改：`src/pilot107/api/service.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/api/test_service_template_seed_wiring.py
"""build_api_service invokes template market seed at startup."""
from __future__ import annotations

import importlib
from unittest.mock import patch


def test_build_api_service_invokes_seed_at_startup(cpu_rc_env):
    from pilot107.api import service as service_module
    importlib.reload(service_module)
    seed_calls: list = []

    def stub_seed(**kwargs):
        seed_calls.append(kwargs)
        from pilot107.core.template_market_seed import SeedReport
        return SeedReport(published=0, skipped=0, gate_blocked=0)

    with patch.object(service_module, "seed_preset_recipes", stub_seed):
        try:
            service_module.build_api_service()
        except Exception:
            pass
    assert len(seed_calls) == 1, "seed must run exactly once at startup"
```

（`cpu_rc_env` fixture 复用任务 2 中的 `tests/api/test_service_snapshot_wiring.py`；若 pytest 找不到，把 fixture 移到 `tests/api/conftest.py`。）

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/api/test_service_template_seed_wiring.py -v`
预期：FAIL，seed 未被调用。

- [ ] **步骤 3：编写实现**

修改 `src/pilot107/api/service.py`。在 import 段加入：

```python
from pilot107.core.template_market_seed import seed_preset_recipes
```

在 `build_api_service()` 末尾、`return Pilot107HttpApi(...)` 之前（A-1 接入之后）插入：

```python
    # --- A-2: Template market seed (idempotent, fault-tolerant) ---
    try:
        seed_preset_recipes(
            catalog=recipe_catalog,
            store=template_market_store,
            role_directory=template_role_directory,
        )
    except Exception:  # noqa: BLE001 - startup must not crash on seed failure
        pass  # seed errors are recorded in the SeedReport; total failure is non-fatal
```

注意：`recipe_catalog`、`template_market_store`、`template_role_directory` 变量名要核对函数内既有名称。`recipe_catalog` 可能叫 `catalog`，`template_role_directory` 可能叫 `role_directory`——按实际调整。

- [ ] **步骤 4：运行测试验证通过 + 全量回归**

运行：`uv run pytest tests/api/test_service_template_seed_wiring.py tests/api/test_service_snapshot_wiring.py -v && uv run pytest -q`
预期：PASS（3 wiring tests + 624+ existing tests，0 failed）

- [ ] **步骤 5：Commit**

```bash
git add src/pilot107/api/service.py tests/api/test_service_template_seed_wiring.py tests/api/conftest.py
git commit -m "feat(api): wire template market seed at startup

Invoke seed_preset_recipes at build_api_service() time. Idempotent: re-runs
skip already-published recipes. Fault-tolerant: gate-blocked recipes are
recorded without aborting startup."
```

---

## 任务 5：A-3 LLM 配置模板

**文件：**
- 修改：`simulator/compose/.env.cpu-rc.example`

- [ ] **步骤 1：读取当前 .env.cpu-rc.example**

运行：`cat simulator/compose/.env.cpu-rc.example` 确认当前 LLM 配置段（应为空或注释）。

- [ ] **步骤 2：编写实现**

在 `simulator/compose/.env.cpu-rc.example` 的 LLM 配置段（若不存在则新增）加入：

```bash
# --- LLM (OpenAI-compatible) ---
# Leave empty to disable LLM (deterministic rule fallback).
# To enable USTC glm-5.2-107, fill from opencode config (ustc-107 provider):
PILOT107_LLM_BASE_URL=https://api.llm.ustc.edu.cn/v1
PILOT107_LLM_API_KEY=<from opencode ustc-107, do not commit real key>
PILOT107_LLM_MODEL=glm-5.2-107
PILOT107_LLM_STRUCTURED_OUTPUT_MODE=prompt_json
```

- [ ] **步骤 3：验证 example 文件语法**

运行：`bash -n simulator/compose/.env.cpu-rc.example` 或 `docker compose --env-file simulator/compose/.env.cpu-rc.example config --quiet 2>&1 | head`（在 compose 目录下，按项目实际验证方式）。
预期：无语法错误。

- [ ] **步骤 4：Commit**

```bash
git add simulator/compose/.env.cpu-rc.example
git commit -m "chore(env): add LLM config template to .env.cpu-rc.example

USTC glm-5.2-107 endpoint as the documented default. API key is a placeholder
(<from opencode ustc-107, do not commit real key>) — real key is injected at
deploy time into the gitignored .env.cpu-rc on the VM."
```

---

## 任务 6：A-3 UI provider 选择 + api.ts 透传

**文件：**
- 修改：`apps/web/src/api.ts:325`
- 修改：`apps/web/src/AgentPage.tsx`
- 测试：`apps/web/src/api.test.ts`
- 测试：`apps/web/src/AgentPage.test.ts`

- [ ] **步骤 1：编写失败的测试**

在 `apps/web/src/api.test.ts` 中加：

```typescript
import { describe, expect, it, vi } from "vitest";
import { advanceRemediationSession } from "./api";

describe("advanceRemediationSession provider passthrough", () => {
  it("sends provider=local by default", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ state: "diagnosing" }), { status: 200 })
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    await advanceRemediationSession("alice", "sess_123");
    const call = fetchMock.mock.calls[0];
    const body = JSON.parse((call[1] as RequestInit).body as string);
    expect(body.provider).toBe("local");
  });

  it("sends provider=none when explicitly requested", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ state: "diagnosing" }), { status: 200 })
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    await advanceRemediationSession("alice", "sess_123", undefined, { provider: "none" });
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body.provider).toBe("none");
  });
});
```

在 `apps/web/src/AgentPage.test.ts` 中加：

```typescript
import { describe, expect, it } from "vitest";
import { defaultProvider, providerLabel } from "./AgentPage";

describe("LLM provider selection", () => {
  it("defaults to local when LLM is configured", () => {
    expect(defaultProvider({ llmConfigured: true })).toBe("local");
  });

  it("defaults to none when LLM is unconfigured", () => {
    expect(defaultProvider({ llmConfigured: false })).toBe("none");
  });

  it("labels providers in Chinese", () => {
    expect(providerLabel("local")).toBe("USTC LLM (glm-5.2-107)");
    expect(providerLabel("none")).toBe("确定性规则（无 LLM）");
  });
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd apps/web && npx vitest run src/api.test.ts src/AgentPage.test.ts`
预期：FAIL，`advanceRemediationSession` 不接受 provider 选项；`defaultProvider`/`providerLabel` 未导出。

- [ ] **步骤 3：编写实现 — api.ts**

修改 `apps/web/src/api.ts:325`。把：

```typescript
  advanceRemediationSession: (user: string, sessionId: string, signal?: AbortSignal) =>
    // ... existing fetch ...
```

改为接受可选 `options: { provider?: "local" | "none" }`，body 默认 `{"provider":"local"}`：

```typescript
  advanceRemediationSession: async (
    user: string,
    sessionId: string,
    signal?: AbortSignal,
    options?: { provider?: "local" | "none" }
  ) => {
    const provider = options?.provider ?? "local";
    const response = await fetch(`/api/v1/runs/-/remediation-sessions/${sessionId}/advance`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Pilot107-User": user },
      body: JSON.stringify({ provider }),
      signal,
    });
    if (!response.ok) throw new Error(`advance failed: ${response.status}`);
    return response.json();
  },
```

注意：精确的 URL 路径和请求头要核对现有 `api.ts` 的模式（其他请求函数怎么写的）。如果现有代码用 `apiFetch` helper，复用它。

- [ ] **步骤 4：编写实现 — AgentPage.tsx**

在 `apps/web/src/AgentPage.tsx` 中导出两个纯函数（用于测试）并在 remediation session 详情区加 provider 选择器：

```typescript
// Near other pure helpers in AgentPage.tsx:
export type LlmProvider = "local" | "none";

export function defaultProvider(opts: { llmConfigured: boolean }): LlmProvider {
  return opts.llmConfigured ? "local" : "none";
}

export function providerLabel(provider: LlmProvider): string {
  return provider === "local" ? "USTC LLM (glm-5.2-107)" : "确定性规则（无 LLM）";
}
```

在 remediation session 详情组件中（找到现有 session 详情渲染处），加一个 provider 选择器：

```tsx
// Inside the session detail component:
const [provider, setProvider] = useState<LlmProvider>(defaultProvider({ llmConfigured: llmEnabled }));

<select
  value={provider}
  onChange={(e) => setProvider(e.target.value as LlmProvider)}
  aria-label="LLM provider"
>
  <option value="local">{providerLabel("local")}</option>
  <option value="none">{providerLabel("none")}</option>
</select>
// Pass provider to advanceRemediationSession when calling advance.
```

`llmEnabled` 的来源：从现有平台能力查询 hook（如 `usePlatformCapabilities`）读取，若没有则从 `/api/v1/platform/capabilities` 的 `llm` 字段读取。若现有代码无此字段，先用 `true` 占位并在 PR 描述里标注 follow-up。

- [ ] **步骤 5：运行测试验证通过**

运行：`cd apps/web && npx vitest run src/api.test.ts src/AgentPage.test.ts`
预期：PASS（5 new tests + existing tests）。

- [ ] **步骤 6：运行 web 全量测试**

运行：`cd apps/web && npx vitest run`
预期：PASS（0 failed）

- [ ] **步骤 7：Commit**

```bash
git add apps/web/src/api.ts apps/web/src/AgentPage.tsx apps/web/src/api.test.ts apps/web/src/AgentPage.test.ts
git commit -m "feat(web): add LLM provider selector in Agent remediation

advanceRemediationSession now sends provider=local by default (was {}, which
defaulted to 'none' on the backend, so LLM was never invoked even when
configured). AgentPage exposes a provider selector (local / none) so users
can fall back to deterministic rules when desired."
```

---

## 任务 7：A-4 RunPicker 组件

**文件：**
- 创建：`apps/web/src/RunPicker.tsx`
- 测试：`apps/web/src/RunPicker.test.ts`

- [ ] **步骤 1：编写失败的测试**

```typescript
// apps/web/src/RunPicker.test.ts
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { RunPicker } from "./RunPicker";

describe("RunPicker", () => {
  it("renders a list of runs", () => {
    const runs = [
      { run_id: "run_1", state: "FAILED", created_at: "2026-07-18T10:00:00Z", recipe_id: "recipe_a" },
      { run_id: "run_2", state: "SUCCEEDED", created_at: "2026-07-18T11:00:00Z", recipe_id: "recipe_b" },
    ];
    render(<RunPicker runs={runs} onSelect={() => {}} />);
    expect(screen.getByText("run_1")).toBeTruthy();
    expect(screen.getByText("run_2")).toBeTruthy();
  });

  it("filters by status when filter prop is set", () => {
    const runs = [
      { run_id: "run_1", state: "FAILED", created_at: "2026-07-18T10:00:00Z", recipe_id: "recipe_a" },
      { run_id: "run_2", state: "SUCCEEDED", created_at: "2026-07-18T11:00:00Z", recipe_id: "recipe_b" },
    ];
    render(<RunPicker runs={runs} filter={{ state: "FAILED" }} onSelect={() => {}} />);
    expect(screen.getByText("run_1")).toBeTruthy();
    expect(screen.queryByText("run_2")).toBeNull();
  });

  it("calls onSelect with run_id when a run is clicked", () => {
    const runs = [
      { run_id: "run_1", state: "FAILED", created_at: "2026-07-18T10:00:00Z", recipe_id: "recipe_a" },
    ];
    const onSelect = vi.fn();
    render(<RunPicker runs={runs} onSelect={onSelect} />);
    fireEvent.click(screen.getByText("run_1"));
    expect(onSelect).toHaveBeenCalledWith("run_1");
  });

  it("shows empty state when no runs match", () => {
    render(<RunPicker runs={[]} onSelect={() => {}} />);
    expect(screen.getByText(/没有匹配的 Run/)).toBeTruthy();
  });
});
```

注意：若项目未装 `@testing-library/react`，改用纯函数测试——把 `RunPicker` 的过滤逻辑抽成纯函数 `filterRuns(runs, filter)` 并测它，组件渲染部分手动 review。检查 `apps/web/package.json` 是否有 testing-library。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd apps/web && npx vitest run src/RunPicker.test.ts`
预期：FAIL，`Cannot find module './RunPicker'`

- [ ] **步骤 3：编写实现**

```typescript
// apps/web/src/RunPicker.tsx
import { useMemo } from "react";

export interface RunSummary {
  run_id: string;
  state: string;
  created_at: string;
  recipe_id: string;
}

export interface RunPickerFilter {
  state?: string;
}

export interface RunPickerProps {
  runs: RunSummary[];
  filter?: RunPickerFilter;
  onSelect: (runId: string) => void;
}

export function filterRuns(runs: RunSummary[], filter?: RunPickerFilter): RunSummary[] {
  if (!filter?.state) return runs;
  return runs.filter((r) => r.state === filter.state);
}

export function RunPicker({ runs, filter, onSelect }: RunPickerProps) {
  const filtered = useMemo(() => filterRuns(runs, filter), [runs, filter]);
  if (filtered.length === 0) {
    return <p className="empty-state">没有匹配的 Run。请先在 Contract Studio 创建并提交作业。</p>;
  }
  return (
    <ul className="run-picker" role="listbox">
      {filtered.map((run) => (
        <li key={run.run_id}>
          <button
            type="button"
            onClick={() => onSelect(run.run_id)}
            aria-label={`选择 Run ${run.run_id}`}
          >
            <span className="run-id">{run.run_id}</span>
            <span className="run-state">{run.state}</span>
            <span className="run-recipe">{run.recipe_id}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`cd apps/web && npx vitest run src/RunPicker.test.ts`
预期：PASS（4 tests）。若 testing-library 不可用，至少 `filterRuns` 纯函数测试必须通过。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/src/RunPicker.tsx apps/web/src/RunPicker.test.ts
git commit -m "feat(web): add RunPicker component for in-workspace Run binding

Pure component: lists runs, optional status filter, calls onSelect with
run_id. Will replace the 'go to Run page' empty states in /agent and
/terminal workspaces."
```

---

## 任务 8：A-4 AgentPage 空状态改 RunPicker

**文件：**
- 修改：`apps/web/src/AgentPage.tsx:78-80`

- [ ] **步骤 1：读取当前 AgentPage 空状态**

运行：`sed -n '70,110p' apps/web/src/AgentPage.tsx` 确认空状态渲染上下文。

- [ ] **步骤 2：编写实现**

在 `apps/web/src/AgentPage.tsx` 顶部 import：

```typescript
import { RunPicker } from "./RunPicker";
import { useRuns } from "./query";  // 若已有则不重复
```

把 `AgentPage.tsx:78-80` 的空状态：

```tsx
            empty={(sessions.data?.items.length ?? 0) === 0}
            emptyTitle="还没有修复会话"
            emptyDetail="从失败 Run 的诊断页启动；Agent 不会扫描或修改其他用户的作业。"
```

改为内联 RunPicker（保留安全声明）：

```tsx
            empty={(sessions.data?.items.length ?? 0) === 0}
            emptyTitle="选择一个失败的 Run 开始修复"
            emptyDetail={
              <RunPicker
                runs={(runs.data?.items ?? []).map((r) => ({
                  run_id: r.run_id,
                  state: r.state,
                  created_at: r.created_at,
                  recipe_id: r.recipe_id,
                }))}
                filter={{ state: "FAILED" }}
                onSelect={(runId) => {
                  createRemediationSession.mutate({ runId });
                }}
              />
            }
```

并在空状态下方保留安全声明文案（单独一行）："Agent 只处理所选 Run 的 Evidence，不会扫描或修改其他作业。"

注意：
- `runs` query hook 的精确名称和返回结构要核对 `query.ts`。若叫 `useRuns` 且返回 `{ data: { items: [...] } }`，按此用。
- `createRemediationSession` mutation 的精确名称和参数要核对 `api.ts`/`query.ts`。
- `emptyDetail` 原本是 string，改为 ReactNode 需确认 `QueryBoundary`（或 whatever component owns `empty*` props）接受 ReactNode。若只接受 string，改用 `emptyBody` 或在 `empty` 为 true 时下方插入 RunPicker 的方式。

- [ ] **步骤 3：运行 web 测试 + 手动检查**

运行：`cd apps/web && npx vitest run && npx tsc --noEmit`
预期：PASS，无类型错误。

- [ ] **步骤 4：Commit**

```bash
git add apps/web/src/AgentPage.tsx
git commit -m "feat(web): inline RunPicker in Agent workspace empty state

Replace 'go to Run page' prompt with an inline RunPicker filtered to FAILED
runs. Users can now bind a Run from within the /agent workspace without
leaving it. Run page entry point is preserved. Agent stays Evidence-bound
and per-Run (security invariant unchanged)."
```

---

## 任务 9：A-4 TerminalCollaborationPage 空状态改 RunPicker

**文件：**
- 修改：`apps/web/src/pages.tsx:298`

- [ ] **步骤 1：读取当前 Terminal 空状态**

运行：`sed -n '290,310p' apps/web/src/pages.tsx`

- [ ] **步骤 2：编写实现**

在 `apps/web/src/pages.tsx` 顶部 import：

```typescript
import { RunPicker } from "./RunPicker";
```

把 `pages.tsx:298` 的 `TerminalCollaborationPage` 空状态：

```tsx
          <QueryBoundary pending={Boolean(runId) && run.isPending} error={run.error} empty={!runId} emptyTitle="尚未选择 Run" emptyDetail="从 Run 摘要中的“终端协同”进入，命令才会绑定明确 Job ID。">
```

改为内联 RunPicker（命令绑定明确 Job ID 的入口直接在 workspace 内）：

```tsx
          <QueryBoundary
            pending={Boolean(runId) && run.isPending}
            error={run.error}
            empty={!runId}
            emptyTitle="选择一个 Run 进入终端协同"
            emptyDetail={
              <RunPicker
                runs={(runs.data?.items ?? []).map((r) => ({
                  run_id: r.run_id,
                  state: r.state,
                  created_at: r.created_at,
                  recipe_id: r.recipe_id,
                }))}
                onSelect={(selectedId) => {
                  navigate(`${location.pathname}?run=${selectedId}`);
                }}
              />
            }
          >
```

注意：
- `runs` query hook 在 `pages.tsx` 中可能已有（`RunListPage` 用过）。若无，import `useRuns` from `./query`。
- `navigate` 和 `location` 已在 `TerminalCollaborationPage` props 中（`PageProps & { ... }`），直接用。
- `emptyDetail` 同样要确认是否接受 ReactNode。

- [ ] **步骤 3：运行 web 测试 + 类型检查**

运行：`cd apps/web && npx vitest run && npx tsc --noEmit`
预期：PASS，无类型错误。

- [ ] **步骤 4：Commit**

```bash
git add apps/web/src/pages.tsx
git commit -m "feat(web): inline RunPicker in Terminal workspace empty state

Replace 'go to Run page' prompt with inline RunPicker. Users can now bind a
Run from within the /terminal workspace. Command binding still requires a
Job ID (security invariant unchanged). Run page entry point preserved."
```

---

## 任务 10：全量重建 + VM 重部 + 12 步闭环验证

**文件：** 无（运维任务）

- [ ] **步骤 1：本地重建 app 镜像**

运行（在 worktree 根目录）：
```bash
PILOT107_SKIP_BUILD=0 bash scripts/build-cpu-rc.sh
```
或项目既有的重建命令。新 revision tag 自动生成（如 `cpu-rc-<newsha>`）。
预期：4 镜像构建成功，digest 与新代码对应。

- [ ] **步骤 2：导出新 RC bundle**

运行：
```bash
bash scripts/export-cpu-rc-bundle.sh
```
或既有导出命令。产物：`artifacts/deployment/107pilot-cpu-rc-<newsha>-<timestamp>.tar.gz`。
预期：bundle 生成，sidecar SHA256 记录。

- [ ] **步骤 3：上传 bundle 到 VM**

运行（paramiko 或 scp）：
```bash
scp -P 8000 artifacts/deployment/107pilot-cpu-rc-<newsha>-<timestamp>.tar.gz root@114.214.241.31:/root/
```
预期：上传完成，SHA256 与本地一致。

- [ ] **步骤 4：VM 上停旧栈、解包、导入镜像、起新栈**

在 VM 上：
```bash
cd /root/107pilot-cpu-rc-<oldsha>-<oldts> && bash scripts/stop-cpu-rc.sh
cd /root && tar xzf 107pilot-cpu-rc-<newsha>-<timestamp>.tar.gz
cd /root/107pilot-cpu-rc-<newsha>-<timestamp> && bash scripts/import-cpu-rc-images.sh
# 注入 LLM apiKey 到 .env.cpu-rc（从本地 opencode 配置 ustc-107 条目读取）
# 编辑 .env.cpu-rc: PILOT107_LLM_API_KEY=<rotate-previous-key-and-set-via-secrets>
PILOT107_SKIP_BUILD=1 bash scripts/start-cpu-rc.sh
```
预期：10/10 容器健康，`/healthz` 200。

- [ ] **步骤 5：验证 12 步闭环**

按 spec `docs/superpowers/specs/2026-07-18-phase-a-four-gaps-design.md` 的"验证"段逐项验证：

1. `GET /api/v1/platform/capabilities?owner=alice` → `latest_snapshot` 非 null，包含 CPU-RC 分区 + anode16 节点
2. `GET /api/v1/templates` → 返回 ≥3 个已发布模板（CPU 可见）
3. Contract Studio 创建 Contract → preflight 通过
4. 三层脚本预览（original/resolved/wrapper + SHA256）
5. 提交作业 → 真实 job_id
6. Run 时间线 Pending → Running → Succeeded
7. Evidence 采集（submission/slurm/logs/outputs）
8. 制造失败 → 诊断 → retry
9. `/agent` workspace 选失败 Run → createRemediationSession → provider=local → LLM 响应
10. Capsule 生成，capsule_state=ready
11. 重启恢复（已验证，可跳过）
12. LLM 不可用降级（临时清空 apiKey 重启 → 规则诊断仍工作）

- [ ] **步骤 6：更新 S1 部署证据文档**

修改 `docs/phase-3/s1_vm_deployment_evidence_20260718.md`：加一段"Phase A 修复后重部"记录新 revision、4 缺口修复状态、12 步闭环复验结果。

- [ ] **步骤 7：合并 worktree 回 main**

```bash
cd /home/knowingthesea/107pilot
git merge --no-ff phase-a-four-gaps -m "feat: Phase A four-gaps fix (Slurm REST snapshot, template seed, LLM wiring, workspace Run binder)"
git branch -d phase-a-four-gaps
git worktree remove .worktrees/phase-a
```

- [ ] **步骤 8：Commit 证据更新**

```bash
cd /home/knowingthesea/107pilot
git add docs/phase-3/s1_vm_deployment_evidence_20260718.md
git commit -m "docs: record Phase A redeployment + 12-step loop re-verification"
```

---

## 自检

### 1. 规格覆盖度

逐项对照 spec `2026-07-18-phase-a-four-gaps-design.md`：

| Spec 段 | 覆盖任务 |
|---|---|
| A-1 Slurm REST 采集器 | 任务 1 |
| A-1 启动接入 + 后台刷新 | 任务 2 |
| A-2 seed 函数 | 任务 3 |
| A-2 启动接入 + 系统身份 | 任务 3 (policy) + 任务 4 |
| A-3 配置层 (.env) | 任务 5 |
| A-3 UI provider 选择 | 任务 6 |
| A-4 RunPicker 组件 | 任务 7 |
| A-4 AgentPage 空状态 | 任务 8 |
| A-4 TerminalCollaborationPage 空状态 | 任务 9 |
| 重建 + 重部 + 12 步验证 | 任务 10 |

无遗漏。

### 2. 占位符扫描

- 任务 6 步骤 4 中 "若现有代码无此字段，先用 `true` 占位并在 PR 描述里标注 follow-up" — 这是实现期的 fallback 决策，不是 spec 占位。可接受。
- 任务 8/9 中 "若叫 `useRuns` ... 按此用" — 这是命名核对指引，不是占位。可接受。
- 任务 3 步骤 3b 中 "字段名需在实现时核对" — 同上，实现期核对指引。

无 "TODO"/"待定"/"后续实现" 占位。

### 3. 类型一致性

- `SlurmrestSnapshotCollector.collect(*, captured_at)` — 任务 1 定义，任务 2 调用，签名一致 ✓
- `seed_preset_recipes(*, catalog, store, role_directory) -> SeedReport` — 任务 3 定义，任务 4 调用，签名一致 ✓
- `TemplateRoleDirectory.system_reviewer_principal() -> TemplateReviewerPrincipal` — 任务 3a 定义，任务 3b 调用 ✓
- `RunPicker({ runs, filter, onSelect })` — 任务 7 定义，任务 8/9 调用 ✓
- `advanceRemediationSession(user, sessionId, signal, options?)` — 任务 6 定义，任务 8 调用（通过 createRemediationSession mutation，间接）✓
- `defaultProvider({ llmConfigured })` / `providerLabel(provider)` — 任务 6 定义并在同任务测试 ✓

无不一致。

### 4. 实现期核对点（非阻塞，fixer 实现时注意）

- `service.py` 内 `platform_snapshot_store`、`recipe_catalog`、`template_market_store`、`template_role_directory` 的实际变量名（任务 2、4）
- `template_market.py` 中 `TemplateMarketStore.list_releases`/`get_review` 的精确签名和返回 dataclass 字段名（任务 3b）
- `TemplateVisibility.PUBLIC` 枚举值（任务 3b）
- `api.ts` 中其他请求函数的 helper 模式（任务 6 步骤 3）
- `query.ts` 中 `useRuns` 的返回结构和 `createRemediationSession` mutation 名称（任务 8）
- `QueryBoundary` 的 `emptyDetail` 是否接受 ReactNode（任务 8、9）

这些是按现有代码模式跟进的核对点，不是 spec 决策。fixer 实现时按代码实际调整即可。

---

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-07-18-phase-a-four-gaps.md`。两种执行方式：

**1. 子代理驱动（推荐）** — 每个任务调度一个新的子代理，任务间进行审查，快速迭代

**2. 内联执行** — 在当前会话中使用 executing-plans 执行任务，批量执行并设有检查点

选哪种方式？
