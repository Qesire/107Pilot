const fs = require("node:fs/promises");
const path = require("node:path");

const evidenceRoot = path.resolve("artifacts/qa/frontend-scientific-journeys");

function uniqueName(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
}

async function captureEvidence(page, caseId, payload) {
  await fs.mkdir(evidenceRoot, { recursive: true });
  await page.screenshot({ path: path.join(evidenceRoot, `${caseId}.png`), fullPage: true });
  await fs.writeFile(
    path.join(evidenceRoot, `${caseId}.json`),
    `${JSON.stringify({ case_id: caseId, url: page.url(), ...payload }, null, 2)}\n`,
  );
}

async function waitForRunTerminal(page) {
  const terminal = page.getByText(/已成功|已失败|已取消|收集失败/).first();
  await terminal.waitFor({ state: "visible", timeout: 180_000 });
  return (await terminal.textContent())?.trim() || "unknown";
}

module.exports = { captureEvidence, uniqueName, waitForRunTerminal };
