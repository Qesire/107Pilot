# Probe Artifacts

本目录保存 Phase -1 和 Phase 1 的平台 probe 输出。

不得提交或保存：

- JWT；
- Authorization header；
- X-SLURM-USER-TOKEN；
- Cookie；
- SSH key；
- 私有路径全文，除非用户确认。

建议文件：

```text
cluster_profile.json
user_entitlement_profile.json
endpoint_set.json
openapi_digest.txt
openapi.redacted.json
submission_strategy_probe.json
workdir_preflight_probe.json
evidence_transport_probe.json
```

每个 artifact 必须包含：

```json
{
  "observed_at": "...",
  "source_authority": "A1",
  "collector": "...",
  "redacted": true
}
```

