const fs = require("node:fs");
const path = require("node:path");
const { expect, test } = require("@playwright/test");

const screenshotDir = path.resolve(__dirname, "../../artifacts/visual-regression");

test.beforeEach(async ({ page }) => {
  fs.mkdirSync(screenshotDir, { recursive: true });
  await installMockApi(page);
});

test("workspace renders live run and platform read models", async ({ page }) => {
  await page.goto("/projects?user=alice");

  await expect(page.getByRole("heading", { name: "把下一次提交建立在可验证事实之上" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看 run_alice_succeeded" })).toBeVisible();
  await expect(page.getByText("acct_alice", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("可见分区", { exact: true })).toBeVisible();
  await expect(page.locator(".signal-strip").getByText("2", { exact: true })).toBeVisible();
  await capture(page, "phase3d-workspace.png");
});

test("run filters are URL-controlled and narrow the server query", async ({ page }) => {
  await page.goto("/runs?user=alice");

  await page.getByPlaceholder("搜索 Run ID、Job ID 或 workdir").fill("failed");
  await expect(page).toHaveURL(/q=failed/);
  await page.getByLabel("状态").selectOption("FAILED");
  await expect(page).toHaveURL(/state=FAILED/);
  await expect(page.getByRole("button", { name: "查看 run_alice_failed" })).toBeVisible();
  await expect(page.getByText("run_alice_succeeded", { exact: true })).toHaveCount(0);
});

test("run deep link survives direct navigation and wraps long workdir", async ({ page }) => {
  await page.goto("/runs/run_alice_failed?user=alice");

  await expect(page.getByRole("heading", { name: "Run 详情" })).toBeVisible();
  await expect(page.getByText("contract_alice_002", { exact: true })).toBeVisible();
  await expect(page.getByText("/work/alice/projects/a-very-long-directory-name/failed-case", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "诊断" }).click();
  await expect(page).toHaveURL(/tab=diagnosis/);
  await expect(page.getByText("RUNTIME.PYTHON_PACKAGE_MISSING", { exact: true })).toBeVisible();
  await expect(page.getByText("evidence:\/\/runs\/run_alice_failed\/logs\/stderr.tail.json", { exact: true })).toBeVisible();
});

test("successful run exposes shareable logs, results, and verified capsule", async ({ page }) => {
  await page.goto("/runs/run_alice_succeeded?user=alice");

  await page.getByRole("button", { name: "日志" }).click();
  await expect(page).toHaveURL(/tab=logs/);
  await expect(page.getByText("training complete", { exact: false })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("store_path");

  await page.getByRole("button", { name: "结果" }).click();
  await expect(page.getByText("outputs/result.txt", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /outputs\/result.txt/ }).click();
  await expect(page).toHaveURL(/object=ev_result/);
  await expect(page.getByText("accuracy=0.91", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Capsule" }).click();
  await expect(page.getByText("Capsule checksum 验证通过", { exact: true })).toBeVisible();
  await capture(page, "phase3d-run-evidence.png");
});

test("run detail makes an omitted workdir explicit", async ({ page }) => {
  await installMockApi(page, { omitWorkdir: true });
  await page.goto("/runs/run_alice_failed?user=alice");

  await expect(page.getByText("服务器 read model 未公开", { exact: true })).toBeVisible();
});

test("switching user updates URL and invalidates scoped queries", async ({ page }) => {
  await page.goto("/projects?user=alice");
  await expect(page.getByRole("button", { name: "查看 run_alice_succeeded" })).toBeVisible();

  await page.getByLabel("当前用户").selectOption("bob");
  await expect(page).toHaveURL(/user=bob/);
  await expect(page.getByRole("button", { name: "查看 run_bob_running" })).toBeVisible();
  await expect(page.getByText("acct_bob", { exact: true }).first()).toBeVisible();
});

test("an untrusted URL user is normalized before it reaches API or shell output", async ({ page }) => {
  await page.goto("/studio/new?user=alice%27%3Btouch%20%2Ftmp%2Fpwned&tab=terminal");

  await expect(page).toHaveURL(/user=alice(&|$)/);
  await expect(page.getByLabel("当前用户")).toHaveValue("alice");
  await expect(page.getByText("touch /tmp/pwned", { exact: false })).toHaveCount(0);
});

test("stale and degraded dynamic facts remain explicit", async ({ page }) => {
  await installMockApi(page, { stalePlatform: true });
  await page.goto("/cluster?user=alice");

  await expect(page.getByText("stale", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("degraded", { exact: true }).first()).toBeVisible();
});

test("forbidden capability scope renders an authorization state", async ({ page }) => {
  await installMockApi(page, { capabilitiesForbidden: true });
  await page.goto("/cluster?user=alice");

  await expect(page.getByRole("alert")).toContainText("无权查看此范围");
  await expect(page.getByRole("alert")).toContainText("scope denied");
});

test("mobile layout exposes primary destinations without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/projects?user=alice");

  await expect(page.getByRole("navigation", { name: "主要导航" })).toBeVisible();
  await expect(page.getByRole("link", { name: /工作台/ })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflow).toBe(false);
  await capture(page, "phase3d-workspace-mobile.png");
});

test("studio requires server validation before creating a canonical contract", async ({ page }) => {
  await page.goto("/studio/new?user=alice");

  await expect(page.getByRole("heading", { name: "新建 Contract" })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 Contract" })).toBeDisabled();
  await page.getByLabel("Workdir").fill("/public/home/alice/studio-case");
  await page.getByRole("button", { name: "服务端校验" }).click();
  await expect(page.getByText("服务器 OK", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 Contract" })).toBeEnabled();
  await page.getByRole("button", { name: "创建 Contract" }).click();
  await expect(page).toHaveURL(/\/studio\/contract_visual_001\?.*panel=script/);
});

test("dirty source is not silently overwritten by a basic form update", async ({ page }) => {
  await page.goto("/studio/new?user=alice&tab=source");
  const editor = page.locator(".cm-content");
  await editor.fill("schema_version: pilot107.contract/v2\nrecipe_version_id: changed-in-source\n");
  await page.getByLabel("Workdir").fill("/public/home/alice/form-change");

  await expect(page.getByRole("alert")).toContainText("表单与未应用源码发生冲突");
  await expect(page.getByRole("button", { name: "应用源码并覆盖表单" })).toBeVisible();
});

test("market release adoption opens the server-created canonical contract", async ({ page }) => {
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

test("persisted contract requires object-level confirmation before submit", async ({ page }) => {
  await page.goto("/studio/contract_visual_001?user=alice&tab=basic");
  await page.getByRole("button", { name: "运行动态预检" }).click();
  await expect(page.getByText("Preflight OK", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "准备 Run" }).click();
  await expect(page.getByText("run_studio_prepared", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认提交 run_studio_prepared" })).toBeDisabled();
  await page.getByLabel("我确认提交 Run run_studio_prepared").check();
  await page.getByRole("button", { name: "确认提交 run_studio_prepared" }).click();
  await expect(page).toHaveURL(/\/runs\/run_studio_prepared/);
});

test("Agent separates durable read-only conversation from controlled repair", async ({ page }) => {
  await page.goto("/agent?user=alice");

  await expect(page.getByRole("heading", { name: "持久化只读对话" })).toBeVisible();
  await expect(page.getByText("只读边界", { exact: true })).toBeVisible();
  await expect(page.getByText("排队原因是 Students 分区当前资源不足。", { exact: true })).toBeVisible();
  await expect(page.getByLabel("事件 3")).toBeVisible();

  await page.getByRole("button", { name: "修复" }).click();
  await expect(page).toHaveURL(/mode=repair/);
  await expect(page.getByRole("heading", { name: "可审计的修复会话" })).toBeVisible();
  await expect(page.getByLabel("Agent 会话筛选")).toBeVisible();

  await page.getByRole("button", { name: "对话" }).click();
  await expect(page).toHaveURL(/mode=conversation/);
  await capture(page, "agent-durable-conversation.png");
});

async function capture(page, filename) {
  await page.screenshot({
    path: path.join(screenshotDir, filename),
    fullPage: true,
  });
}

async function installMockApi(page, options = {}) {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  const agentSession = agentSessionPayload();
  const agentEvents = agentEventPayloads();
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/healthz") {
      return json(route, { status: "ok" });
    }
    if (url.pathname === "/api/v1/health/ready") {
      return json(route, { status: "ready", checks: { database: { status: "ok" } } });
    }
    if (url.pathname === "/api/v1/web/session") {
      const requestedUser = request.headers()["x-pilot107-user"] || "alice";
      return json(route, { identity_mode: "demo", user: requestedUser, switchable: true });
    }
    if (url.pathname === "/api/v1/contracts/schema") {
      return json(route, contractSchemaPayload());
    }
    if (url.pathname === "/api/v1/recipes") {
      return json(route, {
        items: [
          {
            recipe_id: "recipe_python_cpu",
            latest_version: "1.0.0",
            title: "Python CPU",
            trust_level: "builtin",
            executable: true,
          },
        ],
      });
    }
    if (url.pathname === "/api/v1/contracts/validate" && request.method() === "POST") {
      const contract = request.postDataJSON();
      return json(route, contractValidationPayload(contract));
    }
    if (url.pathname === "/api/v1/contracts" && request.method() === "POST") {
      const contract = request.postDataJSON();
      return json(route, contractRecordPayload(contract), 201);
    }
    if (url.pathname === "/api/v1/contracts/contract_visual_001") {
      return json(route, contractRecordPayload(defaultStudioContract()));
    }
    if (url.pathname === "/api/v1/contracts/contract_adopted_visual") {
      return json(route, { ...contractRecordPayload(defaultStudioContract()), contract_id: "contract_adopted_visual", derivation_reason: "template_adoption" });
    }
    if (url.pathname === "/api/v1/contracts/contract_visual_001/preflight" && request.method() === "POST") {
      return json(route, contractValidationPayload(defaultStudioContract()));
    }
    if (url.pathname === "/api/v1/runs/prepare" && request.method() === "POST") {
      return json(route, preparedRunPayload(), 201);
    }
    if (url.pathname === "/api/v1/runs/run_studio_prepared/submit" && request.method() === "POST") {
      return json(route, preparedRunPayload({ state: "SUBMITTED" }));
    }
    if (url.pathname === "/api/v1/templates") {
      return json(route, { items: [templateMarketPayload()], page: { limit: 20, has_more: false, next_cursor: null } });
    }
    if (url.pathname === "/api/v1/market/items") {
      return json(route, {
        items: [unifiedMarketItemPayload()],
        page: { limit: 20, has_more: false, next_cursor: null },
      });
    }
    if (url.pathname === "/api/v1/market/items/curated_release_visual") {
      return json(route, unifiedMarketItemPayload());
    }
    if (url.pathname === "/api/v1/market/applications" && request.method() === "POST") {
      return json(route, marketApplicationPayload(), 201);
    }
    if (
      url.pathname === "/api/v1/market/applications/market_application_visual/confirmation"
      && request.method() === "POST"
    ) {
      return json(route, marketApplicationPayload({ completed: true }));
    }
    if (url.pathname === "/api/v1/templates/template_visual/releases/1.1.0") {
      return json(route, templateMarketPayload());
    }
    if (url.pathname === "/api/v1/templates/template_visual/diff") {
      return json(route, { template_id: "template_visual", from: { release_id: "release_100", release_version: "1.0.0", content_sha256: "b".repeat(64) }, to: { release_id: "release_110", release_version: "1.1.0", content_sha256: "c".repeat(64) }, changes: [{ path: "/payload/resources/nodes", before: 1, after: 2 }] });
    }
    if (url.pathname === "/api/v1/templates/template_visual/releases/1.1.0/adopt" && request.method() === "POST") {
      return json(route, { adoption_id: "adoption_visual", release_id: "release_110", adopter: "alice", request_key: request.postDataJSON().request_key, target_template_id: "template_private_visual", target_draft_id: "draft_visual", target_contract_id: "contract_adopted_visual", created_at: "2026-07-16T02:08:00Z" }, 201);
    }
    if (url.pathname === "/api/v1/platform/capabilities") {
      if (options.capabilitiesForbidden) {
        return json(route, { error: { code: "AUTH.FORBIDDEN", message: "scope denied" } }, 403);
      }
      return json(route, capabilityPayload());
    }
    if (url.pathname === "/api/v1/platform/snapshots/latest") {
      return json(route, platformPayload(Boolean(options.stalePlatform)));
    }
    if (url.pathname === "/api/v1/platform/entitlements/latest") {
      return json(route, entitlementPayload(url.searchParams.get("owner") || "alice"));
    }
    if (url.pathname === "/api/v1/agent-sessions" && request.method() === "GET") {
      return json(route, {
        items: [agentSession],
        page: { limit: 100, has_more: false, next_cursor: null },
      });
    }
    if (url.pathname === "/api/v1/agent-sessions/session_visual_001") {
      return json(route, agentSession);
    }
    if (url.pathname === "/api/v1/agent-sessions/session_visual_001/events") {
      const after = Number(url.searchParams.get("after_event_id") || "0");
      const items = agentEvents.filter((item) => item.event_id > after);
      return json(route, {
        session_id: agentSession.session_id,
        items,
        page: {
          limit: 100,
          has_more: false,
          next_after_event_id: null,
          last_event_id: items.at(-1)?.event_id ?? after,
        },
      });
    }
    if (url.pathname === "/api/v1/remediation-sessions") {
      return json(route, { items: [] });
    }
    if (url.pathname === "/api/v1/runs") {
      const owner = url.searchParams.get("owner") || "alice";
      const state = url.searchParams.get("state");
      const query = (url.searchParams.get("q") || "").toLowerCase();
      const limit = Number(url.searchParams.get("limit") || "20");
      const filtered = runsFor(owner).filter((run) => {
        const matchesState = !state || run.state === state;
        const haystack = [run.run_id, run.job_id, run.workdir].join(" ").toLowerCase();
        return matchesState && (!query || haystack.includes(query));
      });
      return json(route, {
        items: filtered.slice(0, limit),
        page: { limit, has_more: filtered.length > limit, next_cursor: null },
      });
    }
    const evidenceObjectMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/evidence\/objects\/([^/]+)$/);
    if (evidenceObjectMatch) {
      const [, runId, objectId] = evidenceObjectMatch.map(decodeURIComponent);
      const preview = evidencePreviewPayload(runId, objectId);
      return preview
        ? json(route, preview)
        : json(route, { error: { code: "EVIDENCE.OBJECT_NOT_FOUND", message: "object not found" } }, 404);
    }
    const evidenceMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/evidence$/);
    if (evidenceMatch) {
      return json(route, evidencePayload(decodeURIComponent(evidenceMatch[1])));
    }
    const diagnosesMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/(diagnoses|diagnose)$/);
    if (diagnosesMatch) {
      return json(route, diagnosesPayload(decodeURIComponent(diagnosesMatch[1])));
    }
    const capsuleMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/capsule$/);
    if (capsuleMatch) {
      const runId = decodeURIComponent(capsuleMatch[1]);
      const run = runsFor("alice").find((item) => item.run_id === runId);
      return json(route, capsulePayload(run));
    }
    if (url.pathname.startsWith("/api/v1/runs/")) {
      const runId = decodeURIComponent(url.pathname.slice("/api/v1/runs/".length));
      const run = [...runsFor("alice"), ...runsFor("bob")].find((item) => item.run_id === runId);
      const detail = run && options.omitWorkdir ? { ...run, workdir: undefined } : run;
      return detail
        ? json(route, detail)
        : json(route, { error: { code: "RUN.NOT_FOUND", message: "run not found" } }, 404);
    }

    return route.continue();
  });
}

function agentSessionPayload() {
  return {
    session_id: "session_visual_001",
    owner: "alice",
    request_key: "visual-session-1",
    profile_id: "hpc-readonly-v1",
    model_profile_id: "campus-default",
    source: { run_id: "run_alice_failed" },
    state: "idle",
    state_version: 4,
    resource_usage: { input_tokens: 128, output_tokens: 42 },
    outcome: { status: "completed" },
    created_at: "2026-07-16T02:00:00Z",
    updated_at: "2026-07-16T02:05:00Z",
  };
}

function agentEventPayloads() {
  return [
    {
      event_id: 1,
      turn_id: "turn_visual_001",
      session_id: "session_visual_001",
      sequence: 1,
      event_type: "turn_started",
      payload: { model_profile_id: "campus-default", task_kind: "interactive_readonly" },
      created_at: "2026-07-16T02:03:00Z",
    },
    {
      event_id: 2,
      turn_id: "turn_visual_001",
      session_id: "session_visual_001",
      sequence: 2,
      event_type: "tool_call_completed",
      payload: { tool_call_id: "tool-1", tool_name: "run_get", result: {}, is_error: false },
      created_at: "2026-07-16T02:03:01Z",
    },
    {
      event_id: 3,
      turn_id: "turn_visual_001",
      session_id: "session_visual_001",
      sequence: 3,
      event_type: "message_delta",
      payload: { delta: "排队原因是 Students 分区当前资源不足。" },
      created_at: "2026-07-16T02:03:02Z",
    },
  ];
}

function runsFor(owner) {
  if (owner === "bob") {
    return [
      runPayload({
        runId: "run_bob_running",
        owner,
        state: "RUNNING",
        jobId: "70003",
        contractId: "contract_bob_001",
        workdir: "/work/bob/active-case",
        collectionState: "pending",
      }),
    ];
  }
  return [
    runPayload({
      runId: "run_alice_succeeded",
      owner,
      state: "SUCCEEDED",
      jobId: "70001",
      contractId: "contract_alice_001",
      workdir: "/work/alice/success-case",
      collectionState: "succeeded",
    }),
    runPayload({
      runId: "run_alice_failed",
      owner,
      state: "FAILED",
      jobId: "70002",
      contractId: "contract_alice_002",
      workdir: "/work/alice/projects/a-very-long-directory-name/failed-case",
      collectionState: "failed",
      exitCode: "1:0",
    }),
  ];
}

function runPayload({ runId, owner, state, jobId, contractId, workdir, collectionState, exitCode = null }) {
  return {
    run_id: runId,
    contract_id: contractId,
    owner,
    state,
    collection_state: collectionState,
    diagnosis_state: state === "FAILED" ? "succeeded" : "pending",
    capsule_state: state === "SUCCEEDED" ? "ready" : "pending",
    result_status: state.toLowerCase(),
    job_id: jobId,
    exit_code: exitCode,
    workdir,
    recipe_version_id: "recipe_python_cpu@1.0.0",
    created_at: "2026-07-16T02:00:00Z",
    updated_at: "2026-07-16T02:05:00Z",
  };
}

function evidencePayload(runId) {
  const run = runsFor("alice").find((item) => item.run_id === runId) || runsFor("bob")[0];
  const objects = evidenceObjects(runId);
  return {
    run_id: runId,
    owner: run.owner,
    job_id: run.job_id,
    run_state: run.state,
    collection_state: run.collection_state,
    tasks: [
      { task_id: 1, task_type: "collect_terminal", state: run.collection_state, attempts: 1, updated_at: run.updated_at },
    ],
    objects,
    tree: { name: runId, kind: "directory", logical_path: "", children: [] },
  };
}

function evidenceObjects(runId) {
  const base = `evidence://runs/${runId}`;
  return [
    evidenceObject("ev_stdout", "logs", "logs/stdout.tail.json", `${base}/logs/stdout.tail.json`, "application/json", 72),
    evidenceObject("ev_stderr", "logs", "logs/stderr.tail.json", `${base}/logs/stderr.tail.json`, "application/json", 48),
    evidenceObject("ev_summary", "derived", "derived/result_summary.v1.json", `${base}/derived/result_summary.v1.json`, "application/json", 94),
    evidenceObject("ev_result", "outputs", "outputs/result.txt", `${base}/outputs/result.txt`, "text/plain", 14),
  ];
}

function evidenceObject(objectId, category, logicalPath, sourceUri, mimeType, sizeBytes) {
  return {
    object_id: objectId,
    category,
    logical_path: logicalPath,
    source_uri: sourceUri,
    sha256: "d".repeat(64),
    size_bytes: sizeBytes,
    mime_type: mimeType,
    collection_status: "collected",
    mutable_during_run: false,
    finalized_at: "2026-07-16T02:05:00Z",
  };
}

function evidencePreviewPayload(runId, objectId) {
  const object = evidenceObjects(runId).find((item) => item.object_id === objectId);
  if (!object) return null;
  const content = {
    ev_stdout: JSON.stringify({ stream: "stdout", tail: "epoch 4\ntraining complete\n" }),
    ev_stderr: JSON.stringify({ stream: "stderr", tail: runId.includes("failed") ? "ModuleNotFoundError: No module named 'numpy'\n" : "" }),
    ev_summary: JSON.stringify({ result_status: runId.includes("failed") ? "failed" : "succeeded", outputs: { file_count: 1, total_size_bytes: 14 } }),
    ev_result: "accuracy=0.91\n",
  }[objectId];
  return {
    ...object,
    preview: {
      available: true,
      content,
      encoding: "utf-8",
      bytes_read: Buffer.byteLength(content),
      max_bytes: 131072,
      truncated: false,
      integrity: "verified",
    },
  };
}

function diagnosesPayload(runId) {
  if (!runId.includes("failed")) return { run_id: runId, diagnosis_state: "skipped", items: [] };
  return {
    run_id: runId,
    diagnosis_state: "succeeded",
    items: [
      {
        diagnosis_id: "diagnosis_visual_001",
        run_id: runId,
        rule_id: "RUNTIME.PYTHON_PACKAGE_MISSING",
        severity: "error",
        summary: "Python package is missing from the runtime environment.",
        evidence_refs: [`evidence://runs/${runId}/logs/stderr.tail.json`],
        suggested_patch: { runtime: { conda_env: "ml" } },
        retryable: true,
        confidence: "high",
        category: "runtime",
        stage: "execution",
        fix_guide: { fix: "Use an environment that provides the missing package." },
        created_at: "2026-07-16T02:05:00Z",
      },
    ],
  };
}

function capsulePayload(run) {
  if (!run) return { error: { code: "RUN.NOT_FOUND", message: "run not found" } };
  if (run.state !== "SUCCEEDED") return { ...run, capsule: null };
  return {
    ...run,
    capsule: {
      run_id: run.run_id,
      capsule_id: `capsule_${run.run_id}`,
      manifest_sha256: "e".repeat(64),
      files_copied: 4,
      valid: true,
      checked_files: 4,
      manifest: { schema_version: "pilot107.raw-capsule/v1", run_id: run.run_id },
      warnings: [],
      errors: [],
    },
  };
}

function capabilityPayload() {
  return {
    profile_id: "docker-real107-sim",
    source_authority: "simulated_slurm",
    captured_at: "2026-07-16T02:04:00Z",
    freshness_seconds: 300,
    default_partition: "Students",
    default_qos: "qos_stu_medium_2gpu",
    partitions: [
      {
        name: "Students",
        nodes: "cpu-[001-004]",
        total_nodes: 4,
        state: ["idle"],
        allow_qos: ["qos_stu_medium_2gpu"],
        gpu_types: [],
      },
      {
        name: "GPU",
        nodes: "gpu-[001-002]",
        total_nodes: 2,
        state: ["mixed"],
        allow_qos: ["qos_gpu_short"],
        gpu_types: ["a100"],
      },
    ],
    qos: [
      {
        name: "qos_stu_medium_2gpu",
        max_cpus: 24,
        max_gpus: 2,
        max_memory_gb: 128,
        max_wall_hours: 12,
        source_authority: "simulated_slurm",
      },
    ],
    dynamic_facts: ["node_state", "partition_capacity"],
    limitations: ["模拟环境不代表真实 107 当前空闲资源。"],
  };
}

function platformPayload(stale) {
  return {
    snapshot_id: "snapshot_platform_001",
    scope: "login_node",
    source_type: "simulated_slurm",
    observed_at: "2026-07-16T02:04:00Z",
    freshness: stale ? "stale" : "fresh",
    data_quality: stale ? "degraded" : "ok",
    facts: { hostname: "login-sim", partitions: 2 },
    limitations: stale ? ["采集时间超过 freshness 阈值。"] : [],
  };
}

function entitlementPayload(owner) {
  return {
    snapshot_id: `snapshot_entitlement_${owner}`,
    observed_at: "2026-07-16T02:04:00Z",
    freshness: "fresh",
    data_quality: "ok",
    default_account: `acct_${owner}`,
    associations: [
      { account: `acct_${owner}`, partition: "Students", qos: ["qos_stu_medium_2gpu"] },
    ],
  };
}

function contractSchemaPayload() {
  return {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    required: ["schema_version", "recipe_version_id", "project", "entry", "resources"],
    properties: {
      schema_version: { const: "pilot107.contract/v2" },
      recipe_version_id: { type: "string", minLength: 1 },
      project: { type: "object" },
      entry: { type: "object" },
      resources: { type: "object" },
    },
    additionalProperties: true,
  };
}

function defaultStudioContract() {
  return {
    schema_version: "pilot107.contract/v2",
    recipe_version_id: "recipe_python_cpu@1.0.0",
    project: { name: "", workdir: "/public/home/alice" },
    entry: { command: "python3 main.py" },
    runtime: { conda_env: null, container_image: null, modules: [], environment: {} },
    resources: {
      partition: "Students",
      qos: "qos_stu_medium_2gpu",
      nodes: 1,
      ntasks: 1,
      cpus_per_task: 1,
      memory: "4G",
      gpus_per_node: null,
      time_limit: "00:30:00",
      array: null,
    },
    workflow: { dependencies: [], retry: { max_attempts: 1, backoff_seconds: 0 } },
    outputs: { expected: [], success_conditions: ["slurm_exit_code_zero"] },
    policy: { automation_level: "explain", max_remediation_attempts: 0, require_approval: true },
    extensions: {},
  };
}

function contractValidationPayload(contract) {
  return {
    status: "OK",
    findings: [],
    effective_request: {
      recipe_version_id: contract.recipe_version_id,
      schema_version: contract.schema_version,
      contract_digest: "a".repeat(64),
      contract,
      workdir: contract.project.workdir,
      script: "#!/bin/bash\npython3 main.py\n",
      materializer: "generic_command",
      resource_plan: contract.resources,
    },
    risk_lint: [],
    configuration_snapshot_id: "visual-fixture",
    observed_at: "2026-07-16T02:05:00Z",
  };
}

function contractRecordPayload(contract) {
  return {
    contract_id: "contract_visual_001",
    owner: "alice",
    recipe_version_id: contract.recipe_version_id,
    schema_version: contract.schema_version,
    digest: "a".repeat(64),
    contract,
    field_sources: [],
    created_at: "2026-07-16T02:05:00Z",
    updated_at: "2026-07-16T02:05:00Z",
  };
}

function templateMarketPayload() {
  return {
    release_id: "release_110",
    template_id: "template_visual",
    release_version: "1.1.0",
    publisher: "instructor",
    title: "Verified Python CPU",
    description: "A reviewed CPU template with canonical Contract payload.",
    visibility: "public",
    scope_key: null,
    payload: defaultStudioContract(),
    compatibility: { partitions: ["Students"], gpu: false },
    publication: { tags: ["python", "cpu"] },
    gate_report: { status: "passed" },
    content_sha256: "c".repeat(64),
    published_at: "2026-07-16T02:06:00Z",
    withdrawn_at: null,
    withdrawal_reason: null,
    metrics: { adoption_count: 3, verification_passed: 2, verification_failed: 0, verification_expired: 0, success_rate: 1, latest_verification: null },
  };
}

function unifiedMarketItemPayload() {
  const release = templateMarketPayload();
  return {
    kind: "curated_template",
    item_id: "curated_release_visual",
    title: release.title,
    description: release.description,
    visibility: release.visibility,
    scope_key: release.scope_key,
    publisher: release.publisher,
    published_at: release.published_at,
    updated_at: release.published_at,
    tags: release.publication.tags,
    adoption: { available: true, reason: null },
    withdrawn_at: null,
    template: {
      template_id: release.template_id,
      release_version: release.release_version,
      content_sha256: release.content_sha256,
    },
    contract_payload: release.payload,
    compatibility: release.compatibility,
    publication: release.publication,
    metrics: release.metrics,
  };
}

function marketApplicationPayload(options = {}) {
  const completed = Boolean(options.completed);
  return {
    session_id: "market_application_visual",
    owner: "alice",
    request_key: "web-adopt-market-item-visual",
    source_kind: "curated_template",
    source_item_id: "curated_release_visual",
    source_digest: "d".repeat(64),
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
    plan_digest: "e".repeat(64),
    confirmation_digest: "f".repeat(64),
    change_set_digest: "a".repeat(64),
    created_at: "2026-07-16T02:08:00Z",
    updated_at: "2026-07-16T02:08:00Z",
  };
}

function preparedRunPayload(options = {}) {
  return {
    run_id: "run_studio_prepared",
    contract_id: "contract_visual_001",
    owner: "alice",
    state: options.state || "PREPARED",
    collection_state: "pending",
    diagnosis_state: "pending",
    capsule_state: "pending",
    result_status: "pending",
    job_id: options.state ? "job_visual" : null,
    exit_code: null,
    created_at: "2026-07-16T02:09:00Z",
    updated_at: "2026-07-16T02:09:00Z",
    preview: { submitted_script: "#!/bin/bash\npython3 main.py\n", execution_wrapper: "#!/bin/bash\n" },
    risk_lint: [],
    preflight: [],
  };
}

async function json(route, body, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}
