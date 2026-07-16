# Phase 0B 承载力检查报告

日期：2026-07-12

## 1. 验证口径

本轮验证的是本地 competition profile 的 100 并发承载力：

```text
client
→ HTTPS reverse proxy
→ pilot107-web
→ pilot107-api
→ pilot107-command-gateway
→ Docker Slurm simulator
→ pilot107-worker Evidence collection
```

100 并发分为两类：

- 轻量并发：健康检查、页面、Recipe、Contract validate、Contract create、Run prepare；
- 完整工作流并发：100 个客户端并发完成 `contract create → run prepare → submit → wait SUCCEEDED + Evidence collected → Capsule ready`。

## 2. 当前架构观察

- Web、API、reverse proxy 当前使用 Python `ThreadingHTTPServer`；
- SQLite 已启用 WAL；
- API/Worker 共享 SQLite 和 `pilot107-data` volume；
- competition 模式下 API/Worker 通过 `pilot107-command-gateway` 访问 Slurm，不使用 `demo` backend；
- 当前只有 1 个 `pilot107-worker` 常驻进程，Evidence 采集吞吐是完整工作流尾延迟的主要瓶颈。

## 3. 已运行命令

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition.sh
python3 scripts/load_competition.py --concurrency 100 --scenario all
python3 scripts/load_competition.py --concurrency 100 --scenario workflow --workflow-timeout 360
docker stats --no-stream \
  pilot107-sim-pilot107-api-1 \
  pilot107-sim-pilot107-web-1 \
  pilot107-sim-pilot107-worker-1 \
  pilot107-sim-pilot107-command-gateway-1 \
  pilot107-sim-pilot107-reverse-proxy-1
```

## 4. 轻量 100 并发结果

```text
load read concurrency=100
ok=100 errors=0 elapsed=2.482s rps=40.3
latency_ms=min:9.5 p50:1025.8 p95:2282.6 max:2472.6

load validate concurrency=100
ok=100 errors=0 elapsed=2.497s rps=40.0
latency_ms=min:8.3 p50:1043.4 p95:2269.2 max:2463.7

load prepare concurrency=100
ok=100 errors=0 elapsed=3.179s rps=31.5
latency_ms=min:7.5 p50:10.1 p95:1108.7 max:3116.0
```

结论：当前 Web/HTTPS/API/SQLite 写入路径可以承受 100 并发轻量请求，无错误。

## 5. 完整工作流 100 并发结果

```text
load workflow concurrency=100
ok=100 errors=0 elapsed=164.619s rps=0.6
latency_ms=min:4135.4 p50:105078.2 p95:164184.3 max:164580.9
```

该场景强制每个客户端完成：

```text
POST /api/v1/contracts
POST /api/v1/runs/prepare
POST /api/v1/runs/{run_id}/submit
轮询 GET /api/v1/runs/{run_id}
等待 SUCCEEDED + collection_state=succeeded
POST /api/v1/runs/{run_id}/capsule
等待 capsule_state=ready
```

结论：当前系统可以承受 100 个并发完整提交、Evidence 和 Capsule 完成流程，但完整流程尾延迟较高，p95 约 164 秒。

## 6. 资源快照

压测后主要容器资源：

```text
pilot107-api               CPU 0.01%   MEM 114.1MiB
pilot107-web               CPU 0.01%   MEM 20.8MiB
pilot107-worker            CPU 23.33%  MEM 35.89MiB
pilot107-command-gateway   CPU 62.67%  MEM 22.62MiB
pilot107-reverse-proxy     CPU 0.01%   MEM 18.37MiB
```

资源快照说明 API/Web/Proxy 内存压力很小；完整工作流的主要压力集中在 command gateway、Worker 单进程采集、Slurm 作业排队和 Evidence/Capsule 文件 I/O。

## 7. 判断

当前结论：

```text
100 并发轻量 Web/API 请求：通过
100 并发 Contract create + Run prepare：通过
100 并发真实 Slurm submit + Evidence + Capsule 完整工作流：通过，但尾延迟较高
```

因此，本地 competition profile 满足“至少能应对 100 并发”的最低验收口径。

## 8. 缺口和加固建议

如果比赛要求只是 100 个用户同时浏览、验证 Contract、创建 Run 并提交短作业，当前版本可支撑。

如果要求是 100 个用户同时提交较长作业并快速拿到 Evidence/Capsule，应继续加固：

- 增加 `pilot107-worker` 副本数，验证 `CollectionTask` lease 在多 Worker 下的吞吐和幂等；
- 增加 SQLite `busy_timeout`，减少高并发写入时的锁等待风险；
- 将 stdlib `ThreadingHTTPServer` 替换为生产级 ASGI/WSGI server；
- command gateway 增加并发限制、队列长度和指标；
- 增加 `/metrics` 或至少 Worker backlog 指标；
- 将 100 并发 workflow 加入 `scripts/check-competition.sh` 的可选 nightly/stress 模式，而不是默认 smoke。
