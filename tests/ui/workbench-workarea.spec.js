const { expect, test } = require("@playwright/test");

const WORKAREA_ID = "workarea-workbench";
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
      contracts: [],
      runs: [],
      assets: [{
        kind: "asset",
        target_ref: "/public/home/alice/project",
        role: "code",
        source: "user",
        linked_at: "2026-09-06T12:00:00Z",
      }],
    },
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
      return json({ items: [] });
    }
    if (url.pathname === "/api/v1/runs") {
      return json({ items: [] });
    }
    if (url.pathname === "/api/v1/platform/capabilities") {
      return json({ partitions: [], source_authority: "mock" });
    }
    if (url.pathname === "/api/v1/platform/connections") {
      return json({ items: [] });
    }
    if (url.pathname === "/api/v1/platform/snapshots/latest") {
      return json({ error: { code: "PLATFORM.NOT_FOUND", message: "not observed" } }, 404);
    }
    if (url.pathname === "/api/v1/platform/entitlements/latest") {
      return json({ error: { code: "ENTITLEMENT.NOT_FOUND", message: "not observed" } }, 404);
    }
    if (url.pathname.includes("storage")) {
      return json({ used_bytes: 0, total_bytes: null });
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

test("workbench requires explicit WorkArea selection and persists it across reload", async ({ page }) => {
  await page.goto("/projects?user=alice");

  const switcher = page.getByLabel("当前研究区");
  const newLaunch = page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" });

  // Even with exactly one visible WorkArea, 107Pilot must not infer selection.
  await expect(switcher).toHaveValue("");
  await expect(newLaunch).toBeDisabled();
  await expect(page.getByText("先明确选择研究区")).toBeVisible();

  await switcher.selectOption(WORKAREA_ID);
  await expect(switcher).toHaveValue(WORKAREA_ID);
  await expect(newLaunch).toBeEnabled();
  await expect(page.locator(".workbench-current-run > strong")).toHaveText("Competition workbench");
  await expect(page.getByRole("heading", { name: "最近 Launch" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前研究区运行" })).toBeVisible();
  await expect(page.evaluate((key) => localStorage.getItem(key), CURRENT_KEY)).resolves.toBe(WORKAREA_ID);

  await page.reload();
  await expect(page.getByLabel("当前研究区")).toHaveValue(WORKAREA_ID);
  await expect(page.locator(".workbench-current-run > strong")).toHaveText("Competition workbench");

  await page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" }).click();
  await expect(page).toHaveURL(new RegExp(`/workareas/${WORKAREA_ID}/launch/new`));
});

test("stale WorkArea preference fails closed instead of selecting another area", async ({ page }) => {
  await page.goto("/projects?user=alice");
  await page.evaluate((key) => localStorage.setItem(key, "workarea-missing"), CURRENT_KEY);
  await page.reload();

  await expect(page.getByRole("alert")).toContainText("系统没有自动切换到其它研究区");
  await expect(page.getByLabel("当前研究区")).toHaveValue("");
  await expect(page.getByLabel("当前研究区").locator(`option[value="${WORKAREA_ID}"]`)).toHaveCount(1);
  await expect(
    page.locator(".workbench-v2-header").getByRole("button", { name: "新建运行" }),
  ).toBeDisabled();
});
