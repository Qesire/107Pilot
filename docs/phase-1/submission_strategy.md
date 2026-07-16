# Submission Strategy Decision

> 状态：draft  
> 当前建议：比赛主线在 Docker Slurm 中同时实现 REST submit 和模拟 command backend；真实 107 submit 仅作为 M1-R 人工确认 smoke，不阻塞主线。

## 1. Candidate Strategies

```text
rest_native
trusted_command_proxy
user_side_cli
```

## 2. Decision Matrix

| 条件 | rest_native | trusted_command_proxy | user_side_cli |
|---|---|---|---|
| REST submit smoke 成功 | 必需 | 不必需 | 不必需 |
| 服务端可代表用户提交 | 必需 | 由代理保证 | 否 |
| 工作目录服务端可见 | 必需 | 登录节点可见 | 用户侧可见 |
| 需要用户本地动作 | 否 | 否 | 是 |
| 适合比赛 M1 Docker 演示 | 是 | 是，限模拟环境 | 可作为未来降级 |

## 3. Required Smoke Tests

只读：

- `/jobs`；
- `/job/{id}`；
- `/nodes`；
- `/partitions`；
- accounting；
- cancel 已终态作业语义。

提交 smoke，必须用户显式确认：

- shared workdir success；
- invalid workdir failure；
- unwritable output failure。

## 4. WorkDirPreflight

提交前验证：

- workdir 存在；
- workdir 位于共享目录；
- 用户可读；
- 用户可执行；
- 输出父目录可写；
- 计算节点能看到同一路径；
- 不依赖登录节点 `/tmp`；
- 不把本地电脑路径写入 sbatch。

## 5. Idempotency

提交请求必须包含：

```text
submission_nonce
script_hash
contract_hash
expected_job_name_marker
```

如果 submit 超时：

```text
state = SUBMISSION_UNCERTAIN
→ 按 marker 和时间窗口查询
→ 找到 job 后绑定 job_id
→ 找不到且确认未提交后才允许重试
```

## 6. Current Decision

```yaml
selected_strategy_for_competition: rest_native_plus_simulated_command_backend
real_platform_strategy: read_only_first_optional_manual_smoke
real_command_proxy: unsupported_until_admin_approval
blocking_unknowns:
  - competition_app_node_to_docker_host_network
  - docker_host_ports
  - real_platform_smoke_submit_permission
```
