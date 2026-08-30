const { expect, test } = require("@playwright/test");
const { captureEvidence } = require("./live-helpers");

test.describe.configure({ mode: "serial" });

test("LIVE-00 exposes the real workload home", async ({ page }) => {
  await page.goto("/projects?user=alice");
  await expect(page.getByRole("heading", {
    name: "把下一次提交建立在可验证事实之上",
  })).toBeVisible();
  await captureEvidence(page, "LIVE-00", { outcome: "PASS", user: "alice" });
});
