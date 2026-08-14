# pilot107-api

Phase 0A keeps API code behind this boundary.

Initial API responsibilities:

- accept Recipe/Contract validation requests;
- submit Runs through a selected Slurm backend;
- expose Run, Evidence and Capsule status;
- enforce user/path authorization before any backend call;
- keep Docker simulator behavior separate from future real 107 compatibility mode.

The first implementation should use the pure `pilot107.core` modules before
adding FastAPI-specific request handlers.

Phase 0A exposes a minimal stdlib HTTP API for local development:

```bash
bash scripts/serve-api.sh
```

Local M0 development may use `http://127.0.0.1`. Competition deployment must
terminate HTTPS at the application-node reverse proxy; `pilot107-api` itself
should listen only on localhost or a private network interface.

## Agent LLM provider

`POST /api/v1/runs/{run_id}/agent/explain` always supports
`{"provider":"none"}`. This mode is deterministic and only explains stored
diagnoses with evidence references.

The optional `{"provider":"local"}` mode calls the private `pilot-agentd`
service. Python owns identity, durable state, policy, and evidence; Agentd owns
Pi Agent Core and every model-provider request. Configure the API/Worker side
with only the internal boundary:

```bash
export PILOT107_AGENTD_URL="http://pilot-agentd:8091"
export PILOT107_AGENTD_TOKEN="..."
export PILOT107_AGENTD_MODEL_PROFILE="campus-default"
```

Configure the campus OpenAI-compatible endpoint only on `pilot-agentd`:

```bash
export PILOT107_LLM_BASE_URL="https://api.llm.ustc.edu.cn/v1"
export PILOT107_LLM_MODEL="deepseek-v4-flash-ascend"
export PILOT107_LLM_API_KEY="..."
export PILOT107_LLM_TIMEOUT_SECONDS=60
export PILOT107_LLM_MAX_TOKENS=1200
export PILOT107_LLM_MAX_ATTEMPTS=2
```

The API key must not be committed to repository files or injected into the API
or Worker containers. Agentd has no Slurm token, SSH key, shared-workspace
mount, user credential, or cluster command capability. Its `/healthz` endpoint
means the process is alive; `/readyz` reports the selected profile and whether
that profile is configured without making a billable provider request.

Migration mapping:

| Previous Python setting | New placement |
| --- | --- |
| `PILOT107_LLM_BASE_URL` | Agentd only, unchanged name |
| `PILOT107_LLM_API_KEY` | Agentd only, secret environment injection |
| `PILOT107_LLM_MODEL` | Agentd only, unchanged name |
| timeout/token/attempt settings | Agentd only, unchanged names |
| `PILOT107_LLM_STRUCTURED_OUTPUT_MODE` | Removed; Agentd tool schemas enforce output |
| none | API/Worker use `PILOT107_AGENTD_URL`, token, and model profile |

Run the deterministic local vertical smoke after building app images:

```bash
bash scripts/build-app-images.sh
bash scripts/smoke-pilot-agentd-faux.sh
```

Run the campus smoke before enabling `provider=local` in a shared deployment:

```bash
bash scripts/smoke-campus-llm.sh
```

The campus script reads only the three `PILOT107_AGENTD_*` client variables. It
prints `SKIP: pilot-agentd or campus profile is not configured` and exits zero
when they are absent, so remote-VM or campus credentials are not a local
completion prerequisite. It never reads or prints the campus API key.
