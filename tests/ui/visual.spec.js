const fs = require("node:fs");
const path = require("node:path");
const { expect, test } = require("@playwright/test");

const screenshotDir = path.resolve(__dirname, "../../artifacts/visual-regression");

test.beforeEach(async ({ page }) => {
  fs.mkdirSync(screenshotDir, { recursive: true });
  await installMockApi(page);
});

test("workspace prioritizes explicit WorkArea and preparation facts", async ({ page }) => {
  await page.goto("/projects?user=alice");

  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
  const switcher = page.getByRole("combobox", { name: "当前研究区" });
  await expect(switcher).toHaveValue("");
  await switcher.selectOption("workarea_visual_alice");
  await expect(page.getByRole("heading", { name: "当前研究区" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看 run_alice_succeeded" })).toBeVisible();
  await expect(page.getByText("acct_alice", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("可见分区", { exact: true })).toBeVisible();
  await expect(page.getByText("1 次运行需要处理", { exact: true })).toBeVisible();
  await capture(page, "phase3d-workspace.png");
});

test("file workspace consumes backend storage and upload session read models", async ({ page }) => {
  await page.goto("/files?user=alice");

  await expect(page.getByRole("heading", { name: "文件工作区" })).toBeVisible();
  await expect(page.getByText("个人存储", { exact: true })).toBeVisible();
  await expect(page.getByText("后台传输", { exact: true })).toBeVisible();
  await expect(page.getByText("dataset.tar.gz", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/完整性已验证，正在写入/)).toBeVisible();
  await expect(page.getByLabel("压缩包上传后处理")).toHaveValue("keep");
});

test("run filters are URL-controlled and narrow the server query", async ({ page }) => {
  await page.goto("/runs?user=alice");

  await page.getByPlaceholder("搜索运行 ID、Job ID 或工作目录").fill("failed");
  await expect(page).toHaveURL(/q=failed/);
  await page.getByLabel("状态").selectOption("FAILED");
  await expect(page).toHaveURL(/state=FAILED/);
  await expect(page.getByRole("button", { name: "查看 run_alice_failed" })).toBeVisible();
  await expect(page.getByText("run_alice_succeeded", { exact: true })).toHaveCount(0);
});

test("run deep link survives direct navigation and wraps long workdir", async ({ page }) => {
  await page.goto("/runs/run_alice_failed?user=alice");

  await expect(page.getByRole("heading", { name: "运行详情", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行视图", level: 2 })).toBeVisible();
  await expect(
    page.getByTestId("run-workspace-overview").getByText("contract_alice_002", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByTestId("run-workspace-overview").getByText("/work/alice/projects/a-very-long-directory-name/failed-case", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Run Evidence 视图").getByRole("button", { name: "诊断", exact: true }).click();
  await expect(page).toHaveURL(/tab=diagnosis/);
  await expect(page.getByText("RUNTIME.PYTHON_PACKAGE_MISSING", { exact: true })).toBeVisible();
  await expect(page.getByText("evidence://runs/run_alice_failed/logs/stderr.tail.json", { exact: true })).toBeVisible();
});

test("successful run exposes shareable logs, results, and verified capsule", async ({ page }) => {
  await page.goto("/runs/run_alice_succeeded?user=alice");

  await page.getByRole("button", { name: "日志" }).click();
  await expect(page).toHaveURL(/tab=logs/);
  await expect(
    page.getByLabel("Runtime Watch 实时日志").getByText("training complete", { exact: false }),
  ).toBeVisible();
  await expect(page.locator("body")).not.toContainText("store_path");

  await page.getByLabel("Run Evidence 视图").getByRole("button", { name: "结果", exact: true }).click();
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

test("switching user updates URL and resets explicit WorkArea context", async ({ page }) => {
  await page.goto("/projects?user=alice");
  const aliceSwitcher = page.getByRole("combobox", { name: "当前研究区" });
  await aliceSwitcher.selectOption("workarea_visual_alice");
  await expect(page.getByRole("button", { name: "查看 run_alice_succeeded" })).toBeVisible();

  await page.getByLabel("当前用户").selectOption("bob");
  await expect(page).toHaveURL(/user=bob/);
  const bobSwitcher = page.getByRole("combobox", { name: "当前研究区" });
  await expect(bobSwitcher).toHaveValue("");
  await expect(page.getByText("run_alice_succeeded", { exact: true })).toHaveCount(0);
  await bobSwitcher.selectOption("workarea_visual_bob");
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

  await expect(page.getByRole("heading", { name: "运行配置", level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 Contract" })).toBeDisabled();
  await page.getByRole("textbox", { name: "工作目录", exact: true }).fill("/public/home/alice/studio-case");
  await page.getByRole("textbox", { name: /^runtime\.environment\.DATA_ROOT/ }).fill("/public/home/alice/dataset.tar.gz");
  await page.getByRole("button", { name: "服务端校验" }).click();
  await expect(page.getByText("服务器 OK", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 Contract" })).toBeEnabled();
  await page.getByRole("button", { name: "创建 Contract" }).click();
  await expect(page).toHaveURL(/\/studio\/contract_visual_001\?.*panel=script/);
});

test("studio workdir picker browses backend directories without leaving the contract", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览工作目录" }).click();
  await expect(page.getByRole("dialog", { name: "选择实验工作目录" })).toBeVisible();
  await page.getByRole("button", { name: /project-a/ }).click();
  await page.getByRole("button", { name: "选择此目录" }).click();
  await expect(page.getByRole("textbox", { name: "工作目录", exact: true })).toHaveValue("/public/home/alice/project-a");
  await expect(page.getByRole("region", { name: "实验资产" })).toContainText("/public/home/alice/project-a");
  await expect(page).toHaveURL(/\/studio\/new\?user=alice/);
});

test("recipe shared_path browses existing backend files and writes canonical field", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览 runtime.environment.DATA_ROOT" }).click();
  await expect(page.getByRole("dialog", { name: /选择共享路径/ })).toBeVisible();
  await page.getByRole("button", { name: /dataset.tar.gz/ }).click();
  await page.getByRole("button", { name: "选择此文件" }).click();
  await expect(page.getByRole("textbox", { name: /^runtime\.environment\.DATA_ROOT/ })).toHaveValue("/public/home/alice/dataset.tar.gz");
  await expect(page.getByRole("region", { name: "实验资产" })).toContainText("/public/home/alice/dataset.tar.gz");
  await expect(page).toHaveURL(/\/studio\/new\?user=alice/);
});

test("studio picker keeps a bounded DOM while paging a large directory", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览工作目录" }).click();
  const dialog = page.getByRole("dialog", { name: "选择实验工作目录" });
  await dialog.getByRole("button", { name: /picker-large/ }).click();
  await expect(dialog.getByText(/已加载 500 项；目录仍有更多内容/)).toBeVisible();
  expect(await dialog.locator(".file-picker-entry").count()).toBeLessThan(80);

  await dialog.getByRole("button", { name: "加载更多目录内容" }).click();
  await expect(dialog.getByText(/已加载 1000 项；目录仍有更多内容/)).toBeVisible();
  expect(await dialog.locator(".file-picker-entry").count()).toBeLessThan(80);
});

test("dirty source is not silently overwritten by a basic form update", async ({ page }) => {
  await page.goto("/studio/new?user=alice&tab=source");
  const editor = page.locator(".cm-content");
  await editor.fill("schema_version: pilot107.contract/v2\nrecipe_version_id: changed-in-source\n");
  await page.getByRole("textbox", { name: "工作目录", exact: true }).fill("/public/home/alice/form-change");

  await expect(page.getByRole("alert").filter({ hasText: "表单与未应用源码发生冲突" })).toBeVisible();
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


test("experiment shell opens a Run as one workspace without eager deep reads", async ({ page }) => {
  let listRequests = 0;
  let workspaceRequests = 0;
  let evidenceRequests = 0;
  let diagnosisRequests = 0;
  let capsuleRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/runs") listRequests += 1;
    if (url.pathname === "/api/v1/runs/run_alice_failed/workspace") workspaceRequests += 1;
    if (url.pathname === "/api/v1/runs/run_alice_failed/evidence") evidenceRequests += 1;
    if (url.pathname === "/api/v1/runs/run_alice_failed/diagnoses") diagnosisRequests += 1;
    if (url.pathname === "/api/v1/runs/run_alice_failed/capsule") capsuleRequests += 1;
  });

  await page.goto("/runs/run_alice_failed?user=alice");
  await expect(page.getByRole("heading", { name: "运行详情", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行视图", level: 2 })).toBeVisible();
  await expect(page.getByText("contract_alice_002", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("/work/alice/projects/a-very-long-directory-name/failed-case", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: /已加载 .* 个结果/ })).toHaveCount(0);
  await expect(page.getByTestId("run-workspace-overview")).toBeVisible();
  await expect.poll(() => listRequests).toBe(0);
  await expect.poll(() => workspaceRequests).toBeGreaterThan(0);
  await expect.poll(() => evidenceRequests).toBe(0);
  await expect.poll(() => diagnosisRequests).toBe(0);
  await expect.poll(() => capsuleRequests).toBe(0);

  await page.getByRole("button", { name: "阶段：修复" }).click();
  await expect(page).toHaveURL(/tab=diagnosis/);
  await expect(page.getByRole("button", { name: "诊断", exact: true })).toHaveAttribute("aria-current", "page");
});

test("experiment shell keeps Studio preparation and preflight in the same context", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await expect(page.getByRole("heading", { name: "运行配置", level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "阶段：准备" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "阶段：配置" })).toHaveAttribute("aria-current", "step");
  await expect(page.getByRole("button", { name: "阶段：运行前检查" })).toBeDisabled();
  await page.getByRole("button", { name: "阶段：准备" }).click();
  await expect(page.getByRole("heading", { name: "实验资产" })).toBeVisible();
  await expect(page).toHaveURL(/\/studio\/new\?user=alice/);
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
    if (url.pathname === "/api/v1/workareas" && request.method() === "GET") {
      const user = request.headers()["x-pilot107-user"] || "alice";
      return json(route, { items: [workareaSummary(user)] });
    }
    if (url.pathname === "/api/v1/workareas/workarea_visual_alice" && request.method() === "GET") {
      return json(route, workareaDetail("alice"));
    }
    if (url.pathname === "/api/v1/workareas/workarea_visual_bob" && request.method() === "GET") {
      return json(route, workareaDetail("bob"));
    }
    if (
      (url.pathname === "/api/v1/workareas/workarea_visual_alice/launches"
        || url.pathname === "/api/v1/workareas/workarea_visual_bob/launches")
      && request.method() === "GET"
    ) {
      return json(route, { items: [] });
    }
    if (url.pathname === "/api/v1/files/usage") {
      return json(route, {
        home: "/public/home/alice",
        used_bytes: 1073741824,
        total_bytes: 2147483648,
        observed_at: "2026-09-03T04:00:00Z",
      });
    }
    if (url.pathname === "/api/v1/files/uploads") {
      return json(route, {
        items: [
          {
            upload_id: "upload_visual_001",
            owner: "alice",
            target_path: "/public/home/alice",
            filename: "dataset.tar.gz",
            total_size: 100,
            is_partial: false,
            received_bytes: 100,
            sha256_expected: null,
            sha256_actual: "a".repeat(64),
            state: "verified",
            auto_extract: false,
            created_at: "2026-09-03T04:00:00Z",
            written_path: null,
            extracted_members: null,
            error: null,
          },
        ],
      });
    }
    if (url.pathname === "/api/v1/files") {
      const currentPath = url.searchParams.get("path") || "/public/home/alice";
      const limit = Number(url.searchParams.get("limit") || 500);
      const offset = Number(url.searchParams.get("cursor") || 0);
      const allEntries = currentPath === "/public/home/alice"
        ? [
            { name: "project-a", type: "directory", size: 0, mtime: 1788408000 },
            { name: "picker-large", type: "directory", size: 0, mtime: 1788408000 },
            { name: "dataset.tar.gz", type: "file", size: 100, mtime: 1788408000 },
          ]
        : currentPath === "/public/home/alice/picker-large"
          ? Array.from({ length: 1200 }, (_, index) => ({
              name: `dir-${String(index).padStart(4, "0")}`,
              type: "directory",
              size: 0,
              mtime: 1788408000 + index,
            }))
          : [];
      const entries = allEntries.slice(offset, offset + limit);
      const nextOffset = offset + entries.length;
      const hasMore = nextOffset < allEntries.length;
      return json(route, {
        path: currentPath,
        entries,
        page: { limit, has_more: hasMore, next_cursor: hasMore ? String(nextOffset) : null },
        directory_revision: "visual-fixture-v2",
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
    if (url.pathname === "/api/v1/platform/connections") {
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
    if (url.pathname === "/api/v1/platform/observation") {
      return json(route, {
        source: "worker",
        observed_at: options.stalePlatform ? "2020-01-01T00:00:00Z" : "2026-07-16T02:08:00Z",
        status: options.stalePlatform ? "stale" : "fresh",
        reason: options.stalePlatform ? "observation is stale" : null,
        scheduler: {
          default_account: request.headers()["x-pilot107-user"] === "bob" ? "acct_bob" : "acct_alice",
          default_qos: "normal",
          partitions: ["Students", "GPU"],
        },
      });
    }
    if (url.pathname === "/api/v1/platform/nodes") {
      return json(route, {
        nodes: [
          { name: "node-a", partition: "Students", state: "idle", cpus_total: 64, cpus_alloc: 16, memory_mb: 256000, gres: "gpu:a100:4" },
          { name: "node-b", partition: "GPU", state: "mixed", cpus_total: 64, cpus_alloc: 32, memory_mb: 256000, gres: "gpu:a100:8" },
        ],
        observed_at: options.stalePlatform ? "2020-01-01T00:00:00Z" : "2026-07-16T02:08:00Z",
      });
    }
    if (url.pathname === "/api/v1/runs") {
      const state = url.searchParams.get("state");
      const q = (url.searchParams.get("q") || "").toLowerCase();
      const items = runListFor(request.headers()["x-pilot107-user"] || "alice")
        .filter((item) => !state || item.state === state)
        .filter((item) => !q || JSON.stringify(item).toLowerCase().includes(q));
      return json(route, { items, page: { limit: 20, has_more: false, next_cursor: null } });
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/workspace") {
      return json(route, runWorkspacePayload(runDetailPayload({ omitWorkdir: options.omitWorkdir })));
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/workspace") {
      return json(route, runWorkspacePayload(runSucceededPayload()));
    }
    if (url.pathname === "/api/v1/runs/run_bob_running/workspace") {
      return json(route, runWorkspacePayload(runBobPayload()));
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed") {
      return json(route, runDetailPayload({ omitWorkdir: options.omitWorkdir }));
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded") {
      return json(route, runSucceededPayload());
    }
    if (url.pathname === "/api/v1/runs/run_bob_running") {
      return json(route, runBobPayload());
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/evidence") {
      return json(route, runEvidencePayload("run_alice_failed"));
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/evidence") {
      return json(route, runEvidencePayload("run_alice_succeeded"));
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/diagnoses") {
      return json(route, {
        run_id: "run_alice_failed",
        diagnosis_state: "succeeded",
        items: [
          {
            diagnosis_id: "diag_visual_missing_numpy",
            run_id: "run_alice_failed",
            rule_id: "RUNTIME.PYTHON_PACKAGE_MISSING",
            severity: "error",
            summary: "Python dependency is missing.",
            evidence_refs: ["evidence://runs/run_alice_failed/logs/stderr.tail.json"],
            suggested_patch: {},
            retryable: true,
            confidence: "high",
            category: "optional_dependency",
            stage: "runtime",
            fix_guide: { fix: "install numpy" },
            created_at: "2026-07-16T02:05:00Z",
          },
        ],
      });
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/runtime-watch") {
      return json(route, runtimeWatchPayload("run_alice_succeeded"));
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/runtime-watch/logs") {
      const stream = url.searchParams.get("stream") || "stdout";
      return json(route, {
        run_id: "run_alice_succeeded",
        stream,
        content: stream === "stdout" ? "training complete\n" : "",
        bytes: stream === "stdout" ? 18 : 0,
        next_cursor: stream === "stdout" ? "18" : "0",
      });
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/runtime-watch/alerts") {
      return json(route, { items: [] });
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/runtime-watch") {
      return json(route, runtimeWatchPayload("run_alice_failed"));
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/runtime-watch/logs") {
      const stream = url.searchParams.get("stream") || "stdout";
      return json(route, {
        run_id: "run_alice_failed",
        stream,
        content: stream === "stderr" ? "ModuleNotFoundError: numpy\n" : "starting\n",
        bytes: stream === "stderr" ? 27 : 9,
        next_cursor: stream === "stderr" ? "27" : "9",
      });
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/runtime-watch/alerts") {
      return json(route, { items: [] });
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/evidence/objects/ev_result") {
      return json(route, evidencePreviewPayload("run_alice_succeeded", "ev_result"));
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/evidence/objects/ev_stdout") {
      return json(route, evidencePreviewPayload("run_alice_succeeded", "ev_stdout"));
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/evidence/objects/ev_stderr") {
      return json(route, evidencePreviewPayload("run_alice_failed", "ev_stderr"));
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/capsule") {
      return json(route, {
        ...runSucceededPayload(),
        collection_state: "succeeded",
        diagnosis_state: "idle",
        capsule_state: "ready",
        result_status: "success",
        capsule: {
          run_id: "run_alice_succeeded",
          capsule_id: "capsule_visual_001",
          manifest_sha256: "f".repeat(64),
          files_copied: 3,
          valid: true,
          checked_files: 3,
          manifest: { run_id: "run_alice_succeeded" },
          warnings: [],
          errors: [],
        },
      });
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/diagnosis") {
      return json(route, {
        run_id: "run_alice_failed",
        summary: "Python dependency is missing.",
        findings: [
          {
            code: "RUNTIME.PYTHON_PACKAGE_MISSING",
            message: "ModuleNotFoundError: numpy",
            evidence_refs: ["evidence://runs/run_alice_failed/logs/stderr.tail.json"],
          },
        ],
      });
    }
    if (url.pathname === "/api/v1/runs/run_alice_failed/logs") {
      return json(route, {
        stdout: "starting\n",
        stderr: "ModuleNotFoundError: numpy\n",
      });
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/logs") {
      return json(route, {
        stdout: "training complete\n",
        stderr: "",
      });
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/evidence") {
      return json(route, {
        run_id: "run_alice_succeeded",
        items: [
          {
            evidence_id: "ev_result",
            kind: "result",
            logical_path: "outputs/result.txt",
            size_bytes: 13,
            sha256: "a".repeat(64),
          },
        ],
      });
    }
    if (url.pathname === "/api/v1/evidence/ev_result") {
      return json(route, {
        evidence_id: "ev_result",
        logical_path: "outputs/result.txt",
        text: "accuracy=0.91",
        sha256: "a".repeat(64),
      });
    }
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/capsule") {
      return json(route, {
        run_id: "run_alice_succeeded",
        capsule_id: "capsule_visual_001",
        checksum_verified: true,
        checksum: "f".repeat(64),
      });
    }
    if (url.pathname === "/api/v1/agent-sessions") {
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

    return route.continue();
  });
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function workareaSummary(user) {
  return {
    workarea_id: `workarea_visual_${user}`,
    owner: user,
    title: user === "bob" ? "Bob research context" : "Alice research context",
    description: "Explicit visual-test WorkArea",
    created_at: "2026-07-16T00:00:00Z",
    updated_at: "2026-07-16T03:02:00Z",
  };
}

function workareaDetail(user) {
  const summary = workareaSummary(user);
  const runIds = user === "bob"
    ? ["run_bob_running"]
    : ["run_alice_failed", "run_alice_succeeded"];
  return {
    ...summary,
    bindings: {
      contracts: [],
      runs: runIds.map((target_ref) => ({
        kind: "run",
        target_ref,
        source: "inherited",
      })),
      assets: [],
    },
  };
}

function runListFor(user) {
  if (user === "bob") return [runBobPayload()];
  return [runDetailPayload(), runSucceededPayload()];
}

function evidenceObject(runId, objectId, category, logicalPath, sourceUri = null) {
  return {
    object_id: objectId,
    category,
    logical_path: logicalPath,
    source_uri: sourceUri,
    sha256: "a".repeat(64),
    size_bytes: 18,
    mime_type: "text/plain",
    collection_status: "collected",
    mutable_during_run: false,
    finalized_at: "2026-07-16T02:05:00Z",
  };
}

function runEvidencePayload(runId) {
  const succeeded = runId === "run_alice_succeeded";
  const objects = succeeded
    ? [
        evidenceObject(runId, "ev_stdout", "logs", "logs/stdout.tail.txt", "evidence://runs/run_alice_succeeded/logs/stdout.tail.txt"),
        evidenceObject(runId, "ev_result", "outputs", "outputs/result.txt", "evidence://runs/run_alice_succeeded/outputs/result.txt"),
      ]
    : [
        evidenceObject(runId, "ev_stderr", "logs", "logs/stderr.tail.json", "evidence://runs/run_alice_failed/logs/stderr.tail.json"),
      ];
  return {
    run_id: runId,
    owner: "alice",
    job_id: succeeded ? "12344" : "12345",
    run_state: succeeded ? "SUCCEEDED" : "FAILED",
    collection_state: "succeeded",
    tasks: [],
    objects,
    tree: { name: "evidence", kind: "directory", logical_path: "", children: [] },
  };
}

function evidencePreviewPayload(runId, objectId) {
  const result = objectId === "ev_result";
  const stderr = objectId === "ev_stderr";
  const base = result
    ? evidenceObject(runId, objectId, "outputs", "outputs/result.txt", "evidence://runs/run_alice_succeeded/outputs/result.txt")
    : stderr
      ? evidenceObject(runId, objectId, "logs", "logs/stderr.tail.json", "evidence://runs/run_alice_failed/logs/stderr.tail.json")
      : evidenceObject(runId, objectId, "logs", "logs/stdout.tail.txt", "evidence://runs/run_alice_succeeded/logs/stdout.tail.txt");
  return {
    ...base,
    preview: {
      available: true,
      content: result ? "accuracy=0.91" : stderr ? "ModuleNotFoundError: numpy\n" : "training complete\n",
      encoding: "utf-8",
      bytes_read: 18,
      max_bytes: 262144,
      truncated: false,
      integrity: "verified",
    },
  };
}

function runtimeWatchPayload(runId) {
  return {
    watch_id: `watch_${runId}`,
    run_id: runId,
    state: "stopped",
    next_poll_at: null,
    updated_at: "2026-07-16T02:05:00Z",
    streams: {
      stdout: { generation: 0, offset: 18, last_data_at: "2026-07-16T02:04:00Z", last_checked_at: "2026-07-16T02:05:00Z", quiet_polls: 0 },
      stderr: { generation: 0, offset: runId === "run_alice_failed" ? 27 : 0, last_data_at: null, last_checked_at: "2026-07-16T02:05:00Z", quiet_polls: 0 },
    },
    alert_count: 0,
  };
}

function runWorkspacePayload(run) {
  const failed = run.state === "FAILED";
  const succeeded = run.state === "SUCCEEDED";
  const running = run.state === "RUNNING";
  return {
    run: {
      run_id: run.run_id,
      owner: run.run_id.startsWith("run_bob") ? "bob" : "alice",
      job_name: run.run_id,
      created_at: run.created_at,
      updated_at: run.updated_at,
      exit_code: failed ? "1:0" : succeeded ? "0:0" : null,
      terminal_state: failed ? "FAILED" : succeeded ? "COMPLETED" : null,
      attempt: 0,
    },
    states: {
      execution: run.state,
      collection: running ? "pending" : "succeeded",
      diagnosis: failed ? "succeeded" : "idle",
      capsule: succeeded ? "ready" : "none",
      result: succeeded ? "success" : failed ? "failed" : "unknown",
    },
    outcome: {
      kind: failed ? "failed" : succeeded ? "succeeded" : running ? "running" : "queued",
      summary: failed
        ? "作业以非成功状态结束，退出码 1:0。"
        : succeeded
          ? "计算已完成；科学结果仍需根据输出与证据评价。"
          : "作业正在 Slurm 中执行或完成收尾。",
    },
    attention: failed
      ? {
          severity: "critical",
          title: "Python 运行环境缺少作业需要的包。",
          detail: "依据已有诊断 RUNTIME.PYTHON_PACKAGE_MISSING；可继续查看其 Evidence 引用。",
        }
      : { severity: running ? "info" : "none", title: null, detail: null },
    next_action: failed
      ? {
          kind: "prepare_repair",
          label: "查看诊断并准备修复",
          detail: "已有持久化诊断；先核对 Evidence，再决定是否进入受控修复。",
        }
      : succeeded
        ? {
            kind: "view_results",
            label: "查看结果与证据",
            detail: "计算与证据整理已完成；继续评价实际输出，不自动推断科学结论。",
          }
        : {
            kind: "watch_run",
            label: "查看实时状态",
            detail: "查看当前 Slurm 状态和增量日志。",
          },
    evidence_summary: {
      object_count: failed ? 2 : succeeded ? 3 : 0,
      result_count: succeeded ? 1 : 0,
      diagnosis_count: failed ? 1 : 0,
      stdout_available: succeeded,
      stderr_available: failed,
      capsule_available: succeeded,
    },
    provenance: {
      contract_id: run.contract_id ?? null,
      contract_digest: "d".repeat(64),
      workdir: run.workdir ?? null,
      job_id: run.scheduler_job_id ?? null,
      parent_run_id: null,
      lineage_reason: null,
      remediation_plan_id: null,
      workspace_revision: null,
      workspace_digest: null,
      source_revision: null,
      platform_snapshot_ref: null,
    },
  };
}

function runDetailPayload(options = {}) {
  return {
    run_id: "run_alice_failed",
    contract_id: "contract_alice_002",
    state: "FAILED",
    scheduler_job_id: "12345",
    workdir: options.omitWorkdir ? undefined : "/work/alice/projects/a-very-long-directory-name/failed-case",
    created_at: "2026-07-16T02:00:00Z",
    updated_at: "2026-07-16T02:05:00Z",
  };
}

function runSucceededPayload() {
  return {
    run_id: "run_alice_succeeded",
    contract_id: "contract_alice_001",
    state: "SUCCEEDED",
    scheduler_job_id: "12344",
    workdir: "/work/alice/projects/success-case",
    created_at: "2026-07-16T01:00:00Z",
    updated_at: "2026-07-16T01:05:00Z",
  };
}

function runBobPayload() {
  return {
    run_id: "run_bob_running",
    contract_id: "contract_bob_001",
    state: "RUNNING",
    scheduler_job_id: "22344",
    workdir: "/work/bob/projects/current",
    created_at: "2026-07-16T03:00:00Z",
    updated_at: "2026-07-16T03:02:00Z",
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
    resources: { partition: "Students", nodes: 1, cpus: 1, gpus: 0, memory: "1G", time: "00:10:00" },
    runtime: { environment: {} },
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
    digest: "d".repeat(64),
    contract,
    created_at: "2026-07-16T02:08:00Z",
  };
}

function preparedRunPayload(overrides = {}) {
  return {
    run_id: "run_studio_prepared",
    contract_id: "contract_visual_001",
    state: "PREPARED",
    preview: { submitted_script: "#!/bin/bash\npython3 main.py\n" },
    ...overrides,
  };
}

function unifiedMarketItemPayload() {
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

function templateMarketPayload() {
  return {
    template_id: "template_visual",
    title: "Verified Python CPU",
    summary: "Verified on simulator",
    latest_release: {
      release_id: "release_110",
      version: "1.1.0",
      trust_level: "verified",
    },
  };
}

function marketApplicationPayload(overrides = {}) {
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
