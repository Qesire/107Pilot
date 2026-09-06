const { expect, test } = require("@playwright/test");

const WORKAREA_ID = "workarea-workbench";
const CONTRACT_ID = "contract-workbench";
const BOUND_RUN_ID = "run-workbench-bound";
const UNBOUND_RUN_ID = "run-global-unbound-failure";
const LAUNCH_ID = "launch-workbench";
const CURRENT_KEY = "107pilot-current-workarea:alice";

async function installWorkbenchMock(page) {
  const workarea = {
    workarea_id: WORKAREA_ID,
    owner: "alice",
    title: "Competition workbench",
    description: "Explicit WorkArea continuity",
    created_at: "2026-09-06T12:00:00Z",
    updated_at: "2026-09-06T12:00:00Z",
    bindings: {
      contracts: [{ kind: "contract", target_ref: CONTRACT_ID, source: "user" }],
      runs: [{ kind: "run", target_ref: BOUND_RUN_ID, source: "inherited" }],
      assets: [{
        kind: "asset",
        target_ref: "/public/home/alice/project",
        role: "code",
        source: "user",
        linked_at: "2026-09-06T12:00:00Z",
      }],
    },
  };
  const launch = {
    launch_id: LAUNCH_ID,
    candidate_id: "launchcand-workbench",
    preflight_id: "preflight-workbench",
    workarea_id: WORKAREA_ID,
    owner: "alice",
    contract_id: CONTRACT_ID,
    candidate_digest: "a".repeat(64),
    preflight_digest: "b".repeat(64),
    committed_at: "2026-09-06T12:01:00Z",
    submitted_at: "2026-09-06T12:01:01Z",
    submit_error: null,
    run_ids: [BOUND_RUN_ID],
  };
  const boundRun = {
    run_id: BOUND_RUN_ID,
    contract_id: CONTRACT_ID,
    state: "SUBMITTED",
    scheduler_job_id: "41001",
    workdir: "/public/home/alice/project",
    created_at: "2026-09-06T12:01:00Z",
    updated_at: "2026-09-06T12:01:01Z",
  };
  const unboundRun = {
    run_id: UNBOUND_RUN_ID,
    contract_id: "contract-other",
    state: "FAILED",
    scheduler_job_id: "41999",
    workdir: "/public/home/alice/other-project",
    created_at: "2026-09-06T12:02:00Z",
    updated_at: "2026-09-06T12:02:10Z",
  };

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
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
      return json({ identity_mode: "demo", user: "alice", switchable: true });
    }
    if (url.pathname === "/api/v1/workareas" && request.method() === "GET") {
      return json({ items: [workarea] });
    }
    if (url.pathname === `/api/v1/workareas/${WORKAREA_ID}` && request.method() === "GET") {
      return json(workarea);
    }
    if (url.pathname === `/api/v1/workareas/${WORKAREA_ID}/launches`) {
      return json({ items: [launch] });
    }
    if (url.pathname === "/api/v1/runs") {
      return json({
        items: [unboundRun, boundRun],
        page: { limit: 100, has_more: false, next_cursor: null },
      });
    }
    if (url.pathname === "/api/v1/platform/capabilities") {
      return json({ partitions: [], source_authority: "mock" });
    }
    if (url.pathname === "/api/v1/platform/connections") return json({ items: [] });
    if (url.pathname === "/api/v1/platform/snapshots/latest") {
      return json({ error: { code: "PLATFORM.NOT_FOUND", message: "not observed" } }, 404);
    }
    if (url.pathname === "/api/v1/platform/entitlements/latest") {
      return json({ error: { code: "ENTITLEMENT.NOT_FOUND", message: "not observed" } }, 404);
    }
    if (url.pathname === "/api/v1/files/usage") {
      return json({
        home: "/public/home/alice",
        used_bytes: 0,
        total_bytes: null,
        observed_at: "2026-09-06T12:00:00Z",
      });
    }
    if (url.pathname.startsWith("/api/")) {
      return json({ error: { code: "TEST.UNHANDLED", message: url.pathname } }, 404);
    }
    return route.continue();
  });
}

test.beforeEach(async ({ page }) => {
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
  // A global failed Run exists, but it is not allowed to become current context.
  await expect(page.getByText(UNBOUND_RUN_ID, { exact: true })).toHaveCount(0);

  await switcher.selectOption(WORKAREA_ID);
  await expect(switcher).toHaveValue(WORKAREA_ID);
  await expect(newLaunch).toBeEnabled();
  await expect(page.locator(".workbench-current-run > strong")).toHaveText("Competition workbench");
  await expect(page.getByRole("heading", { name: "最近 Launch" })).toBeVisible();
  await expect(page.getByText(LAUNCH_ID, { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前研究区运行" })).toBeVisible();
  await expect(page.getByText(BOUND_RUN_ID, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(UNBOUND_RUN_ID, { exact: true })).toHaveCount(0);
  await expect(page.getByText("没有待处理的失败运行")).toBeVisible();
  expect(await page.evaluate((key) => localStorage.getItem(key), CURRENT_KEY)).toBe(WORKAREA_ID);

  await page.reload();
  await expect(page.getByRole("combobox", { name: "当前研究区" })).toHaveValue(WORKAREA_ID);
  await expect(page.locator(".workbench-current-run > strong")).toHaveText("Competition workbench");
  await expect(page.getByText(LAUNCH_ID, { exact: true })).toBeVisible();

  await page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" }).click();
  await expect(page).toHaveURL(new RegExp(`/workareas/${WORKAREA_ID}/launch/new`));
});

test("stale WorkArea preference fails closed instead of selecting another area", async ({ page }) => {
  await page.goto("/projects?user=alice");
  await page.evaluate((key) => localStorage.setItem(key, "workarea-missing"), CURRENT_KEY);
  await page.reload();

  const switcher = page.getByRole("combobox", { name: "当前研究区" });
  await expect(page.getByRole("alert")).toContainText("系统没有自动切换到其它研究区");
  await expect(switcher).toHaveValue("");
  await expect(switcher.locator(`option[value="${WORKAREA_ID}"]`)).toHaveCount(1);
  await expect(
    page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" }),
  ).toBeDisabled();
});
