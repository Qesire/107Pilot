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

// These exact legacy visual titles are replaced one-for-one by scenarios in
// current-contracts.spec.js.  The replacement tests use the current API read
// models instead of preserving obsolete mock payloads.  All other legacy visual
// scenarios and files.spec.js remain blocking.
const supersededLegacyVisualTitles = /^(?:workspace prioritizes current work and preparation facts|run filters are URL-controlled and narrow the server query|switching user updates URL and invalidates scoped queries|stale and degraded dynamic facts remain explicit|mobile layout exposes primary destinations without horizontal overflow|studio requires server validation before creating a canonical contract|dirty source is not silently overwritten by a basic form update|market release adoption opens the server-created canonical contract|Agent separates durable read-only conversation from controlled repair)$/;

module.exports = defineConfig({
  testDir: "./tests/ui",
  outputDir: "./artifacts/playwright-output",
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  grepInvert: supersededLegacyVisualTitles,
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
