# Phase 3D 新生可用性验收协议

> 已于 2026-07-16 被 [`phase3d_user_feedback_protocol.md`](phase3d_user_feedback_protocol.md) 取代。项目当前只要求使用可获得参与者的反馈改进产品，不再设置至少 5 人、十分钟中位数或总体 `passed/failed` 门禁。本文及其 schema/evaluator 保留为历史方案，不再决定 Phase 3D/3E 准入。

状态：等待真实受试者  
目标：至少 5 名没有 Slurm 经验的本科生，首次成功作业中位时间不超过 10 分钟，基本流程不使用终端。

## 受试者与隐私

- 只邀请自报没有 Slurm 经验的本科生；
- 使用 `p01` 一类匿名 participant ID，不记录姓名、学号、邮箱、IP 或录屏中的个人信息；
- 开始前说明这是产品测试，不是能力考试；受试者可以随时退出；
- facilitator 只按统一提示卡回答，不替受试者点击或解释 Slurm 术语；
- 自动化、项目开发者和已使用过 Slurm 的参与者不能计入 5 人门槛。

## 统一任务

1. 从模板市场找到 Python CPU 模板；
2. 采用模板并修改 workdir 与 command；
3. 阅读资源预检结果，并用自己的话说明是否可以提交；
4. 创建并提交 Run；
5. 在 Run 工作台找到 stdout、结果文件；
6. 打开预置失败 Run，找到失败原因和对应 Evidence；
7. 回到成功 Run，向 facilitator 报告 Run ID 与结果内容。

从任务卡交给受试者时记录 `started_at`，首次出现服务端 `SUCCEEDED` 且受试者找到结果时记录
`first_success_at`。计时不得从登录后或模板已打开时开始。若使用任何 terminal/CLI，`used_terminal=true`，该研究不能
通过“基本流程无需终端”门禁。

## 事实绑定

每个会话必须记录：

- 采用后 `contract_id`；
- 真实 `success_run_id` 与预置 `failure_run_id`；
- 成功 Run 的 `logs`、`outputs`，以及失败 Run 的 `logs` Evidence URI；
- 八个任务布尔值；
- `automated=false`、`slurm_experience=none`、`completed=true`。

记录格式由 [`phase3d_novice_acceptance.schema.json`](../../config/phase3d_novice_acceptance.schema.json) 固定。禁止把
Playwright、pilot-browser、API smoke 或人工编造数据写成真人结果。

## 执行门禁

研究开始前先在固定身份 competition 部署上执行只读 readiness gate：

```bash
PYTHONPATH=src uv run python scripts/check_phase3d_novice_study_readiness.py \
  --base-url https://127.0.0.1:8443/api/v1 \
  --user alice \
  --failure-run-id run_actual_failure_id \
  --insecure
```

只有 `status=ready` 才能发放任务卡。该检查要求公开且门禁通过的 Python CPU 模板、不可切换的固定身份、真实 command backend 的 FAILED Run、完整 Evidence 和至少一条带 Evidence 引用的确定性诊断；它只证明研究环境可用，不计入真人结果。

完成真人记录后执行结果门禁：

```bash
PYTHONPATH=src uv run python scripts/check_phase3d_novice_acceptance.py \
  /path/to/anonymized-study.json \
  --out artifacts/usability/phase3d_novice_acceptance_report.json
```

退出码：`0=passed`、`1=failed/invalid`、`2=pending`。只有 `passed` 才能宣称 Phase 3D 真人可用性目标完成。

## Facilitator 观察项

以下只用于改进，不替代硬门禁：

- 第一次停顿超过 30 秒的位置；
- 对 workdir、partition、QoS、preflight、Run、Evidence 的误解；
- facilitator intervention 次数与原话；
- 是否误以为模板采用会修改公共模板；
- 是否能区分“没有规则诊断”和“作业一定没有问题”。
