const { expect, test } = require("@playwright/test");

const WORKAREA_ID = "workarea-binding-removal";
const USER_ASSET = "/public/home/alice/project/code";
const USER_RUN = "run-history-user";
const INHERITED_RUN = "run-launch-inherited";
const INHERITED_CONTRACT = "contract-launch-inherited";

async function installMock(page) {
  let bindings = {
    assets: [{
      kind: "asset",
      target_ref: USER_ASSET,
      role: "code",
      source: "user",
      linked_at: "2026-09-06T10:00:00Z",
    }],
    runs: [
      { kind: "run", target_ref: USER_RUN, source: "user" },
      { kind: "run", target_ref: INHERITED_RUN, source: "inherited" },
    ],
    contracts: [
      { kind: "contract", target_ref: INHERITED_CONTRACT, source: "inherited" },
    ],
  };

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      body: status === 204 ? "" : JSON.stringify(body),
    });

    if (url.pathname === "/healthz") return json({ status: "ok" });
    if (url.pathname === "/api/v1/health/ready") {
      return json({ status: "ready", checks: { database: { status: "ok" } } });
    }
    if (url.pathname === "/api/v1/web/session") {
      return json({ identity_mode: "demo", user: "alice", switchable: true });
    }
    if (url.pathname === `/api/v1/workareas/${WORKAREA_ID}` && request.method() === "GET") {
      return json({
        workarea_id: WORKAREA_ID,
        owner: "alice",
        title: "Binding correction",
        description: "Correct explicit context without rewriting provenance",
        created_at: "2026-09-06T10:00:00Z",
        updated_at: "2026-09-06T10:00:00Z",
        bindings,
      });
    }
    if (url.pathname === `/api/v1/workareas/${WORKAREA_ID}/launches`) {
      return json({ items: [] });
    }
    if (
      request.method() === "DELETE"
      && url.pathname.startsWith(`/api/v1/workareas/${WORKAREA_ID}/bindings/`)
    ) {
      const parts = url.pathname.split("/");
      const kind = decodeURIComponent(parts[6]);
      const target = decodeURIComponent(parts.slice(7).join("/"));
      if (kind === "run" && target === USER_RUN) {
        return json({
          error: {
            code: "WORKAREA_BINDING.IMMUTABLE",
            message: "Run binding is required by durable Launch provenance",
          },
        }, 409);
      }
      bindings = {
        ...bindings,
        [kind === "asset" ? "assets" : kind === "run" ? "runs" : "contracts"]:
          bindings[kind === "asset" ? "assets" : kind === "run" ? "runs" : "contracts"]
            .filter((item) => item.target_ref !== target),
      };
      return json({}, 204);
    }

    return route.continue();
  });
}

test.beforeEach(async ({ page }) => {
  await installMock(page);
});

test("only explicit bindings are removable and removal survives reload", async ({ page }) => {
  await page.goto(`/workareas/${WORKAREA_ID}?user=alice`);

  await expect(page.getByText(USER_ASSET)).toBeVisible();
  await expect(page.getByText(INHERITED_RUN)).toBeVisible();
  await expect(page.getByText(INHERITED_CONTRACT)).toBeVisible();
  await expect(page.getByText("由 Launch 继承 · 不可解除")).toHaveCount(2);
  await expect(page.getByRole("button", { name: `解除绑定 ${INHERITED_RUN}` })).toHaveCount(0);
  await expect(page.getByRole("button", { name: `解除绑定 ${INHERITED_CONTRACT}` })).toHaveCount(0);

  await page.getByRole("button", { name: `解除绑定 ${USER_ASSET}` }).click();
  await expect(page.getByText(USER_ASSET)).toHaveCount(0);

  await page.reload();
  await expect(page.getByText(USER_ASSET)).toHaveCount(0);
  await expect(page.getByText(INHERITED_RUN)).toBeVisible();
});

test("server provenance guard remains authoritative for a user-labelled edge", async ({ page }) => {
  await page.goto(`/workareas/${WORKAREA_ID}?user=alice`);

  await page.getByRole("button", { name: `解除绑定 ${USER_RUN}` }).click();

  await expect(page.getByRole("alert")).toContainText("Launch provenance");
  await expect(page.getByText(USER_RUN)).toBeVisible();
});
