# Phase 3C 权限与发布门禁切片审查

日期：2026-07-16  
范围：`003c.002` migration、模板审核授权、发布门禁、release/adoption 安全边界。  
结论：本切片 P0/P1 已清零；Phase 3C 整体仍在进行中。

## Findings-first 结果

### 已修复 P1：publish 信任过期审核门禁

初版只在 `submit_review` 执行门禁，若审核后 Recipe、materializer 或门禁策略收紧，publish 仍可能
使用旧的 OK 报告。现已在 publish 的同一事务中重新执行当前门禁；新结果为 BLOCK 时不创建
release。release 固化 policy version 和 gate report，采用时拒绝无有效门禁报告的历史对象。

### 已修复 P1：高级 raw sbatch 未进入危险指令检查

Contract 的 `extensions.advanced.raw_sbatch` 会在采用往返中保留，但初版只检查 `entry.command`。
现使用固定指令 allowlist；本切片只允许无参数 `#SBATCH --exclusive`，拒绝命令替换、非 directive
文本、未知选项及为无参数选项附值。

### 已修复 P1：容器验证可由发布者自报

草稿 compatibility 中的 `verified: true` 不是可信证据。现只接受由门禁构造时注入的受信
sha256 digest 集；草稿字段不能提升信任级别。Contract materializer 当前还没有 OCI capability，
所以受信 digest 只消除“来源未验证”finding，仍不能绕过 materializer capability 阻断。

## 权限边界

- public/campus/private review：reviewer 或 admin；
- course review：同 course scope 的 instructor/TA，或 admin；
- 所有可见性均禁止 requester 自审，包括 admin；
- reviewer actor、授权角色和 course scope 持久化到 review；
- 当前 principal 是领域层可信输入，HTTP 身份/课程成员目录尚未接入，不能把客户端自报角色直接
  转成 principal。

## 发布门禁覆盖

- ContractV2 normalization/schema、Recipe 查找、materializer 和静态 preflight；
- secret-like key/value 与常见 credential material；
- `rm -rf`/`rm -fr`、network pipe to shell、sudo、interactive srun、world-writable chmod；
- workdir 必须是 `/public` 下绝对路径；
- partition、GPU 和 container compatibility 一致性；
- License、attribution、dataset access、risk statement；
- draft/review/release content digest 绑定与 release 数据库级不可变性。

## 验证证据

- 模板市场定向测试：13 项通过；
- migration/Contract 联合定向测试：19 项通过（首次审查时）；
- 全量测试：424 项通过；
- `ruff check src tests scripts`：通过；
- `mypy src/pilot107`：55 个源模块通过。

本切片没有新增 Slurm/Docker 行为；Docker 纵向验证应在模板 API、采用后 Contract 生成和
verification 写入闭环后执行，并先运行模拟器健康门禁。

## 剩余风险与下一动作

1. 从可信身份/课程目录生成 reviewer principal，禁止直接信任客户端角色和 scope；
2. 增加 draft/review/release/market/adoption API、If-Match 和 idempotency contract tests；
3. 采用 release 后生成 owner-scoped canonical Contract，并重新执行采用者动态 entitlement preflight；
4. verification 只能由受控 Run/Evidence 写入，不能接受发布者自报；
5. 完成 Docker publish-to-real-job smoke 后再执行 Phase 3C 整体结项 review。
