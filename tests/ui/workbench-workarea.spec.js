const fs = require("node:fs");
const path = require("node:path");
const { expect, test } = require("@playwright/test");

const CURRENT_KEY_PREFIX = "107pilot-current-workarea:";
const screenshotDir = path.resolve(__dirname, "../../artifacts/visual-regression");

function workAreaFor(user) {
  const isBob = user === "bob";
  const workareaId = `workarea-workbench-${user}`;
  return {
    workarea_id: workareaId,
    owner: user,
    title: isBob ? "Bob research context" : "Competition workbench",
    description: isBob ? "Independent Bob WorkArea" : "Explicit WorkArea continuity",
    created_at: "2026-09-06T12:00:00Z",
    updated_at: "2026-09-06T12:00:00Z",
    bindings: {
      contracts: [{ kind: "contract", target_ref: `contract-${user}`, source: "user" }],
      runs: isBob
        ? [{ kind: "run", target_ref: "run_bob_running", source: "inherited" }]
        : [{ kind: "run", target_ref: "run_alice_bound", source: "inherited" }],
      assets: [{
        kind: "asset",
        target_ref: `/public/home/${user}/project`,
        role: "code",
        source: "user",
        linked_at: "2026-09-06T12:00:00Z",
      }],
    },
  };
}

function runFor(id, state, user) {
  return {
    run_id: id,
    job_name: id,
    contract_id: `contract-${user}`,
    state,
    collection_state: state === "RUNNING" || state === "SUBMITTED" ? "pending" : "succeeded",
    diagnosis_state: state === "FAILED" ? "succeeded" : "idle",
    capsule_state: "none",
    result_status: state === "FAILED" ? "failed" : "unknown",
    scheduler_job_id: user === "bob" ? "22001" : "12001",
    workdir: `/public/home/${user}/project`,
    created_at: "2026-09-06T12:01:00Z",
    updated_at: "2026-09-06T12:02:00Z",
  };
}

function runListFor(user) {
  if (user === "bob") return [runFor("run_bob_running", "RUNNING", "bob")];
  return [
    // Deliberately newer and FAILED. It must remain invisible because the
    // current WorkArea graph does not bind it.
    runFor("run_global_unbound_failure", "FAILED", "alice"),
    runFor("run_alice_bound", "SUBMITTED", "alice"),
  ];
}

function launchFor(user) {
  const area = workAreaFor(user);
  return {
    launch_id: `launch-workbench-${user}`,
    candidate_id: `launchcand-workbench-${user}`,
    preflight_id: `preflight-workbench-${user}`,
    workarea_id: area.workarea_id,
    owner: user,
    contract_id: `contract-${user}`,
    candidate_digest: "a".repeat(64),
    preflight_digest: "b".repeat(64),
    committed_at: "2026-09-06T12:01:00Z",
    submitted_at: "2026-09-06T12:01:01Z",
    submit_error: null,
    run_ids: user === "bob" ? ["run_bob_running"] : ["run_alice_bound"],
  };
}

async function installWorkbenchMock(page) {
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const requestedUser = request.headers()["x-pilot107-user"] || "alice";
    const user = requestedUser === "bob" ? "bob" : "alice";
    const area = workAreaFor(user);
    const json = (body, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

    if (url.pathname === "/healthz") return json({ status: "ok" });
    if (url.pathname === "/api/v1/health/ready") {
      return json({ status: "ready", checks: { database: { status: "ok" } } });
    }
    if (url.pathname === "/api/v1/web/session") {
      return json({ identity_mode: "demo", user, switchable: true });
    }
    if (url.pathname === "/api/v1/workareas" && request.method() === "GET") {
      return json({ items: [area] });
    }
    if (url.pathname === `/api/v1/workareas/${area.workarea_id}` && request.method() === "GET") {
      return json(area);
    }
    if (url.pathname === `/api/v1/workareas/${area.workarea_id}/launches`) {
      return json({ items: [launchFor(user)] });
    }
    if (url.pathname === "/api/v1/runs") {
      return json({
        items: runListFor(user),
        page: { limit: 100, has_more: false, next_cursor: null },
      });
    }
    if (url.pathname === "/api/v1/platform/capabilities") {
      return json({
        profile_id: `capability-${user}`,
        source_authority: "workbench-fixture",
        captured_at: "2026-09-06T12:00:00Z",
        freshness_seconds: 60,
        default_partition: "Students",
        default_qos: "normal",
        partitions: [{ name: "Students", total_nodes: 2, state: ["UP"], allow_qos: ["normal"], gpu_types: ["A100"] }],
        qos: [{ name: "normal", max_cpus: 64, max_gpus: 4, source_authority: "workbench-fixture" }],
        dynamic_facts: [],
        limitations: [],
        snapshot_ref: { snapshot_id: `platform-${user}`, freshness: "fresh", observed_at: "2026-09-06T12:00:00Z" },
      });
    }
    if (url.pathname === "/api/v1/platform/connections") return json({ items: [] });
    if (url.pathname === "/api/v1/platform/snapshots/latest") {
      return json({
        snapshot_id: `platform-${user}`,
        scope: "login_node",
        source_type: "worker",
        source_name: "workbench-fixture",
        captured_at: "2026-09-06T12:00:00Z",
        observed_at: "2026-09-06T12:00:00Z",
        freshness: "fresh",
        data_quality: "complete",
        collection_status: "succeeded",
        counts: { commands: 1, partitions: 1, nodes: 1, jobs: 0, limitations: 0 },
        snapshot: { snapshot_id: `platform-${user}`, scope: "login_node", captured_at: "2026-09-06T12:00:00Z", nodes: [], squeue_jobs: [] },
        limitations: [],
      });
    }
    if (url.pathname === "/api/v1/platform/entitlements/latest") {
      return json({
        snapshot_id: `entitlement-${user}`,
        captured_at: "2026-09-06T12:00:00Z",
        observed_at: "2026-09-06T12:00:00Z",
        freshness: "fresh",
        data_quality: "complete",
        default_account: `acct_${user}`,
        associations: [{ account: `acct_${user}`, partition: "Students", qos: ["normal"], default_qos: "normal" }],
      });
    }
    if (url.pathname === "/api/v1/files/usage") {
      return json({
        home: `/public/home/${user}`,
        used_bytes: 1024,
        total_bytes: 4096,
        observed_at: "2026-09-06T12:00:00Z",
      });
    }
    if (url.pathname.startsWith("/api/")) {
      return json({ error: { code: "TEST.UNHANDLED", message: url.pathname } }, 404);
    }
    return route.continue();
  });
}

