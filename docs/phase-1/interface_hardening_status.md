# Phase 1 接口硬化状态

> 状态：started, real107 probe ingested, CapabilityProfile first implementation, REST 专项收敛 complete  
> 日期：2026-07-12（最近更新：2026-07-13 REST 专项收敛）  
> 前置决策：真实 VM 验证暂跳过；比赛版主线继续在本地 competition profile 上推进。

## 已完成

- 将 API 层内联身份对象迁移为核心 `UserIdentity`：
  - `IdentityMode.TRUSTED_HEADER`；
  - `AUTH.MISSING`；
  - `AUTH.FORBIDDEN`；
  - 大小写不敏感 trusted header 解析；
  - 用户名安全字符集校验。
- 保持现有 HTTP API 行为不变：
  - 未认证访问仍按 `auth_required` 决定是否允许；
  - body `owner` 不能覆盖可信身份；
  - Contract、Run、Cancel、Evidence 仍按 owner 访问控制。
- 将设计文档中的 `SlurmControlBackend` 对齐到现有 Slurm backend Protocol。
- 新增 Slurm 控制面契约测试：
  - submit；
  - idempotency replay；
  - get_job owner 校验；
  - cross-user read 拒绝；
  - cancel。
- 新增 command gateway 安全测试：
  - bearer token 校验；
  - 禁止 `sh -c`；
  - 结构化 argv 不启用 shell；
  - 拒绝相对路径和 NUL；
  - 拒绝越界写入。
- 新增最小 `ConfigurationSnapshot`：
  - `ClusterProfile`；
  - `UserEntitlementProfile`；
  - `EndpointSet`；
  - `SourceAuthority`；
  - Docker simulator competition baseline。
- 新增 `EvidenceTransport` 契约和授权文件系统实现：
  - `EvidencePolicy`；
  - `EvidenceCapability`；
  - `EvidenceRoot`；
  - `FileStat`；
  - `TextTail`；
  - `OutputInventory`；
  - `AuthorizedFilesystemEvidenceTransport`。
- 新增 EvidenceTransport contract tests：
  - capability probe；
  - prepare run root；
  - stat；
  - read text tail；
  - read bytes range；
  - inventory policy；
  - symlink escape 拒绝；
  - transport 二次授权。
- 新增 CollectionTask 聚合语义测试：
  - retryable failure → `collection_state=degraded`；
  - permanent failure → `collection_state=failed`。
- 新增架构门禁测试：
  - 业务层不得直接调用 `docker exec`；
  - Docker 命令边界保留在 Slurm adapter。
- 新增 Worker 错误分类与认证过期语义：
  - `WorkerErrorCode.AUTH_EXPIRED`；
  - `WorkerErrorCode.AUTH_REQUIRED`；
  - `WorkerErrorCode.AUTH.FORBIDDEN`；
  - `WorkerErrorCode.SLURM.BACKEND_ERROR`；
  - `WorkerErrorCode.EVIDENCE.COLLECTION_ERROR`。
- Worker 对账错误会写入 `worker.run_error` 事件。
- CollectionTask 失败事件会记录：
  - `error_code`；
  - `retryable`；
  - `auth_required`。
- Command gateway 新增请求追踪和审计：
  - 生成或复用安全 `X-Request-Id`；
  - 响应体和响应头返回 request id；
  - JSONL audit log；
  - 审计记录不写入 bearer token、stdin 或文件内容；
  - 记录命令名、参数数量、用户、路径、状态码、错误摘要和耗时。
- Command gateway 新增固定窗口基础限流：
  - gateway 程序默认 `1200` requests / `60` seconds / client；
  - competition profile 默认提升为 `6000` requests / `60` seconds / client；
  - 超限返回 `429`。
- 新增真实 107 只读 `ConfigurationSnapshot` probe 包：
  - 自包含 Python probe；
  - Slurm `sbatch` 任务模板；
  - 打包脚本；
  - 离线 fake REST 测试；
  - 输出 `configuration_snapshot.json` 和 `probe_report.json`；
  - 只调用 HTTP GET，不做 submit/cancel，不保存 token。
