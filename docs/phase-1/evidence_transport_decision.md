# Evidence Transport Decision

> 状态：draft  
> 当前建议：比赛主线使用 Docker 模拟 EvidenceTransport；真实平台 EvidenceTransport 降级为 M1-R/M2 兼容目标。

## 1. Candidate Transports

| Transport | 适用 | 当前状态 |
|---|---|---|
| `DockerVolumeEvidenceTransport` | M0 Docker | 可执行 |
| `AuthorizedFilesystemEvidenceTransport` | 真实平台服务端可读授权目录 | M1-R/M2 待确认 |
| `UserAgentEvidenceTransport` | 真实用户目录可写，Web 读取授权 evidence_root | M1-R/M2 待确认 |
| `CliBundleEvidenceTransport` | Web 无法读取真实共享目录 | 真实平台降级设计 |

## 2. Required Path Classes

```text
SHARED_PERSISTENT: /public（real107 probe observed）, /home（training material, 待独立确认）
NODE_LOCAL_EPHEMERAL: /tmp（training material, 待 compute job 独立确认）
SERVICE_LOCAL: /var/lib/107pilot
UNKNOWN: 其他路径
```

## 3. SafePath Rules

所有文件访问必须：

- 拒绝空字节；
- realpath 后位于允许根；
- 防 symlink escape；
- 防 hardlink 异常；
- 拒绝设备文件、FIFO、socket；
- 限制文件大小、目录深度、文件数量；
- 记录 actor、run_id、source path；
- tar/zip 解包防 path traversal。

## 4. Proposed Run Root

```text
/public/home/<user>/.107pilot/runs/<run_id>/
```

待确认：

- `/public/home/<user>` 是否真实存在；本次 probe 已确认 `/public/home/pb23061276`；
- 服务 Unix 用户是否可读；
- 是否需要 ACL 或服务组；
- 是否允许用户侧 CLI 上传 bundle。

## 5. Decision Gate

比赛 M1 当前选择：

```yaml
selected_transport: DockerVolumeEvidenceTransport
scope: competition_docker
fallback: evidence_bundle_api_between_app_node_and_docker_host
```

真实 107 M1-R 后续再选择：

```yaml
selected_transport:
allowed_roots:
service_access:
fallback:
remaining_risks:
```
