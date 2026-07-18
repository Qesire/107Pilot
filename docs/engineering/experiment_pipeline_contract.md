# 可恢复实验流水线工程规范

本规范将一个在实际 Slurm 平台稳定运行的 HIFX4 v4.9.2 LightX2V Distill runbook 抽象为通用工程约束。它保留经过实践验证的控制协议，不复制模型、用户目录、A100 数量、QoS 或数据集等项目专有事实。

`MUST` 表示不满足时应阻断提交或下游阶段；`SHOULD` 表示默认采用，偏离时必须留下理由和 Evidence。

## 1. 控制面与计算面

- 登录节点 MUST 只做 bootstrap、静态校验、状态查询和 `sbatch` 编排。
- 依赖 import、权重哈希、校准、量化、生成和评测 MUST 在 Slurm allocation 内执行。
- GPU 阶段 MUST 同时验证 `SLURM_JOB_ID` 与可见 GPU allocation；CPU 重任务也 MUST 验证 Slurm 上下文。
- 无效 cwd MUST 切换到稳定 HOME 或 `/tmp`，不能让已删除目录破坏 Python/sbatch 启动。

## 2. 单一运行契约与有效配置

- 模型类、scheduler、步数、权重布局、数据集版本和输出 schema MUST 有一个结构化、版本化的事实源。
- preflight、单元测试、GPU gate 与生产入口 MUST 调用同一 effective-config builder。
- 默认配置、请求字段和模型配置合并完成后，冻结字段 MUST 再次覆盖并逐字段校验。
- MUST 比较 JSON 字段和来源；不得用“源码中是否出现某字符串”代替结构化一致性检查。
- 每次运行 SHOULD 保存冻结契约、effective config、源代码 revision/import origin 和 SHA-256。

## 3. 环境与源码树 import closure

- site 路径、实验 profile、variant matrix SHOULD 分层，不把站点路径写入算法代码。
- 源码树运行时 MUST 在每个生产入口导入前建立同一、确定顺序的 `PYTHONPATH`。
- preflight MUST 记录 `sys.executable`、conda prefix、模块 `__file__`、CUDA/driver 和关键依赖版本。
- import 失败 MUST 区分“源码根缺失”“环境未激活”“包未安装”和“共享文件系统瞬态”，不能统一执行 pip 修复。

## 4. Slurm 资源合同

- account、partition、QoS、CPU、memory、GPU type/count 和 time limit MUST 显式声明；同一指令 MUST 只出现一次。
- array task MUST 校验 task id 边界。
- 单个 array throttle 不等于 DAG 峰值。调度前 MUST 汇总同一依赖层所有 `throttle × GPU/task`。
- 并发上限 MUST 来自 capability/QoS 或显式批准的项目 ceiling；不能因机器空闲临时放大。
- 依赖默认 MUST 使用 `afterok`。Gate 失败时下游 fail closed，不得自动绕过。

## 5. 分片、原子提交和完成判定

每个分片的完成真源是三件套：

1. 非空 artifact；
2. 包含 task、size、hash 和运行身份的 metadata；
3. 最后写入的非空 `COMPLETE`。

重计算 SHOULD 先写 `${SLURM_TMPDIR}`。发布到共享文件系统时 MUST 使用：

```text
node-local output
→ copy to shared tmp
→ validate size/schema/hash
→ atomic rename in final directory
→ write metadata atomically
→ write COMPLETE atomically
```

文件存在本身不是成功。merge MUST 扫描完整三件套，并在任何缺片时输出 missing array spec、拒绝 partial merge。

## 6. DAG、Gate 与资源复用

推荐的阶段形态：

```text
CPU structured preflight
→ GPU backend/unit gate
→ shared calibration
→ shard arrays
→ fail-closed merge
→ one-item smoke per variant
→ small matrix gate
→ full generation/evaluation
→ report collection
```

- 可复用资产 MUST 同时满足内容完整性、metadata 契约和 COMPLETE，不得只看目录或文件名。
- 复用/提交决策和 job IDs SHOULD 原子写入 DAG state manifest。
- 全量阶段 MUST 依赖小规模 smoke/gate，而不是仅依赖 import test。

## 7. 重试与恢复

- 文件系统/import/业务命令重试 MUST 有有限次数、明确延迟和最终原始退出码。
- task 内重试处理瞬态；阶段级恢复扫描缺片，只重提 missing tasks。
- merge/gate 失败时 MUST 先修复上游完整性，不得用 `allow-partial` 让实验继续。
- 恢复入口 MUST 幂等；重复调用不能重算已验证完整的分片。
- 取消、resume、status 和最终收集 SHOULD 使用同一 DAG state 与 artifact truth。

## 8. Evidence 与发布门禁

每个阶段至少保存：materialized script、资源请求、环境摘要、结构化报告、artifact inventory、Slurm accounting、stdout/stderr tail 和完成 marker。构建环境没有执行真实 GPU 重任务时，报告 MUST 明确写成“静态/轻量验证”，不得升级为平台运行结论。

对应内置模板：

- `recipe_structured_preflight_gate@1.0.0`
- `recipe_gpu_shard_array_atomic@2.0.0`
- `recipe_fail_closed_merge_gate@1.0.0`

缺片扫描器：`scripts/scan-array-artifacts.py`。

可编辑的完整 Contract 示例位于：

- `data/submission_templates/examples/structured_preflight.contract.json`
- `data/submission_templates/examples/gpu_shard_array.contract.json`
- `data/submission_templates/examples/fail_closed_merge.contract.json`