- 已回收真实 107 probe 结果：
  - job id `21039`；
  - `summary.status=partial`；
  - REST `v0.0.41`；
  - OpenAPI `3.0.3`；
  - Slurm `25.11.2`；
  - `ping`、`nodes`、`jobs`、`openapi` 成功；
  - `partitions` 因 SlurmDBD/TRES 查询 `Connection refused` 返回 HTTP 500，但 payload 中仍包含 7 个分区摘要；
  - 用户 home/allowed root 确认为 `/public/home/pb23061276`。
- 新增 `CapabilityProfile` ingest 第一版：
  - 默认 competition profile 为 `docker-real107-sim`；
  - 支持从真实 probe 输出目录加载 `configuration_snapshot.json` 和 `probe_report.json`；
  - 保留 docs-main 的动态平台事实、学生 QoS 上限、共享/本地路径语义；
  - 保留真实 probe 的默认分区、AllowQos、REST endpoint、API version、OpenAPI digest 和 partial payload 语义；
  - 新增 `GET /api/v1/platform/capabilities`；
  - API service 支持 `PILOT107_CAPABILITY_PROFILE_PATH`；
  - Run prepare preflight 和 Contract `real107-sim` profile 已消费 `partition_qos()`。
- Issue A 诊断闭环第一版：
  - `diagnoses` 表；
  - `DiagnosisRecord`；
  - `RunStore.replace_diagnoses()` / `list_diagnoses()`；
  - `DiagnosisContextBuilder` 从已索引 EvidenceObject 读取白名单小片段；
  - `DiagnosisService` 统一 API 和 Worker 诊断入口；
  - 规则引擎覆盖 Slurm QoS/partition、command not found、Python package missing、timeout、OOM、nonzero exit；
  - `GET /api/v1/runs/{run_id}/diagnoses`；
  - `POST /api/v1/runs/{run_id}/diagnose`；
  - Worker 在终态 Run 的 Evidence collection `succeeded/degraded` 后自动诊断；
  - Worker 重启后可扫描 `diagnosis_state=pending` 的已完成 Run。
- DockerSlurmEvidenceCollector 已完成第一段 `EvidenceTransport` 迁移：
  - 新增 `DockerVolumeEvidenceTransport`；
  - worker service 仅在 `PILOT107_ENABLE_DOCKER_VOLUME_EVIDENCE_TRANSPORT=1` 时显式启用 docker volume transport；
  - logs finalize 可通过 transport 做 stat/tail/hash；
  - outputs inventory 可通过 transport 做授权目录扫描；
  - 未配置 transport 时保留原命令式采集回退；
  - competition profile 已验证默认 command-gateway fallback，避免非 root 服务用户直接读取用户私有 Slurm 日志。
- Worker collection retry 已增加指数退避，避免 gateway rate limit 触发后同一批任务立即重试造成放大。
- Competition profile 已通过：
  - EvidenceTransport command fallback smoke；
  - success/failure/cancel 三类 Run 的 Evidence + Capsule smoke；
  - 100 并发 read/validate/prepare 承载测试。
