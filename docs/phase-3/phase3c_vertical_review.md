# Phase 3C 模板市场纵向闭环总审查

日期：2026-07-16  
范围：市场查询、采用后 Contract、Run/Evidence/Capsule verification、版本迭代、Docker 纵向验收。  
结论：P0/P1 已清零；Phase 3C 后端纵向闭环完成，可进入 Phase 3D。

## Findings-first 结果

### 已修复 P1：Contract 幂等写入吞掉非冲突型完整性错误

`ContractStore` 初版在 `idempotent=True` 时吞掉所有 SQLite integrity error。注入 trigger 失败时会继续
查询并抛出误导性的 `KeyError`。现仅在确有同 ID Contract 时执行幂等比较；不存在对应记录时重新抛出
原始错误。adoption 的 draft、Contract、lineage 使用同一事务，失败时整体回滚。

### 已修复 P1：Evidence manifest 已生成但未进入索引

Demo 与 Docker collector 会写 `manifest/manifest.json`，但此前只索引 manifest 生成前的 artifacts。
verification 因此无法从数据库证明 finalized manifest。现两个 collector 均在写完 manifest 后追加
`evidence_objects` 记录及 SHA/finalized_at，并有回归断言。

### 已修复 P1：verification 未绑定并复验 Capsule

仅凭终态 Run 和 Evidence 不满足 Phase 3C 的 Evidence/Capsule 口径；仅检查 `capsule_state=ready` 又无法
发现生成后的磁盘篡改。现 verification 要求 ready Capsule，在服务器配置的 Capsule 根目录重新运行
checksums 校验，并固化 capsule ID 与 manifest SHA。缺失或篡改 Capsule 均拒绝写入。

### 已修复 P1：同一模板无法产生第二个 release

draft 发布后原状态机不允许再次编辑，而 schema 又限定一个 template 只有一个 draft，导致
`release_version` 形式存在但实际无法创建 `1.1.0`。现已发布 draft 可凭 expected version 开启下一修订；
旧 release 仍由数据库 trigger 保持不可更新/不可删除，新 review 产生新的 immutable release。

### 已修复 P2：缺少 release diff 公共契约

新增 `GET /api/v1/templates/{template_id}/diff?from=&to=`。服务端先分别执行 release 可见性授权，再输出
稳定排序的 JSON Pointer 变化；withdrawal、验证次数等外部事实不混入内容 diff。

### 已验证的安全边界

- 市场 cursor 绑定 actor、course scopes 与全部过滤条件，不能跨身份或筛选复用；
- private/course/campus/public 可见性在查询与详情/diff/verifications 三处一致执行；
- adoption 并发使用同一 request key 时只生成一个 private draft、Contract 和 lineage；
- verification 请求只允许 `run_id` 与 `request_key`，客户端自报 environment/status 被拒绝；
- Run 必须由当前 actor 所有，且 `contract_id` 必须命中该 actor 对目标 release 的 adoption；
- Docker、real107 CPU、real107 GPU 等级来自服务端部署配置；GPU 等级还要求 GPU Contract Run 和
  environment summary Evidence；
- withdrawn release 不再进入市场或允许新采用，历史 adoption/verification lineage 保留。

## 验证证据

- 全量测试：439 项通过；
- `ruff check src tests scripts`：通过；
- `mypy src/pilot107`：56 个源模块通过；
- 模拟器前置健康门：Compose 服务健康，`sinfo` 正常，`Slurmctld(primary) ... UP`；
- `smoke-sim-phase3c.sh`：通过；真实生成 template、adoption Contract、Slurm Run、Evidence、Raw
  Capsule 与 verification，并验证市场 adoption/verification metrics；
- OpenAPI snapshot 已包含市场、release diff、adopt/withdraw/verify 与 verifications 操作。

## 残余风险

1. role/course directory 仍是服务端静态配置，生产接入需替换为学校身份与课程目录适配器；
2. `real107_cpu`/`real107_gpu` 由受控部署环境声明，本轮仅实测 Docker 等级，不能外推真实 107；
3. `%LIKE%` 搜索适合当前比赛规模，大规模市场需要 FTS 与独立索引；
4. SQLite `BEGIN IMMEDIATE` 满足当前单机一致性，生产多副本仍需迁移 PostgreSQL repository；
5. 模板 UI、Contract Studio 和身份产品化属于 Phase 3D，不在本后端阶段内。