async function selectCurrentArea(page, user) {
  const area = workAreaFor(user);
  const switcher = page.getByRole("combobox", { name: "当前研究区" });
  await switcher.selectOption(area.workarea_id);
  await expect(switcher).toHaveValue(area.workarea_id);
  return area;
}

test.beforeEach(async ({ page }) => {
  fs.mkdirSync(screenshotDir, { recursive: true });
  await installWorkbenchMock(page);
});

test("workbench requires explicit WorkArea selection, scopes facts, and persists across reload", async ({ page }) => {
  await page.goto("/projects?user=alice");

  const switcher = page.getByRole("combobox", { name: "当前研究区" });
  const newLaunch = page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" });

  // Even with exactly one visible WorkArea, 107Pilot must not infer selection.
  await expect(switcher).toHaveValue("");
  await expect(newLaunch).toBeDisabled();
  await expect(page.getByText("先明确选择研究区")).toBeVisible();
  await expect(page.getByText("run_global_unbound_failure", { exact: true })).toHaveCount(0);

  const area = await selectCurrentArea(page, "alice");
  const launchSection = page.locator('section[aria-labelledby="recent-launches-heading"]');
  await expect(newLaunch).toBeEnabled();
  await expect(page.locator(".workbench-current-run > strong")).toHaveText(area.title);
  await expect(page.getByRole("heading", { name: "最近 Launch" })).toBeVisible();
  await expect(launchSection.getByText("run_alice_bound", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前研究区运行" })).toBeVisible();
  await expect(page.getByText("run_alice_bound", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("run_global_unbound_failure", { exact: true })).toHaveCount(0);
  await expect(page.getByText("没有待处理的失败运行")).toBeVisible();
  await expect(page.getByText("acct_alice", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("可见分区", { exact: true })).toBeVisible();
  expect(await page.evaluate((key) => localStorage.getItem(key), `${CURRENT_KEY_PREFIX}alice`)).toBe(area.workarea_id);

  await page.screenshot({ path: path.join(screenshotDir, "workbench-workarea.png"), fullPage: true });

  await page.reload();
  await expect(page.getByRole("combobox", { name: "当前研究区" })).toHaveValue(area.workarea_id);
  await expect(page.locator(".workbench-current-run > strong")).toHaveText(area.title);
  await expect(page.locator('section[aria-labelledby="recent-launches-heading"]').getByText("run_alice_bound", { exact: true })).toBeVisible();

  await page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" }).click();
  await expect(page).toHaveURL(new RegExp(`/workareas/${area.workarea_id}/launch/new`));
});

test("switching user requires that user's explicit WorkArea and keeps scoped queries isolated", async ({ page }) => {
  await page.goto("/projects?user=alice");
  await selectCurrentArea(page, "alice");
  await expect(page.getByText("run_alice_bound", { exact: true }).first()).toBeVisible();

  await page.getByLabel("当前用户").selectOption("bob");
  await expect(page).toHaveURL(/user=bob/);
  await expect(page.getByRole("combobox", { name: "当前研究区" })).toHaveValue("");
  await expect(page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" })).toBeDisabled();
  await expect(page.getByText("run_alice_bound", { exact: true })).toHaveCount(0);

  const bobArea = await selectCurrentArea(page, "bob");
  await expect(page.locator(".workbench-current-run > strong")).toHaveText(bobArea.title);
  await expect(page.getByText("run_bob_running", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("acct_bob", { exact: true }).first()).toBeVisible();
  expect(await page.evaluate((key) => localStorage.getItem(key), `${CURRENT_KEY_PREFIX}bob`)).toBe(bobArea.workarea_id);
});

test("stale WorkArea preference fails closed instead of selecting another area", async ({ page }) => {
  await page.goto("/projects?user=alice");
  await page.evaluate((key) => localStorage.setItem(key, "workarea-missing"), `${CURRENT_KEY_PREFIX}alice`);
  await page.reload();

  const switcher = page.getByRole("combobox", { name: "当前研究区" });
  await expect(page.getByRole("alert")).toContainText("系统没有自动切换到其它研究区");
  await expect(switcher).toHaveValue("");
  await expect(switcher.locator(`option[value="${workAreaFor("alice").workarea_id}"]`)).toHaveCount(1);
  await expect(
    page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" }),
  ).toBeDisabled();
  await expect(page.getByText("run_alice_bound", { exact: true })).toHaveCount(0);
});
