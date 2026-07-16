# Parallel Execution Plan

> 状态：coordination plan  
> 日期：2026-07-15  
> 目的：允许 Docker simulator 重构和 107Pilot 系统侧事实接入并行推进，同时避免把旧 simulator 偏差固化进产品逻辑。

## 1. 当前原则

本轮可以并行，但顺序关系不能颠倒：

- Docker simulator 是行为基准，优先复现 Slurm 版本、REST/JWT、account/QOS/partition、作业生命周期、日志和权限拒绝。
- 107Pilot 系统侧可以先接入官方文档/PDF 中稳定的行为事实，但只能做来源化、结构化、解析和证据模型修正。
- 107Pilot 不应在 simulator 行为稳定前写死具体 CPU/GPU/内存/walltime 数值。
- 真实 SSH/CLI 权限到账后只作为只读校验 lane，不作为产品长期依赖。

## 2. 工作流拆分

### A. Docker Simulator Worker

写入范围：

- `config/platform_profiles/**`
- `simulator/compose/**`
- `simulator/images/slurm/**`
- `scripts/apply-sim-real107-profile.sh`
- `scripts/smoke-sim-real107-profile.sh`
- `scripts/check-slurm-sim-image.sh`
- `tests/test_simulator_profile_config.py`

交付目标：

- 新增 `config/platform_profiles/simulator-real107-behavior.yaml`。
- 让 simulator 配置、初始化脚本、smoke tests 以该 profile 为单一事实源或显式校验源。
- 验证普通用户、受限用户、越权 QOS、合法/非法 partition/QOS 行为。
- 保留 25.11 target image 的 manifest/check 入口，但不在本轮强行完成大镜像构建。

### B. 107Pilot System Worker

写入范围：

- `src/pilot107/core/platform_snapshot.py`
- `src/pilot107/adapters/platform_parsers.py`
- `src/pilot107/adapters/platform_cli.py`
- `src/pilot107/services/platform_snapshot_service.py`
- 平台快照相关测试。

交付目标：

- 增强 `ObservedValue` / `PlatformSnapshot` 对来源、可用性、限制和 runtime limitation 的表达。
- 增强 Slurm CLI parser 对 `AllowAccounts`、`AllowQos`、`MaxTime`、`TRES`、Reason、原始状态/归一化状态的覆盖。
- 把“登录节点无 GPU 不等于 GPU 分区不可用”“GPU runtime limitation 必须显式表达”“默认值不可互相覆盖”等官方行为放进模型或测试。
- 不修改 simulator，不写死真实平台数值。

### C. Coordinator

职责：

- 维护执行顺序、文件边界和集成闸门。
- 审阅两个 worker 的改动是否互相依赖或冲突。
- 合并后运行统一验证。
- 将 simulator 行为报告反向接入 107Pilot 的后续 preflight、evidence、diagnosis 计划。

## 3. 集成闸门

任一阶段合并前至少满足：

- `config/platform_profiles/simulator-real107-behavior.yaml` 是 simulator 行为的唯一新事实入口。
- simulator 代表性数值不得与 SlurmDBD/QOS/partition 行为自相矛盾。
- 107Pilot 对未观测事实使用 `unknown`、`unavailable`、`permission_denied` 或 `unsupported`，不能用空字符串表示。
- REST/API 版本必须以观测值记录，不能把 23.11 fallback 伪装成 25.11。
- GPU scheduler fidelity 与 runtime GPU fidelity 分开声明。
- 登录节点事实与计算节点 runtime facts 分开保存。

## 4. 建议验证命令

文档/单元层：

```text
PYTHONPATH=src uv run pytest tests/test_platform_parsers.py tests/test_platform_snapshot_service.py tests/test_platform_cli.py tests/test_simulator_profile_config.py
PYTHONPATH=src uv run ruff check src tests scripts/real107_probe/probe_real107_cli_snapshot.py scripts/real107_probe/probe_real107_snapshot.py
bash -n scripts/apply-sim-real107-profile.sh scripts/smoke-sim-real107-profile.sh scripts/start-sim-core.sh
```

Docker 层：

```text
bash scripts/build-slurm-sim-image.sh
bash scripts/check-slurm-sim-image.sh
bash scripts/start-sim-core.sh
bash scripts/smoke-sim-real107-profile.sh
```

Docker 层如受本地权限、网络或镜像构建时间限制无法运行，必须在最终报告中明确说明。

## 5. 下一步收敛

并行结果合并后，下一轮按以下顺序推进：

1. 先让 simulator profile 和 SlurmDBD/QOS/association 行为稳定。
2. 再让 `PlatformSnapshot` 从 simulator 和真实只读 CLI 生成同构快照。
3. 然后修正 107Pilot preflight，使其结果与 simulator Slurm 接受/拒绝行为一致。
4. 最后扩展 evidence timeline 和 diagnosis fixtures。