- REST 专项收敛（2026-07-13 初版，2026-07-15 升级到 25.11 target）已完成：
  - simulator REST JWT auth 打通：target image 直接使用源构建 Slurm `25.11.2` 的 JWT 插件；`slurm.conf`/`slurmdbd.conf` 增 `AuthAltTypes=auth/jwt` + `AuthAltParameters=jwt_key=...`；`jwt_hs256.key` 烤入镜像（弃用 bind-mount）；slurmrestd 以非 root `pilot107` 用户运行并设置 `SLURM_JWT=daemon`；live 验证 `scontrol token` + `GET /slurm/v0.0.41/nodes` 返回 200（anode16/anode17，Slurm 25.11.2）。2026-07-13 的 Ubuntu 23.11/v0.0.40 结果仅保留为历史 fallback 记录。
  - REST 适配器契约测试：`tests/test_rest_native_backend.py` 27 个契约测试（6 矩阵 + 3 submit smoke + read/cancel/auth/语义分类），混合 in-process `ScriptedTransport` 与 real-socket `FakeSlurmRestServer`；确认 `RestNativeSlurmBackend`/`UrllibHttpTransport`/`RestAuthStyle.SLURM_HEADERS`/`check_slurm_rest_semantics` 已正确，无需硬化；确认 `SLURM_HEADERS` 同时发送 `X-SLURM-USER-NAME` + `X-SLURM-USER-TOKEN`，无 Bearer。
	  - token mint/refresh：`src/pilot107/adapters/rest_token.py` 新增 `SimulatorRestTokenProvider`（`scontrol token` 签发、按用户内存缓存、60s 刷新阈值、`threading.Lock` 线程安全、token 不入日志）、`StaticTokenProvider`、`RestTokenProvider` Protocol；`tests/test_rest_token.py` 16 个测试；`scripts/_sim_rest_helpers.py` 提供 `detect_sim_rest_url()` + `mint_sim_token()`；当前默认 API version 已随 25.11 target 更新为 `v0.0.41`。
  - live REST 矩阵：`scripts/smoke_sim_rest_live.sh` + `.py` 11 场景（read jobs/nodes/partitions/accounting、submit shared、get by id、cancel-terminal、invalid workdir、unwritable output、idempotency-no-dedupe）；`probe_sim_rest_auth.py` 使用真实 token + v0.0.41，报 supported；`probe_sim_rest_submit.py` 解 skip 走真实 submit/get/cancel。
  - real107 只读 REST smoke：`scripts/probe_real107_rest_readonly.py` + `.sh`，无 token 时安全跳过，计算 openapi_digest，token 不入输出。
  - OpenAPI digest 自动刷新：`src/pilot107/core/platform.py` 新增 `compute_openapi_digest`/`refresh_openapi_digest`/`refresh_configuration_snapshot_digest`/`refresh_rest_capability_digest`，token 不入 digest/errors；`tests/test_openapi_digest.py` 13 个测试。
  - WorkDirPreflight：`src/pilot107/core/preflight.py`（约 640 行）实现 `PathChecker` Protocol、`LocalPathChecker`、`preflight_workdir_paths`（纯函数）、`preflight_workdir_fs`（注入 FS），返回 `list[PreflightFinding]`，`WORKDIR_*` 代码；`tests/test_preflight.py` 26 个测试（`/tmp`/local-only/path-escape → BLOCK）。
  - idempotency 对账：`src/pilot107/core/submission_reconcile.py` 实现 `reconcile_submission` → `ReconcileResult`(bound/not_found/uncertain)，通过 marker + 时间窗口查询；`tests/test_submission_reconcile.py` 5 个测试。
	  - 服务接入：`src/pilot107/adapters/rest_token_backend.py` 实现 `TokenMintingRestBackend` 包装器（按用户 mint，不入 receipt/日志）+ `find_jobs_by_marker`；`src/pilot107/core/run_service.py` 接入 WorkDirPreflight + idempotency 对账 + 新错误类 `WorkDirPreflightError`/`SubmissionUncertainError`；`api/service.py` + `worker/service.py` 新增 `rest_token_provider_enabled`/`workdir_preflight_enabled`/`idempotency_reconcile_enabled` 标志；`tests/test_service_rest_wiring.py` 11 个测试；`simulator/compose/.env.example`/`.env.competition`/`.env.competition.example` 配置 `PILOT107_REST_AUTH_STYLE=slurm_headers`、`PILOT107_SLURM_API_VERSION=v0.0.41`、`PILOT107_REST_TOKEN_PROVIDER=1`。
