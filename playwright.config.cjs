const fs = require("node:fs");
const { defineConfig, devices } = require("@playwright/test");

const preferredChrome =
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ||
  "/home/knowingthesea/.agent-browser/browsers/chrome-149.0.7827.54/chrome";

const launchOptions = {
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
};

if (fs.existsSync(preferredChrome)) {
  launchOptions.executablePath = preferredChrome;
}

module.exports = defineConfig({
  testDir: "./tests/ui",
  outputDir: "./artifacts/playwright-output",
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  use: {
    baseURL: "http://127.0.0.1:3197",
    launchOptions,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
      },
    },
  ],
  webServer: {
    command:
      "PYTHONPATH=src python3 -m pilot107.web.server --host 127.0.0.1 --port 3197 --api-base-url http://127.0.0.1:8070",
    url: "http://127.0.0.1:3197/healthz",
    reuseExistingServer: true,
    timeout: 10_000,
  },
});
