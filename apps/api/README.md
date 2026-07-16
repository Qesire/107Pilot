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

The optional `{"provider":"local"}` mode uses an OpenAI-compatible self-hosted
USTC gateway, matching the `ustc-deepseek` provider used by OpenCode.
Configure it through environment variables only:

```bash
export PILOT107_LLM_BASE_URL="https://api.llm.ustc.edu.cn/v1"
export PILOT107_LLM_MODEL="deepseek-v4-flash-ascend"
export PILOT107_LLM_API_KEY="..."
export PILOT107_LLM_TIMEOUT_SECONDS=60
export PILOT107_LLM_MAX_TOKENS=1200
export PILOT107_LLM_STRUCTURED_OUTPUT_MODE=prompt_json
export PILOT107_LLM_MAX_ATTEMPTS=2
```

The API key must not be committed to repository files. If any required LLM
variable is missing, `provider=local` returns `agent_provider_unsupported`,
while `provider=none` continues to work.

Run the smoke script before enabling `provider=campus` in a shared deployment:

```bash
bash scripts/smoke-campus-llm.sh
```

The script builds a temporary failed Run with evidence-bound diagnoses and calls
the configured provider with at most `PILOT107_LLM_MAX_ATTEMPTS` bounded
attempts. Missing `PILOT107_LLM_*` variables are treated as a safe skip by
default. Set `PILOT107_REQUIRE_LLM_SMOKE=1` when deployment checks must fail if
the gateway is not configured.