- Docker simulator 25.11 重构（2026-07-15）已完成：
  - 默认 simulator image 切换为 source-built `pilot107/slurm-sim:25.11-real107`；
  - `scontrol --version`/report 观测 Slurm `25.11.2`，REST API 默认 `v0.0.41`；
  - `slurmrestd` 以非 root `pilot107` 用户运行，REST/JWT probe 为 supported；
  - SlurmDBD/QOS/account/user association 由 `simulator-real107-behavior.yaml` 驱动并在 smoke 中验证；
  - `slurmd` 节点 spool 不再共享，避免 batch completion 从错误节点返回；
  - target/fallback 镜像均提供 `python` -> `python3`，使官方 `python -V`/`which python` 快照命令在 Docker 中可执行；
  - 23.11 image 只保留为 compatibility fallback，不再用于 parity claim。
- 官方覆盖吸收复核（2026-07-15）已完成：
  - Platform Observation Layer 已落地 `ObservedValue`、`PlatformSnapshot`、allowlisted CLI collector、`scontrol`/`sinfo`/`squeue` 解析和登录节点 GPU runtime limitation；
  - Docker login-node 已用普通用户验证 `hostname`、`pwd`、`whoami`、`date -Is`、`python -V`、`which python`、`scontrol show part`、`sinfo`、`squeue -u alice` 可执行；
  - Evidence collector 在保留旧 `submission/*`/`environment/*` 路径的同时，新增官方建议路径 `run/request/resource-plan.json`、`run/request/submitted-script.sbatch`、`run/request/sbatch-argv.json`、`run/request/capability-profile-ref.json`、`run/environment/basic.json`、`run/environment/gpu.json`（GPU 请求时）和 `run/timeline/events.jsonl`；
  - `scripts/smoke-sim-evidence-transitions.sh` 已把新增 Evidence 路径和 Python runtime facts 纳入 Docker smoke 门禁；
  - 重跑行为报告 `simulator/reports/behavior-fidelity/2026-07-15T105002Z0000.json`：Slurm `25.11.2` target、REST `v0.0.41` supported，已知差异仅保留 GPU runtime/NVML 不可用和 Pending Reason fidelity 未覆盖。
- 官方覆盖下一切片设计（2026-07-15）已形成：
  - 新增 `docs/phase-1/official_coverage_next_slice_design.md`；
  - 明确下一步只做 compute-job runtime probe、`run/slurm/squeue-timeline.jsonl`、Pending Reason 去重和第一批多证据诊断；
  - 明确当前 collector-side runtime facts 不能冒充 compute-job facts，后续应由 sbatch wrapper 在用户脚本前写入。
- 已知错误库后端融入第一段（2026-07-13）已完成：
  - `DiagnosisService` 已从 `data/known_errors/*.yaml` 加载规则；
  - 保留现有 7 条规则的 `rule_id`、触发条件和 `suggested_patch` 向后兼容；
  - 支持 `symptoms` 子串/`regex:`、`terminal_state_match` 和 `state_match`；
  - `DiagnosisRecord` 增加可选 `category`、`stage`、`fix_guide` 并做 SQLite 非破坏性列迁移；
  - `GET /api/v1/diagnosis/known-errors` 和 `GET /api/v1/diagnosis/known-errors/{error_id}` 已暴露错误库；
  - Agent deterministic facts 已纳入 `fix_guide` 的 fix/prevention/automation。
- 已知错误库数据层与优秀提交模板已补齐（2026-07-14）：
  - `data/known_errors/` 当前 27 条规则，覆盖旧 7 条、Wan–HiF4 记录（HF4-005 合并入 Python package missing，HF4-007 保留既有规则）和 107 特化模式；
  - 新增 `data/known_errors/INDEX.yaml`；
  - 新增 `data/submission_templates/recipe_student_cpu_basic.yaml`；
  - 新增 `data/submission_templates/recipe_student_gpu_array.yaml`；
  - 新增 `data/submission_templates/recipe_resilient_submission.yaml`；
  - 新增 `data/submission_templates/INDEX.yaml`；
  - 新增 `tests/test_submission_templates.py`。
