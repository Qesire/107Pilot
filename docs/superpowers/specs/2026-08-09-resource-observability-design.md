# 107Pilot 平台动态状态与作业资源观测设计

- 日期：2026-08-09
- 状态：总体决策保留；实施级细化已由 `2026-08-10-resource-observability-detailed-design.md` 继承；实现未开始
- 环境边界：先在本地 Docker Slurm simulator 验证；远程 VM 当前不可用且不作为前置条件
- 本轮不包含：Dashboard 页面布局、视觉稿和前端组件实现

> 实施权威：本文件保存最初的范围、三级视角与方案选择；采集租约、类型化事实、accounting 归并、终态固化、API、Pi Agent 取证和本地验收以 [`2026-08-10-resource-observability-detailed-design.md`](2026-08-10-resource-observability-detailed-design.md) 为准。若两者冲突，后者优先。

## 1. 背景与结论

当前系统已经具备 Run 提交、状态对账、终态 Evidence、Diagnosis、克隆和 Capsule，但资源观测仍停留在平台快照摘要：

- Web 虽以约 20 秒频率查询，平台快照的实际采集默认约 5 分钟，不能等同实时动态；
- 终态 accounting 只采集基本身份、状态、耗时和分配 TRES，缺少 `TotalCPU`、`CPUTimeRAW`、`MaxRSS`、I/O 和可选 GPU 利用率；
- 没有稳定的短期时间序列、作业级资源总结和资源浪费判定；
- Agent 能解释 OOM、超时、依赖错误等异常，但没有可靠资源事实支撑 CPU、内存、GPU 和 walltime 优化建议。

本设计采用“Slurm 原生事实 + 107Pilot 有界缓存与规则层”的方案。它同时服务三个视角：集群全局、当前学生账号、107Pilot Run；浏览器只读取 107Pilot API，不直接访问 Slurm。

## 2. 账号和数据边界

比赛阶段，一个 107Pilot 账号视为某个学生本人的账号，并与该学生的 POSIX/Slurm 账号一一绑定：

```text
portal_owner 1:1 cluster_user
```

两者仍保留为不同字段，便于审计和未来身份接入，但禁止多个 portal 用户共享同一个真实学生账号。

观测分为三级：

1. **集群全局视角**：节点、分区、总容量、匿名队列聚合；不暴露其他用户的作业明细。
2. **学生账号视角**：该学生账号拥有的全部 Slurm 作业。
3. **107Pilot Run 视角**：Contract 请求、Slurm 分配、实际使用、Evidence 和建议。

学生账号下的作业按来源分类：

- `managed`：`connection_id + job_id` 能映射到 107Pilot Run；
- `external`：同一学生通过命令行等方式自行提交的作业，可展示 Slurm 事实，但不得伪造 Run、Contract 或 Evidence 关联；
- `unknown`：无法安全确认归属，只进入匿名聚合，不返回明细。

本地模拟器以 `alice` 作为比赛账号载体；`bob` 只用于越权、QoS 和负面测试。

## 3. 方案比较与选择

### 3.1 方案 A：请求时直接查询 Slurm

实现简单，但浏览器刷新会放大 Slurm 压力，响应延迟和失败会直接传到页面，也难以形成 24 小时趋势。拒绝。

### 3.2 方案 B：只接 Prometheus/Grafana

适合长期运维，但不能天然绑定 107Pilot Contract、Run、Evidence 和 Agent 建议；比赛环境也未必已有 exporter。只作为未来数据源，不作为首版唯一依赖。

### 3.3 方案 C：107Pilot 租约采集器 + 有界时间序列（采用）

每个集群连接最多一个持租约采集器，读取 Slurm 原生事实并写入有界缓存；API 提供 latest、series 和事件流。终态资源总结固化为长期 Evidence，短期动态样本按保留策略清理。

该方案可在本地模拟器完整验证，也允许以后把数据源替换为 Slurm 25.11 metrics 或 Prometheus recording rules，而不改变产品 API。

## 4. 采集架构

```text
Slurm CLI / REST / native metrics
          │
          ▼
per-connection leased collector
          │
          ├── platform/account bounded samples
          ├── active Run samples
          └── terminal Run accounting summary
                         │
                         ▼
              rule evaluator + Evidence binder
                         │
                         ▼
              Observability API / Agent facts
```

### 4.1 采集频率

