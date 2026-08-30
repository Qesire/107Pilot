const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/ui-live",
  timeout: 180_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["json", { outputFile: "artifacts/qa/frontend-scientific-journeys/playwright.json" }]],
  use: {
    baseURL: process.env.PILOT107_LIVE_BASE_URL || "http://127.0.0.1:13000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
});
