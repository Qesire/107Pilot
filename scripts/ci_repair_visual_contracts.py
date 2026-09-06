"""Temporary CI-only visual mock contract repair.

This script rewrites tests/ui/visual.spec.js in the runner worktree so the
candidate can be validated before the same edits are committed. Delete this
script with the temporary repair workflow after the repair is frozen.
"""

from pathlib import Path
import re


PATH = Path("tests/ui/visual.spec.js")
text = PATH.read_text(encoding="utf-8")


def replace_once(old: str, new: str, *, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected 1 match, found {count}")
    text = text.replace(old, new, 1)


replace_once(
    'page.getByPlaceholder("搜索 Run ID、Job ID 或 workdir")',
    'page.getByPlaceholder("搜索运行 ID、Job ID 或工作目录")',
    label="run search placeholder",
)
replace_once(
    'await expect(page.getByRole("heading", { name: "新建 Contract" })).toBeVisible();',
    'await expect(page.getByRole("heading", { name: "实验工作区" })).toBeVisible();',
    label="studio heading",
)
replace_once(
    '  await page.getByRole("textbox", { name: "工作目录", exact: true }).fill("/public/home/alice/studio-case");\n'
    '  await page.getByRole("button", { name: "服务端校验" }).click();',
    '  await page.getByRole("textbox", { name: "工作目录", exact: true }).fill("/public/home/alice/studio-case");\n'
    '  await page.getByRole("textbox", { name: /^runtime\\.environment\\.DATA_ROOT/ }).fill("/public/home/alice/dataset.tar.gz");\n'
    '  await page.getByRole("button", { name: "服务端校验" }).click();',
    label="studio required shared path",
)
replace_once(
    'await expect(page.getByRole("alert")).toContainText("表单与未应用源码发生冲突");',
    'await expect(page.getByRole("alert").filter({ hasText: "表单与未应用源码发生冲突" })).toBeVisible();',
    label="dirty source alert",
)

platform_pattern = re.compile(
    r'    if \(url\.pathname === "/api/v1/platform/capabilities"\) \{.*?\n'
    r'    if \(url\.pathname === "/api/v1/platform/observation"\) \{',
    re.S,
)
platform_replacement = '''    if (url.pathname === "/api/v1/platform/connections") {
      return json(route, {
        items: [{
          connection_id: "visual-107",
          target_id: "107-simulator",
          state: "active",
          owner: "current-user-only",
          checked_at: "2026-07-16T02:08:00Z",
          expires_at: null,
          message: "连接正常",
          status_code: "ok",
          revision: 1,
        }],
      });
    }
    if (url.pathname === "/api/v1/platform/capabilities") {
      if (options.capabilitiesForbidden) {
        return json(route, {
          error: { code: "FORBIDDEN", message: "scope denied" },
        }, 403);
      }
      return json(route, {
        profile_id: "visual-capability-v1",
        source_authority: "visual-fixture",
        captured_at: "2026-07-16T02:08:00Z",
        freshness_seconds: 60,
        default_partition: "Students",
        default_qos: "normal",
        partitions: [
          { name: "Students", total_nodes: 2, state: ["UP"], allow_qos: ["normal"], gpu_types: ["A100"] },
          { name: "GPU", total_nodes: 1, state: ["UP"], allow_qos: ["normal", "gpu"], gpu_types: ["A100"] },
        ],
        qos: [
          { name: "normal", max_cpus: 64, max_gpus: 4, source_authority: "visual-fixture" },
          { name: "gpu", max_cpus: 64, max_gpus: 8, source_authority: "visual-fixture" },
        ],
        dynamic_facts: [],
        limitations: [],
        snapshot_ref: {
          snapshot_id: "platform_visual",
          freshness: options.stalePlatform ? "stale" : "fresh",
          observed_at: options.stalePlatform ? "2020-01-01T00:00:00Z" : "2026-07-16T02:08:00Z",
        },
      });
    }
    if (url.pathname === "/api/v1/platform/snapshots/latest") {
      const capturedAt = options.stalePlatform ? "2020-01-01T00:00:00Z" : "2026-07-16T02:08:00Z";
      return json(route, {
        snapshot_id: "platform_visual",
        scope: "login_node",
        source_type: "worker",
        source_name: "visual-fixture",
        captured_at: capturedAt,
        observed_at: capturedAt,
        freshness: options.stalePlatform ? "stale" : "fresh",
        data_quality: options.stalePlatform ? "degraded" : "complete",
        collection_status: "succeeded",
        counts: { commands: 2, partitions: 2, nodes: 2, jobs: 1, limitations: 0 },
        snapshot: {
          snapshot_id: "platform_visual",
          scope: "login_node",
          captured_at: capturedAt,
          nodes: [
            { node_name: "node-a", partitions: ["Students"], state_normalized: "idle", cpus_total: 64, cpus_allocated: 16, memory_mb: 256000 },
            { node_name: "node-b", partitions: ["GPU"], state_normalized: "mixed", cpus_total: 64, cpus_allocated: 32, memory_mb: 256000 },
          ],
          squeue_jobs: [],
        },
        limitations: [],
      });
    }
    if (url.pathname === "/api/v1/platform/entitlements/latest") {
      const user = request.headers()["x-pilot107-user"] || "alice";
      return json(route, {
        snapshot_id: `entitlement_${user}`,
        captured_at: "2026-07-16T02:08:00Z",
        observed_at: "2026-07-16T02:08:00Z",
        freshness: "fresh",
        data_quality: "complete",
        default_account: user === "bob" ? "acct_bob" : "acct_alice",
        associations: [{
          account: user === "bob" ? "acct_bob" : "acct_alice",
          partition: "Students",
          qos: ["normal"],
          default_qos: "normal",
        }],
      });
    }
    if (url.pathname === "/api/v1/platform/observation") {'''
text, count = platform_pattern.subn(platform_replacement, text, count=1)
if count != 1:
    raise SystemExit(f"platform route block: expected 1 match, found {count}")

replace_once(
    '      return json(route, { items });\n'
    '    }\n'
    '    if (url.pathname === "/api/v1/runs/run_alice_failed/workspace") {',
    '      return json(route, { items, page: { limit: 20, has_more: false, next_cursor: null } });\n'
    '    }\n'
    '    if (url.pathname === "/api/v1/runs/run_alice_failed/workspace") {',
    label="run page read model",
)

agent_pattern = re.compile(
    r'    if \(url\.pathname === "/api/v1/agent/sessions"\) \{.*?\n'
    r'    if \(url\.pathname === "/api/v1/agent/conversations/conversation_visual/messages"\) \{.*?\n'
    r'    \}\n',
    re.S,
)
agent_replacement = '''    if (url.pathname === "/api/v1/agent-sessions") {
      return json(route, { items: [agentSession] });
    }
    if (url.pathname === "/api/v1/agent-sessions/agent_visual") {
      return json(route, agentSession);
    }
    if (url.pathname === "/api/v1/agent-sessions/agent_visual/events") {
      return json(route, {
        session_id: "agent_visual",
        items: agentEvents,
        page: { limit: 100, has_more: false, next_after_event_id: null, last_event_id: 3 },
      });
    }
'''
text, count = agent_pattern.subn(agent_replacement, text, count=1)
if count != 1:
    raise SystemExit(f"agent route block: expected 1 match, found {count}")

market_pattern = re.compile(
    r'function unifiedMarketItemPayload\(\) \{.*?\n\}\n\nfunction templateMarketPayload\(\)',
    re.S,
)
market_replacement = '''function unifiedMarketItemPayload() {
  return {
    kind: "curated_template",
    item_id: "curated_release_visual",
    title: "Verified Python CPU",
    description: "Verified on simulator",
    visibility: "public",
    scope_key: null,
    publisher: "alice",
    published_at: "2026-07-16T02:08:00Z",
    updated_at: "2026-07-16T02:08:00Z",
    tags: ["python"],
    adoption: { available: true, reason: null },
    withdrawn_at: null,
    template: {
      template_id: "template_visual",
      release_version: "1.1.0",
      content_sha256: "c".repeat(64),
    },
    contract_payload: defaultStudioContract(),
    compatibility: {},
    publication: {},
    metrics: {
      adoption_count: 1,
      verification_passed: 1,
      verification_failed: 0,
      verification_expired: 0,
      success_rate: 1,
      latest_verification: null,
    },
  };
}

function templateMarketPayload()'''
text, count = market_pattern.subn(market_replacement, text, count=1)
if count != 1:
    raise SystemExit(f"market item helper: expected 1 match, found {count}")

application_pattern = re.compile(
    r'function marketApplicationPayload\(overrides = \{\}\) \{.*?\n\}\n\nfunction agentSessionPayload\(\)',
    re.S,
)
application_replacement = '''function marketApplicationPayload(overrides = {}) {
  const completed = Boolean(overrides.completed);
  return {
    session_id: "market_application_visual",
    owner: "alice",
    request_key: "visual-market-application",
    source_kind: "curated_template",
    source_item_id: "curated_release_visual",
    source_digest: "e".repeat(64),
    assurance: "curated",
    user_intent: "将此市场条目安全地应用到我的私有实验工程",
    state: completed ? "completed" : "awaiting_confirmation",
    version: completed ? 2 : 1,
    project_id: "project_market_visual",
    workspace_id: "workspace_market_visual",
    change_set_id: "changeset_market_visual",
    target_contract_id: completed ? "contract_adopted_visual" : null,
    adoption_id: completed ? "adoption_visual" : null,
    target_contract_payload: defaultStudioContract(),
    plan_digest: "a".repeat(64),
    confirmation_digest: "b".repeat(64),
    change_set_digest: "c".repeat(64),
    created_at: "2026-07-16T02:08:00Z",
    updated_at: "2026-07-16T02:08:00Z",
  };
}

function agentSessionPayload()'''
text, count = application_pattern.subn(application_replacement, text, count=1)
if count != 1:
    raise SystemExit(f"market application helper: expected 1 match, found {count}")

agent_helpers_pattern = re.compile(
    r'function agentSessionPayload\(\) \{.*?\n\}\n\nfunction agentEventPayloads\(\) \{.*?\n\}\s*\Z',
    re.S,
)
agent_helpers_replacement = '''function agentSessionPayload() {
  return {
    session_id: "agent_visual",
    owner: "alice",
    request_key: "visual-agent-session",
    profile_id: "hpc-readonly-v1",
    model_profile_id: "campus-default",
    source: {},
    state: "idle",
    state_version: 1,
    resource_usage: {},
    outcome: null,
    created_at: "2026-07-16T02:08:00Z",
    updated_at: "2026-07-16T02:08:03Z",
  };
}

function agentEventPayloads() {
  return [
    {
      event_id: 1,
      turn_id: "turn_visual",
      session_id: "agent_visual",
      sequence: 1,
      event_type: "turn_started",
      payload: { task_kind: "interactive_readonly" },
      created_at: "2026-07-16T02:08:01Z",
    },
    {
      event_id: 2,
      turn_id: "turn_visual",
      session_id: "agent_visual",
      sequence: 2,
      event_type: "tool_call_completed",
      payload: {
        tool_call_id: "call_visual",
        tool_name: "platform_get_snapshot",
        result: { snapshot_id: "platform_visual" },
        is_error: false,
      },
      created_at: "2026-07-16T02:08:02Z",
    },
    {
      event_id: 3,
      turn_id: "turn_visual",
      session_id: "agent_visual",
      sequence: 3,
      event_type: "message_delta",
      payload: { delta: "排队原因是 Students 分区当前资源不足。" },
      created_at: "2026-07-16T02:08:03Z",
    },
  ];
}
'''
text, count = agent_helpers_pattern.subn(agent_helpers_replacement, text, count=1)
if count != 1:
    raise SystemExit(f"agent helpers: expected 1 match, found {count}")

PATH.write_text(text, encoding="utf-8")
