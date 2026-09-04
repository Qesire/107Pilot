const { expect, test } = require("@playwright/test");

const NOW = "2026-09-04T04:30:00Z";

// These scenarios replace the nine exact legacy visual cases superseded in
// playwright.config.cjs.  Their mocked responses intentionally use only the
// current browser read-model contracts; do not add legacy response aliases.
test.beforeEach(async ({ page }) => {
  await installCurrentContractApi(page);
});

test("current contract: workspace prioritizes current work and preparation facts", async ({ page }) => {
  await page.goto("/projects?user=alice");

  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前工作" })).toBeVisible();
  await expect(page.getByText("run_alice_failed", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("acct_alice", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("可见分区", { exact: true })).toBeVisible();
  await expect(page.getByText("1 次运行需要处理", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /查看原因/ })).toBeVisible();
});

test("current contract: run filters are URL-controlled and narrow the server query", async ({ page }) => {
  await page.goto("/runs?user=alice");

  await page.getByPlaceholder("搜索运行 ID、Job ID 或工作目录").fill("failed");
  await expect(page).toHaveURL(/q=failed/);
  await page.getByLabel("状态").selectOption("FAILED");
  await expect(page).toHaveURL(/state=FAILED/);
  await expect(page.getByRole("button", { name: "查看 run_alice_failed" })).toBeVisible();
  await expect(page.getByText("run_alice_succeeded", { exact: true })).toHaveCount(0);
});

test("current contract: switching user updates URL and invalidates scoped queries", async ({ page }) => {
  await page.goto("/projects?user=alice");
  await expect(page.getByText("run_alice_failed", { exact: true }).first()).toBeVisible();

  await page.getByLabel("当前用户").selectOption("bob");
  await expect(page).toHaveURL(/user=bob/);
  await expect(page.getByText("run_bob_running", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("acct_bob", { exact: true }).first()).toBeVisible();
});

test("current contract: stale and degraded dynamic facts remain explicit", async ({ page }) => {
  await installCurrentContractApi(page, { stalePlatform: true });
  await page.goto("/cluster?user=alice");

  await expect(page.getByText("stale", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("degraded", { exact: true }).first()).toBeVisible();
});

test("current contract: mobile layout exposes primary destinations without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/projects?user=alice");

  await expect(page.getByRole("navigation", { name: "主要导航" })).toBeVisible();
  await expect(page.locator('a[aria-label="工作台"]').first()).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(overflow).toBe(false);
});

test("current contract: studio requires server validation before creating a canonical contract", async ({ page }) => {
  await page.goto("/studio/new?user=alice");

  await expect(page.getByRole("heading", { name: "实验工作区" })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 Contract" })).toBeDisabled();
  await page.getByRole("textbox", { name: "工作目录", exact: true }).fill("/public/home/alice/studio-case");
  await page.getByRole("textbox", { name: /^runtime\.environment\.DATA_ROOT/ }).fill("/public/home/alice/dataset.tar.gz");
  await page.getByRole("button", { name: "服务端校验" }).click();
  await expect(page.getByText("服务器 OK", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 Contract" })).toBeEnabled();
  await page.getByRole("button", { name: "创建 Contract" }).click();
  await expect(page).toHaveURL(/\/studio\/contract_visual_001\?.*panel=script/);
});

test("current contract: dirty source is not silently overwritten by a basic form update", async ({ page }) => {
  await page.goto("/studio/new?user=alice&tab=source");
  const editor = page.locator(".cm-content");
  await editor.fill("schema_version: pilot107.contract/v2\nrecipe_version_id: changed-in-source\n");
  await page.getByRole("textbox", { name: "工作目录", exact: true }).fill("/public/home/alice/form-change");

  await expect(
    page.getByRole("alert").filter({ hasText: "表单与未应用源码发生冲突" }),
  ).toContainText("表单与未应用源码发生冲突");
  await expect(page.getByRole("button", { name: "应用源码并覆盖表单" })).toBeVisible();
});

test("current contract: market release adoption opens the server-created canonical contract", async ({ page }) => {
  await page.goto("/market?user=alice");
  await expect(page.getByRole("heading", { name: "作业与模板市场" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Verified Python CPU" })).toBeVisible();
  await page.getByRole("button", { name: /查看条目/ }).click();
  await expect(page).toHaveURL(/\/market\/curated_release_visual/);
  await expect(page.getByText("Canonical Contract payload", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "启动 Agent 应用计划" }).click();
  await expect(page.getByText("确认前 Contract 计划", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "确认精确计划并创建 Contract" }).click();
  await expect(page).toHaveURL(/\/studio\/contract_adopted_visual/);
});

test("current contract: Agent separates durable read-only conversation from controlled repair", async ({ page }) => {
  await page.goto("/agent?user=alice");

  await expect(page.getByRole("heading", { name: "持久化只读对话" })).toBeVisible();
  await expect(page.getByText("只读边界", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("排队原因是 Students 分区当前资源不足。", { exact: true })).toBeVisible();
  await expect(page.getByLabel(/事件 3/)).toBeVisible();

  await page.getByRole("button", { name: "修复" }).click();
  await expect(page).toHaveURL(/mode=repair/);
  await expect(page.getByRole("heading", { name: "可审计的修复会话" })).toBeVisible();
  await expect(page.getByLabel("Agent 会话筛选")).toBeVisible();

  await page.getByRole("button", { name: "对话" }).click();
  await expect(page).toHaveURL(/mode=conversation/);
});

async function installCurrentContractApi(page, options = {}) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const user = request.headers()["x-pilot107-user"] || "alice";

    if (url.pathname === "/api/v1/web/session") {
      return json(route, { identity_mode: "demo", user, switchable: true });
    }
    if (url.pathname === "/api/v1/health/ready") {
      return json(route, {
        status: "ready",
        checks: { database: { status: "ok" } },
        features: { llm: { enabled: false } },
      });
    }
    if (url.pathname === "/api/v1/files/usage") {
      return json(route, {
        home: `/public/home/${user}`,
        used_bytes: 1073741824,
        total_bytes: 2147483648,
        observed_at: NOW,
      });
    }
    if (url.pathname === "/api/v1/platform/capabilities") {
      if (options.capabilitiesForbidden) {
        return json(route, { error: { code: "FORBIDDEN", message: "scope denied" } }, 403);
      }
      return json(route, capabilityPayload());
    }
    if (url.pathname === "/api/v1/platform/snapshots/latest") {
      return json(route, platformSnapshotPayload(Boolean(options.stalePlatform)));
    }
    if (url.pathname === "/api/v1/platform/entitlements/latest") {
      return json(route, entitlementPayload(user));
    }
    if (url.pathname === "/api/v1/platform/connections") {
      return json(route, { items: [] });
    }
    if (url.pathname.startsWith("/api/v1/observability/connections/") && url.pathname.endsWith("/platform/latest")) {
      return json(route, { connection_id: "default", freshness: "fresh", measures: {} });
    }
    if (url.pathname === "/api/v1/runs") {
      const state = url.searchParams.get("state");
      const q = (url.searchParams.get("q") || "").toLowerCase();
      const allItems = user === "bob"
        ? [runBobPayload()]
        : [runFailedPayload(), runSucceededPayload()];
      const items = allItems
        .filter((item) => !state || item.state === state)
        .filter((item) => !q || JSON.stringify(item).toLowerCase().includes(q));
      return json(route, {
        items,
        page: {
          limit: Number(url.searchParams.get("limit") || 20),
          has_more: false,
          next_cursor: null,
        },
      });
    }
    if (url.pathname === "/api/v1/contracts/schema") {
      return json(route, contractSchemaPayload());
    }
    if (url.pathname === "/api/v1/recipes") {
      return json(route, {
        items: [{
          recipe_id: "recipe_python_cpu",
          latest_version: "1.0.0",
          title: "Python CPU",
          trust_level: "builtin",
          executable: true,
        }],
      });
    }
    if (url.pathname === "/api/v1/recipes/recipe_python_cpu/versions/1.0.0") {
      return json(route, {
        recipe_id: "recipe_python_cpu",
        version: "1.0.0",
        parameter_schema: {
          required: ["runtime.environment.DATA_ROOT"],
          "runtime.environment.DATA_ROOT": {
            type: "shared_path",
            prefix: "/public/home/alice",
            contract: "选择已存在的共享输入文件或目录。",
          },
        },
      });
    }
    if (url.pathname === "/api/v1/contracts/validate" && request.method() === "POST") {
      return json(route, contractValidationPayload(request.postDataJSON()));
    }
    if (url.pathname === "/api/v1/contracts" && request.method() === "POST") {
      return json(route, contractRecordPayload(request.postDataJSON()), 201);
    }
    if (url.pathname === "/api/v1/contracts/contract_adopted_visual") {
      return json(route, {
        ...contractRecordPayload(defaultStudioContract()),
        contract_id: "contract_adopted_visual",
        derivation_reason: "template_adoption",
      });
    }
    if (url.pathname === "/api/v1/market/items") {
      return json(route, {
        items: [marketItemPayload()],
        page: { limit: 20, has_more: false, next_cursor: null },
      });
    }
    if (url.pathname === "/api/v1/market/items/curated_release_visual") {
      return json(route, marketItemPayload());
    }
    if (url.pathname === "/api/v1/market/applications" && request.method() === "POST") {
      return json(route, marketApplicationPayload(false), 201);
    }
    if (
      url.pathname === "/api/v1/market/applications/market_application_visual/confirmation"
      && request.method() === "POST"
    ) {
      return json(route, marketApplicationPayload(true));
    }
    if (url.pathname === "/api/v1/agent-sessions") {
      return json(route, {
        items: [agentSessionPayload()],
        page: { limit: 20, has_more: false, next_cursor: null },
      });
    }
    if (url.pathname === "/api/v1/agent-sessions/agent_visual") {
      return json(route, agentSessionPayload());
    }
    if (url.pathname === "/api/v1/agent-sessions/agent_visual/events") {
      return json(route, {
        session_id: "agent_visual",
        items: [agentEventPayload()],
        page: { limit: 100, has_more: false, next_after_event_id: null, last_event_id: 3 },
      });
    }
    if (url.pathname === "/api/v1/remediation-sessions") {
      return json(route, { items: [] });
    }

    return json(route, { error: { code: "TEST.UNMOCKED", message: url.pathname } }, 404);
  });
}

function capabilityPayload() {
  return {
    profile_id: "visual-slurm",
    source_authority: "simulator",
    captured_at: NOW,
    freshness_seconds: 30,
    default_partition: "Students",
    default_qos: "normal",
    partitions: [
      { name: "Students", total_nodes: 2, gpu_types: [], allow_qos: ["normal"] },
      { name: "GPU", total_nodes: 1, gpu_types: ["A100"], allow_qos: ["normal", "gpu"] },
    ],
    qos: [
      { name: "normal", max_gpus: 0, source_authority: "simulator" },
      { name: "gpu", max_gpus: 1, source_authority: "simulator" },
    ],
    dynamic_facts: ["nodes", "jobs"],
    limitations: [],
  };
}

function platformSnapshotPayload(stale) {
  const capturedAt = stale ? "2020-01-01T00:00:00Z" : NOW;
  return {
    snapshot_id: "platform_visual",
    scope: "login_node",
    source_type: "simulator",
    source_name: "visual-fixture",
    captured_at: capturedAt,
    freshness: stale ? "stale" : "fresh",
    collection_status: stale ? "degraded" : "succeeded",
    data_quality: stale ? "degraded" : "ok",
    snapshot: {
      snapshot_id: "platform_visual",
      scope: "login_node",
      captured_at: capturedAt,
      nodes: [
        {
          node_name: "node-a",
          partitions: ["Students"],
          state_normalized: "idle",
          cpus_total: 64,
          cpus_allocated: 16,
          memory_mb: 256000,
        },
      ],
      squeue_jobs: [
        {
          job_id: "12345",
          state_raw: "PENDING",
          pending_reason: "Resources",
          partition: "Students",
          name: "visual",
        },
      ],
    },
  };
}

function entitlementPayload(user) {
  const account = user === "bob" ? "acct_bob" : "acct_alice";
  return {
    snapshot_id: `entitlement_${user}`,
    captured_at: NOW,
    observed_at: NOW,
    freshness: "fresh",
    data_quality: "ok",
    default_account: account,
    associations: [
      { account, partition: "Students", qos: ["normal"], default_qos: "normal" },
    ],
  };
}

function runFailedPayload() {
  return {
    run_id: "run_alice_failed",
    owner: "alice",
    contract_id: "contract_alice_002",
    state: "FAILED",
    job_id: "12345",
    job_name: "failed-visual",
    workdir: "/public/home/alice/failed-case",
    collection_state: "succeeded",
    created_at: NOW,
    updated_at: NOW,
  };
}

function runSucceededPayload() {
  return {
    run_id: "run_alice_succeeded",
    owner: "alice",
    contract_id: "contract_alice_001",
    state: "SUCCEEDED",
    job_id: "12344",
    job_name: "successful-visual",
    workdir: "/public/home/alice/success-case",
    collection_state: "succeeded",
    created_at: NOW,
    updated_at: NOW,
  };
}

function runBobPayload() {
  return {
    run_id: "run_bob_running",
    owner: "bob",
    contract_id: "contract_bob_001",
    state: "RUNNING",
    job_id: "22344",
    job_name: "bob-running",
    workdir: "/public/home/bob/current",
    collection_state: "pending",
    created_at: NOW,
    updated_at: NOW,
  };
}

function contractSchemaPayload() {
  return {
    type: "object",
    required: ["schema_version", "recipe_version_id", "project", "entry", "resources"],
    properties: {
      schema_version: { type: "string" },
      recipe_version_id: { type: "string" },
      project: {
        type: "object",
        required: ["workdir"],
        properties: { workdir: { type: "string" }, name: { type: "string" } },
      },
      entry: {
        type: "object",
        required: ["command"],
        properties: { command: { type: "string" } },
      },
      resources: { type: "object", additionalProperties: true },
    },
  };
}

function defaultStudioContract() {
  return {
    schema_version: "pilot107.contract/v2",
    recipe_version_id: "recipe_python_cpu@1.0.0",
    project: { name: "visual", workdir: "/public/home/alice/studio-case" },
    entry: { command: "python3 main.py" },
    resources: {
      partition: "Students",
      nodes: 1,
      cpus: 1,
      gpus: 0,
      memory: "1G",
      time: "00:10:00",
    },
    runtime: { environment: { DATA_ROOT: "/public/home/alice/dataset.tar.gz" } },
    outputs: { expected: [] },
  };
}

function contractValidationPayload(contract) {
  return {
    status: "OK",
    effective_request: { contract, contract_digest: "d".repeat(64) },
    findings: [],
    warnings: [],
  };
}

function contractRecordPayload(contract) {
  return {
    contract_id: "contract_visual_001",
    owner: "alice",
    digest: "d".repeat(64),
    contract,
    created_at: NOW,
    updated_at: NOW,
  };
}

function marketItemPayload() {
  return {
    kind: "curated_template",
    item_id: "curated_release_visual",
    title: "Verified Python CPU",
    description: "Verified on simulator",
    visibility: "public",
    scope_key: null,
    publisher: "alice",
    published_at: NOW,
    updated_at: NOW,
    tags: ["python", "cpu"],
    adoption: { available: true, reason: null },
    withdrawn_at: null,
    template: {
      template_id: "template_visual",
      release_version: "1.1.0",
      content_sha256: "c".repeat(64),
    },
    contract_payload: defaultStudioContract(),
    compatibility: { partitions: ["Students"], gpu: false },
    publication: { verification_environment: "docker" },
    metrics: {
      adoption_count: 2,
      verification_passed: 1,
      verification_failed: 0,
      verification_expired: 0,
      success_rate: 1,
      latest_verification: null,
    },
  };
}

function marketApplicationPayload(completed) {
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
    created_at: NOW,
    updated_at: NOW,
  };
}

function agentSessionPayload() {
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
    created_at: NOW,
    updated_at: NOW,
  };
}

function agentEventPayload() {
  return {
    event_id: 3,
    turn_id: "turn_visual",
    session_id: "agent_visual",
    sequence: 3,
    event_type: "message_delta",
    payload: { delta: "排队原因是 Students 分区当前资源不足。" },
    created_at: NOW,
  };
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}
