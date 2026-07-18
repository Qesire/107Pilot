# CPU-only 8C/16G 发布候选评审

日期：2026-07-18
证据等级：D1（本机 Docker）
结论：功能候选通过；尚未上传或部署 VM，未连接真实 107。

## Findings first

### P0/P1

无已知未关闭项。

### 非阻塞剩余项

1. 全领域业务 Store 仍以 SQLite 为主；PostgreSQL 已覆盖 ControlRepository，但不是完整业务数据库迁移。单 VM CPU 候选继续使用 SQLite，并保留已演练的冷备恢复入口。
2. 校园身份、正式证书、VM 网络/磁盘性能、真实 Slurm/107 准入均需要未来外部环境验证。
3. 在线依赖和镜像漏洞扫描依赖 CI/网络环境；本轮不把离线状态记为“零漏洞”。按当前优先级，非 P1 供应链发现不阻塞功能包。

## 已实现能力

- 静态 `cpu-only-8c16g-rc` capability profile：仅 `CPU-RC`/`qos_cpu_rc`，作业最多 4 CPU、6 GiB、4 小时；宿主目标为 8C/16G。
- API 在 CPU profile 下隐藏 GPU recipes，并将 CPU recipe 的 partition/QoS 约束改写为当前 profile。
- Compose 只启动一个 Slurm worker；容器 CPU 上限合计 7 CPU，内存上限约 11.6 GiB，给宿主与运行时留有余量。
- 启动时生成本地随机数据库/JWT/gateway/HMAC 凭据；模板占位符存在时拒绝启动。
- 固定 revision 镜像、离线镜像 tar、镜像 content digest、SHA256、Python dependency inventory、Web lockfile、启动/检查/停止/恢复入口均由发布脚本生成。

## 本机功能证据

- `scripts/check-cpu-rc.sh` 两轮通过：成功、失败、取消及其 Evidence/Capsule 闭环通过。
- 整栈重启后，重启前成功 Run 仍可读取；重启后再次完成三类 Run。
- 20 路并发 read/validate/prepare 为 20/20、0 error；4 路并发完整 workflow 为 4/4、0 error。
- 浏览器实际页面只显示一个 `CPU-RC` 分区、一个 CPU QoS，并显示“not a real 107 capability claim”；控制台与页面错误为空。
- Python：594 passed、13 PostgreSQL integration skipped、2 subtests；Ruff 与 strict mypy（73 source files）通过。
- Web：10 files / 64 tests；production build 1,914 modules。

## 边界

这些结果只证明本机 D1 CPU profile 的功能与恢复准备，不证明 8C/16G VM 性能、校园多用户生产身份、真实模型或真实 107 行为。