- 平台快速采集：默认 20 秒，可配置；节点状态、CPU/GPU 分配、队列状态和 pending reason；
- 平台能力慢采集：默认 5 分钟；分区、QoS、容量、TRES 和能力变化；
- 活跃 Run：默认 30 秒；优先使用 `sstat`；
- 终态 Run：进入终态后使用 `sacct` 采集一次，必要时对 accounting 延迟做有限重试。

同一 `connection_id` 只能有一个有效 collector lease。浏览器轮询或 SSE 连接不得触发 Slurm 命令。

### 4.2 数据源优先级

1. 部署明确提供且语义通过 probe 的 Slurm 原生 metrics；
2. Slurm REST 可用端点；
3. 精确 allowlist 的 `sinfo`、`squeue`、`sstat`、`sacct`、`scontrol`；
4. 字段不可用时记录 unavailable，不用零值替代。

活跃作业以 `sstat` 为主，终态以 `sacct` 为准。不同来源的字段必须保留 source 和采集时间，不能静默拼成“精确实时值”。

### 4.3 保留策略

- 原始动态样本：2 小时；
- 1 分钟聚合：24 小时；
- 终态 `RunResourceSummary`：随 Run/Evidence 长期保留；
- 趋势降采样使用确定性聚合，避免前端对大量原始点临时计算。

## 5. 数据模型

### 5.1 通用测量值

每个可选测量值至少包含：

```json
{
  "value": 123.0,
  "unit": "MiB",
  "availability": "available",
  "source": "sacct",
  "captured_at": "2026-08-09T00:00:00Z",
  "freshness": "fresh",
  "quality_warning": null
}
```

`availability` 至少区分 `available`、`unsupported`、`not_collected` 和 `insufficient_coverage`。缺失 GPU 指标不得解释为 0% 利用率。

### 5.2 `PlatformDynamicSample`

包含：

- `connection_id`、`captured_at`、`source`、`freshness`；
- 节点按 `idle/allocated/mixed/down/drain/other` 聚合；
- CPU、GPU 总量与已分配量；
- 队列按状态聚合；
- pending reason Top N；
- 分区/QoS 能力摘要引用；
- `partial` 和安全告警。

### 5.3 `AccountDynamicSample`

包含当前学生账号的：

- managed/external 作业数量；
- pending/running/completing 状态分布；
- 已分配 CPU/GPU/内存；
- 当前 pending reasons；
- 明细只返回该学生账号的作业。

### 5.4 `RunResourceSummary`

分为四组：

1. `requested`：Contract 请求的 CPU、GPU、内存、walltime；
2. `allocated`：`AllocCPUS`、`AllocTRES` 等 Slurm 分配事实；
3. `used`：CPU、内存、I/O 和可选 GPU 测量；
4. `evaluations`：规则结论、证据引用、置信度和建议 patch。

时间字段保留 `Submit`、`Eligible`、`Start`、`End` 和 `Elapsed`，以区分排队、调度资格等待和运行耗时。

## 6. 指标语义

### 6.1 CPU

终态且运行至少 10 分钟时：

```text
cpu_efficiency = total_cpu_seconds / cpu_time_raw
```

其中 `TotalCPU` 是所有 task 使用的 CPU 时间，`CPUTimeRAW` 是已分配 CPU 数乘 elapsed 的总可用 CPU 秒。字段不完整时不计算。

### 6.2 内存

`MaxRSS` 明确表示最大单 task RSS，不冒充整个作业的内存峰值。

只有以下场景计算 memory efficiency：

- 单 task 作业；或
- 集群提供经过 probe 证明可靠的 job-level peak memory。

其他场景只展示 `MaxRSS` 和分配内存，并附口径说明，不判定整体内存浪费。

### 6.3 GPU

仅在集群已配置并验证 `gpumem`/`gpuutil` 等 accounting 时计算。覆盖率低于 80% 时返回 `insufficient_coverage`，不产生低利用率结论。

### 6.4 walltime

```text
walltime_ratio = elapsed / requested_walltime
```

单次过量请求只给低置信度提示；至少 3 个可比较 Run 持续偏低后，才能形成较高置信度建议。

## 7. 确定性评价规则

首版规则：