- 前端已展示 `fix_guide`（2026-07-14）：
  - Run Diagnostics 条目展示 `category`、`stage`；
  - 展示 `fix_guide.fix` / `prevention` / `automation`；
  - UI mock 数据已覆盖新增字段；
  - Playwright UI 回归通过。
- 已新增已知错误库与优秀提交模板总览文档：
  - `docs/phase-1/error_library.md`；
  - `docs/phase-1/submission_templates.md`。
- M1 两机部署脚本化入口已补齐（2026-07-14）：
  - `simulator/compose/compose.competition-slurm-host.yml`；
  - `simulator/compose/compose.competition-app-node.yml`；
  - `scripts/start-competition-slurm-host.sh` / `stop-competition-slurm-host.sh`；
  - `scripts/start-competition-app-node.sh` / `stop-competition-app-node.sh`；
  - `scripts/export-competition-bundle.sh` 已纳入新增数据目录、phase-1 文档、两机脚本和 compose override；
  - `docs/phase-0/competition_deployment_plan.md` 与 `vm_test_readiness.md` 已补两机执行路径。

## 尚未完成

- DockerSlurmEvidenceCollector 已完成 logs/outputs 第一段 `EvidenceTransport` 迁移；
  terminal accounting、environment probe 等命令语义仍保留在 executor 边界，整体迁移未完成。
- OpenAPI digest 自动刷新任务已接入（`refresh_openapi_digest` 等，2026-07-13 REST 收敛完成）；尚未在常驻调度/Worker 中以周期任务形式驱动，目前由 probe 脚本触发。
- Docker simulator 已切到 Slurm 25.11.2 target；残余限制是没有真实 GPU/CUDA/NVML runtime 与 GPU cgroup 绑定，fake GPU GRES 仅用于 scheduler behavior。
- 真实 107 submit/cancel/file read 仍未 probe，继续保持 M1-R 非阻塞（real107 只读 REST smoke 脚本已就绪）。
- REST 专项收敛遗留限制：唯一 job name marker 仍硬编码 `pilot107-run`（per-run 唯一 marker 暂缓）；command-gateway FS checker 待补（`LocalPathChecker` 仅供本机 FS）；live REST smoke 仍直连适配器，经 wired `rest-native` 服务后端的端到端 live 验证仅以 fake 单测覆盖；对账重试策略仅 `not_found` 重试一次，可配置 max-retry + backoff 为后续。
- 前端已消费真实 Diagnosis API；当前 Run Diagnostics 区块可展示 Rule ID、severity、summary、evidence refs、suggested patch、retryable 和 confidence。
- Agent explain API 已完成 `provider=none/campus` 第一版；`none` 为确定性解释，`campus` 使用 OpenAI-compatible 校园/USTC 网关，facts 仍只来自带 evidence refs 的 stored diagnoses。
- 前端已展示 Agent Explain；默认 `provider=none`，可切换 `campus`，Explain 仅由用户点击触发，不随轮询或 Evidence 刷新自动调用 LLM。
- 校园/USTC LLM 网关仍未在真实 key/model 环境下 smoke；当前 competition smoke 不调用 LLM。正式接入时可通过 `PILOT107_LLM_MODEL` 使用 qwen 系列模型。
## 下一步建议

REST 专项收敛已完成（2026-07-13，路径 A），simulator REST live submit/cancel/read 全通，`RestNativeSlurmBackend` 作为 env-gated 可选后端接入 competition profile（默认仍 command backend，竞赛演示无回归）。已知错误库后端与数据层、优秀模板已完成。M1 两机脚本化入口已补齐。下一步：

```text
真实两台 VM 网络/防火墙/证书实测
→ 前端设计包接入前的后端交互回归固化
```

REST 收敛遗留限制（详见 `docs/phase-1/rest_convergence_report.md` §5）按需在 M1-R 收口：唯一 job name marker、command-gateway FS checker、wired `rest-native` 端到端 live 验证、对账可配置重试、真实 107 submit/cancel/file read。