| rule_id | 条件 | 输出边界 |
| --- | --- | --- |
| `CPU_UNDERUTILIZED` | 终态、运行至少 10 分钟、CPU efficiency `< 20%` | 建议减少 CPU 或检查并行度 |
| `MEMORY_OVERALLOCATED` | 可靠 job peak `< 30%` 分配内存 | 建议降低内存；不适用于仅有多 task `MaxRSS` |
| `GPU_UNDERUTILIZED` | 平均 GPU utilization `< 20%` 且覆盖率 `>= 80%` | 建议核查数据管线、batch size 或 GPU 数量 |
| `WALLTIME_OVERREQUESTED` | elapsed/requested `< 20%` | 单次低置信度；3 个可比较 Run 后提高置信度 |
| `QUEUE_CONGESTED` | pending reasons 与短期趋势共同表明拥堵 | 建议可用分区/QoS或错峰，不承诺等待时间 |

OOM、TIMEOUT、依赖错误和非零退出继续属于 Diagnosis；本设计不把故障和效率混为一个分数。

评价对象统一为：

```json
{
  "rule_id": "CPU_UNDERUTILIZED",
  "severity": "warning",
  "summary": "CPU 使用率偏低",
  "measured_values": {},
  "thresholds": {},
  "evidence_refs": [],
  "confidence": "medium",
  "suggested_contract_patch": {}
}
```

规则只能提出结构化建议。任何 Contract patch 仍进入现有 Agent 审批、preflight、派生 Run 和审计流程，不能自动修改或提交。

## 8. API 边界

首版后端接口：

- `GET /api/v1/observability/platform/latest`
- `GET /api/v1/observability/platform/series?window=24h&step=1m`
- `GET /api/v1/observability/account/latest`
- `GET /api/v1/observability/account/series?window=24h&step=1m`
- `GET /api/v1/runs/{run_id}/resources`
- `GET /api/v1/observability/events/stream`

所有接口 owner scoped；平台明细只包含公开集群事实和匿名聚合。响应必须返回 `captured_at`、`freshness`、`partial` 和 `warnings`，不能把陈旧缓存伪装为实时状态。

SSE 只通知新样本或状态变化，客户端断线后可按事件 ID 补读；series 数据仍由普通 GET 获取。

## 9. 异常与降级

- Slurm 暂时不可用：保留最后样本并标记 stale，同时记录 collector 健康事件；
- 部分命令失败：写 partial sample，成功字段仍可用；
- accounting 延迟：有限重试，超限后字段为 not_collected，不阻塞 Run 终态；
- collector lease 丢失：旧 worker 停止写入，防止双采集；
- schema/单位异常：拒绝派生效率值，保留原始安全摘要用于审计；
- GPU accounting 未配置：明确 unsupported；
- 外部作业：不生成虚假 Evidence、Diagnosis 或 Agent patch。

## 10. 本地模拟验收

所有首版能力先在本地模拟器证明，不等待远程 VM：

1. 构造 idle/mixed/down 节点和 pending/running/terminal 作业，验证 latest 与 24 小时 series；
2. 证明同一连接只有一个持租约 collector 写入；
3. 证明 `alice` 只能看到本人 managed/external 明细，`bob` 越权失败；
4. 构造 CPU 低利用、内存口径不足、GPU unsupported、walltime 单次与三次历史场景；
5. 证明缺失值不变成 0，stale/partial/coverage 在 API 中可见；
6. 终态 summary 的 Evidence 引用和 digest 可验证；
7. Agent 只能把评价作为引用事实提出建议，仍需用户批准；
8. collector 重启、租约接管和 accounting 延迟不会产生重复终态总结。

远程 VM 恢复后只补部署与真实命令兼容验证；真实 107 未 probe 前不得宣称 GPU 使用率或真实集群动态已经通过。

## 11. 实现切片

后续实现计划应按以下独立切片编写：

1. 数据模型、迁移、retention 和租约；
2. simulator 平台/account collector 与解析器；
3. active `sstat` 和 terminal `sacct` Run summary；
4. 确定性效率规则与 Evidence 绑定；
5. latest/series/resource/SSE API；
6. 本地 simulator 纵向 smoke 与故障注入；
7. 前端 Dashboard（已明确暂缓，另行设计）。

## 12. 参考实现与资料

- Slurm `sstat`：<https://slurm.schedmd.com/sstat.html>
- Slurm `sacct`：<https://slurm.schedmd.com/sacct.html>
- Slurm accounting：<https://slurm.schedmd.com/accounting.html>
- Slurm GRES/GPU accounting：<https://slurm.schedmd.com/gres.html>
- Slurm native metrics：<https://slurm.schedmd.com/metrics.html>
- Slurm REST 安全与缓存边界：<https://slurm.schedmd.com/rest.html>
- Prometheus recording rules：<https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/>
