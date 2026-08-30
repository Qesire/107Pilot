# Frontend Scientific Journey Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute a repeatable, live-frontend acceptance suite that proves or disproves the undergraduate, graduate-array, direct-Run-sharing, and formal-template-release journeys against authoritative vm-slurm.

**Architecture:** Use the local web application at `http://127.0.0.1:13000` as the only mutation surface. First perform an exploratory browser pass and record evidence, then encode stable release-gate assertions in a separate Playwright live suite that never installs mocked routes; failures become the evidence-driven input to four subsequent remediation plans.

**Tech Stack:** React 18 frontend, Playwright 1.55, Vitest 3, Python HTTP API, vm-slurm, pilot-browser/agent-browser CLI.

## Global Constraints

- Treat vm-slurm and the VM compute node as the real scheduler and compute resource for this deployment.
- Perform acceptance mutations through public frontend interactions only; direct API calls may diagnose a failure read-only but cannot turn a step into PASS.
- Do not expose raw Contract, compatibility, publication, environment, or extensions JSON/YAML in an ordinary scientist journey.
- The exact submitted sbatch script is the primary user-visible authoring artifact and must survive save, reload, adoption, rerun, and submission.
- Cluster facts, personal entitlement, editor defaults, preflight, approval summary, and submitted script must agree.
- Direct Run sharing must not claim reproducibility; a formal Template Release must pass sanitization, compatibility, independent reproduction, review, and versioned publication.
- Automated browser evidence proves repeatability and integration, not human novice usability.
- Preserve all unrelated dirty-worktree changes; stage and commit only files named by the current task.
- Source design: `docs/superpowers/specs/2026-08-30-frontend-scientific-journeys-design.md`.

---

## File structure

- Create `playwright.live.config.cjs`: isolated Playwright configuration for the live local stack; it must never start or mock an API.
- Create `tests/ui-live/live-helpers.js`: bounded polling, evidence capture, unique test-name generation, and JSON audit-row writer.
- Create `tests/ui-live/scientific-journeys.spec.js`: ordered browser journeys and release-gate assertions.
- Create `artifacts/qa/frontend-scientific-journeys/.gitkeep`: documents the local evidence root while screenshots and run-specific JSON remain generated artifacts.
- Create `docs/qa/2026-08-30-frontend-scientific-journey-report.md`: human-readable matrix containing every action, fact source, outcome, ID, and defect.
- Modify `package.json`: add the explicit `test:ui:live` entry without changing the existing mocked `test:ui` suite.

The acceptance run intentionally does not modify product implementation. Each
Blocker or Severe result will be assigned to one of four follow-up plans:

1. authoritative facts and sbatch-first Studio;
2. undergraduate files/results/rerun/direct-sharing journey;
3. graduate array monitoring and selective recovery;
4. formal template sanitization/reproduction/review/adoption.

---

### Task 1: Add an isolated live-browser harness

**Files:**
- Create: `playwright.live.config.cjs`
- Create: `tests/ui-live/live-helpers.js`
- Create: `tests/ui-live/scientific-journeys.spec.js`
- Create: `artifacts/qa/frontend-scientific-journeys/.gitkeep`
- Modify: `package.json:5-13`

**Interfaces:**
- Consumes: a running frontend from `PILOT107_LIVE_BASE_URL`, defaulting to `http://127.0.0.1:13000`.
- Produces: `uniqueName(prefix): string`, `captureEvidence(page, caseId, payload): Promise<void>`, and `waitForRunTerminal(page): Promise<string>`.

- [ ] **Step 1: Write the failing harness smoke test**

Create `tests/ui-live/scientific-journeys.spec.js` with a public-page smoke test and no `page.route` calls:

```js
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
```

- [ ] **Step 2: Run the smoke test and verify the missing config/helper failure**

Run: `npx playwright test --config=playwright.live.config.cjs --grep LIVE-00`

Expected: FAIL because `playwright.live.config.cjs` or `live-helpers.js` does not exist.

- [ ] **Step 3: Implement the live config and bounded evidence helper**

Create `playwright.live.config.cjs`:

```js
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
```

Create `tests/ui-live/live-helpers.js`:

```js
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
```

Add to `package.json` scripts:

```json
"test:ui:live": "playwright test --config=playwright.live.config.cjs"
```

- [ ] **Step 4: Run the live harness smoke test**

Run: `npm run test:ui:live -- --grep LIVE-00`

Expected: PASS and files `LIVE-00.png` plus `LIVE-00.json` exist under the evidence root.

- [ ] **Step 5: Verify that the live suite contains no network mocks**

Run: `rg -n "page\.route|route\.fulfill|installMockApi" tests/ui-live playwright.live.config.cjs`

Expected: no matches.

- [ ] **Step 6: Commit the harness**

```bash
git add package.json playwright.live.config.cjs tests/ui-live artifacts/qa/frontend-scientific-journeys/.gitkeep
git commit -m "test: add live frontend journey harness"
```

---

### Task 2: Record authoritative-fact and file-path baseline

**Files:**
- Modify: `tests/ui-live/scientific-journeys.spec.js`
- Create: `docs/qa/2026-08-30-frontend-scientific-journey-report.md`

**Interfaces:**
- Consumes: `captureEvidence()` from Task 1 and the public `/cluster`, `/studio/new`, and `/files` routes.
- Produces: evidence cases `FACT-01` through `FILE-04` and report rows using `PASS | PARTIAL | FAIL | BLOCKED`.

- [ ] **Step 1: Add the fact-consistency release-gate test**

```js
test("FACT-01 cluster facts and Studio defaults agree", async ({ page }) => {
  await page.goto("/cluster?user=alice");
  await expect(page.getByText("CPU-RC", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("qos_cpu_rc", { exact: true }).first()).toBeVisible();
  await captureEvidence(page, "FACT-01-cluster", { expected_partition: "CPU-RC", expected_qos: "qos_cpu_rc" });

  await page.goto("/studio/new?user=alice");
  await expect(page.getByLabel("Partition")).toHaveValue("CPU-RC");
  await expect(page.getByLabel("QoS")).toHaveValue("qos_cpu_rc");
  await captureEvidence(page, "FACT-01-studio", { outcome: "PASS" });
});
```

- [ ] **Step 2: Add entitlement and freshness assertions**

Assert that a successful user with an observed association sees either an
authoritative entitlement or a visible refresh/explanation action. Capture the
data age, source authority, partition count, association count, and exact
limitation text in `FACT-02.json`.

- [ ] **Step 3: Add manual-path and file-search assertions**

Drive the actual file page through labels and roles:

```js
test("FILE-01 manually entered path and search result remain usable", async ({ page }) => {
  await page.goto("/files?user=alice");
  const pane = page.locator(".file-pane").first();
  await pane.getByTitle("手动输入路径").click();
  await pane.getByLabel("路径").fill("/public/home/alice");
  await pane.getByLabel("路径").press("Enter");
  await expect(pane).toHaveAttribute("data-pane-cwd", "/public/home/alice");
  await page.getByLabel("搜索文件名或路径").fill("slurm");
  await expect(page.locator(".file-search-panel")).toContainText(/slurm/i);
  await captureEvidence(page, "FILE-01", { outcome: "PASS" });
});
```

Also test an absent path, a path outside the owner root, and a large text file;
the visible error must explain existence, permission, or truncation rather than
showing a generic request failure.

- [ ] **Step 4: Run and preserve the expected baseline failures**

Run: `npm run test:ui:live -- --grep "FACT-|FILE-"`

Expected on the current baseline: file discovery may pass; `FACT-01` or
`FACT-02` may fail because Studio defaults and entitlement do not agree with the
cluster page. Do not weaken assertions to make the run green.

- [ ] **Step 5: Write the first report section with exact evidence**

Start `docs/qa/2026-08-30-frontend-scientific-journey-report.md` with:

```markdown
| Case | User action | Required fact | Visible source | Durable fact | Outcome | Recovery | Evidence | Severity |
|---|---|---|---|---|---|---|---|---|
| FACT-01 | Compare cluster and new Studio | partition/QoS | `/cluster`, `/studio/new` | none | FAIL | none visible | `FACT-01-*.json` | Severe |
```

Replace the sample outcome only with the observed value; include the exact UI
text and timestamp below the table.

- [ ] **Step 6: Commit the baseline tests and report**

```bash
git add tests/ui-live/scientific-journeys.spec.js docs/qa/2026-08-30-frontend-scientific-journey-report.md
git commit -m "test: record cluster and file journey baseline"
```

---

### Task 3: Execute the undergraduate discovery and sbatch-authoring journey

**Files:**
- Modify: `tests/ui-live/scientific-journeys.spec.js`
- Modify: `docs/qa/2026-08-30-frontend-scientific-journey-report.md`

**Interfaces:**
- Consumes: public `/projects`, `/market`, market detail/application, and `/studio` routes.
- Produces: cases `NOVICE-01` through `NOVICE-08` and the first exact submitted-script digest.

- [ ] **Step 1: Add market discovery assertions**

Navigate from the workload home, not a deep link. Assert that a novice can
reach the market within three principal clicks, search by scientific purpose,
distinguish a successful Run from a curated template, and read human summaries
for inputs, outputs, resource estimate, compatibility source/time, author,
version, and verification.

The DOM gate is explicit:

```js
await expect(page.locator("main")).not.toContainText(/Compatibility JSON|Contract plan JSON|Canonical Contract payload|Publication metadata JSON/);
```

- [ ] **Step 2: Adopt through the visible application and confirmation flow**

Use the first current-platform-compatible curated CPU template. Record its item
ID and version. Start the Agent application plan, require a natural-language
description next to the structured approval, confirm it, and verify navigation
to the server-created private Studio document.

- [ ] **Step 3: Assert the sbatch-first editing contract**

The Studio test must require:

```js
const script = page.getByLabel("sbatch 作业脚本");
await expect(script).toBeVisible();
await expect(script).toContainText("#!/bin/bash");
await expect(script).toContainText("#SBATCH --partition=CPU-RC");
await expect(script).toContainText("#SBATCH --qos=qos_cpu_rc");
await expect(page.locator("main")).not.toContainText(/源码投影|YAML|Environment JSON|Extensions JSON/);
```

Edit a recognized resource field and assert the script changes; append a legal
unrecognized directive/comment, save, reload, and assert byte-preserving
retention. Introduce one invalid directive and require a line-bound diagnostic.

- [ ] **Step 4: Assert exact-script approval binding**

After validation and preparation, compare the editor text to the submitted
script shown in the approval summary, record its SHA-256 from the visible
receipt, confirm the concrete Run ID, and navigate to the Run detail. The test
must fail if the UI has only a materialized preview and no exact primary script.

- [ ] **Step 5: Run the novice authoring slice**

Run: `npm run test:ui:live -- --grep "NOVICE-0[1-8]"`

Expected on the current baseline: market discovery and adoption may progress;
the raw-JSON and sbatch-primary gates are expected to fail. Record the last
successful user action and the first impossible action rather than bypassing it.

- [ ] **Step 6: Update and commit the evidence report**

For every step, add required information, whether it was available, exact user
action, response, recovery, IDs, and severity. Commit only the test and report:

```bash
git add tests/ui-live/scientific-journeys.spec.js docs/qa/2026-08-30-frontend-scientific-journey-report.md
git commit -m "test: capture novice sbatch journey gaps"
```

---

### Task 4: Complete submission, results, rerun, and direct Run sharing

**Files:**
- Modify: `tests/ui-live/scientific-journeys.spec.js`
- Modify: `docs/qa/2026-08-30-frontend-scientific-journey-report.md`

**Interfaces:**
- Consumes: the prepared Run from Task 3, or the newest frontend-visible
  successful Run owned by `alice` if Task 3 is blocked before submission.
- Produces: `RUN-01` through `RUN-07`, `SHARE-01` through `SHARE-08`, Run ID,
  Job ID, result paths, publication ID, and visitor URL.

- [ ] **Step 1: Submit and observe the real Job**

Confirm the exact Run ID, submit, and poll through the Run page until a terminal
state. Record the real Job ID and visible scheduler state transitions. No API
polling is allowed. The terminal Run must survive a page reload.

- [ ] **Step 2: Inspect logs and scientific results**

Open Logs and Results through tabs. Require stdout/stderr to be separated from
scientific assets, preview the first image or bounded text result, download one
asset, and record its source Job/Run. Mark `PARTIAL` if only Slurm log files are
available and no declared scientific output is identified.

- [ ] **Step 3: Create and compare a rerun**

Use the visible clone/retry action, require a new Run ID and parent lineage,
change one scientific parameter through Studio if the UI offers it, submit, and
use Compare to assert the parameter/resource/result differences. Do not count a
byte-identical retry without editable inputs as the requested rerun experience.

- [ ] **Step 4: Publish a selectively shared successful Run**

Use a title from `uniqueName("heat-run-share")`. Select only resource summary,
result summary, script, and one small asset; leave reference Contract and
evidence preview unselected. Require the natural-language disclosure summary,
the explicit not-reproducible label, final preview, and confirmation checkbox.

- [ ] **Step 5: Verify as a different user**

Switch to `bob` through the frontend user selector and open the stable market
URL. Assert that selected content is visible and unselected content, private
absolute paths, usernames embedded in paths, environment variables, and secret
patterns are absent. Attempt to address an unselected asset only through visible
links; no hidden link may be rendered.

- [ ] **Step 6: Withdraw and verify disappearance**

Switch back to `alice`, enter an explicit reason, withdraw, return as `bob`, and
require the public item to be unavailable or visibly withdrawn. Capture owner
preview, visitor view, and post-withdrawal view.

- [ ] **Step 7: Run and commit the lifecycle evidence**

Run: `npm run test:ui:live -- --grep "RUN-|SHARE-"`

Expected: every mutation yields a visible immutable identifier. Any privacy
leak is a Blocker; preview mismatch or false reproducibility wording is Severe.

```bash
git add tests/ui-live/scientific-journeys.spec.js docs/qa/2026-08-30-frontend-scientific-journey-report.md
git commit -m "test: exercise Run results rerun and sharing"
```

---

### Task 5: Execute formal Template Release and cross-user adoption

**Files:**
- Modify: `tests/ui-live/scientific-journeys.spec.js`
- Modify: `docs/qa/2026-08-30-frontend-scientific-journey-report.md`

**Interfaces:**
- Consumes: a successful source Run, author `alice`, reviewer identity exposed
  by the demo role directory, and adopter `bob`.
- Produces: `TPL-01` through `TPL-12`, draft/review/reproduction/release/adoption
  IDs, and the independent adopted Run ID/Job ID.

- [ ] **Step 1: Start a structured publication draft**

Open "从成功 Run 发布模板", select the source Run through a picker rather than
copying an opaque ID when possible, and fill title, description, visibility,
audience, compatible resource type, overridable resource range, parameter
schema, output rules, and environment requirements using structured controls.

Assert the ordinary page has no Compatibility JSON, Publication metadata JSON,
or Contract JSON textarea.

- [ ] **Step 2: Review sanitization findings and exact diff**

Require the UI to scan owner path, username, host, token-like string, private
input, temporary output, and author-only Slurm settings. The test confirms every
rewrite in a visible before/after diff and verifies that a high-risk unresolved
finding blocks progress.

- [ ] **Step 3: Verify compatibility provenance**

Require current `CPU-RC/qos_cpu_rc`, fact source, capture timestamp, resource
limits, and separate local-versus-portable claims. A hard-coded `Students`
compatibility object is a Severe failure.

- [ ] **Step 4: Run independent reproduction through the frontend**

Start the reproduction action, require a new workspace and Run, observe the
vm-slurm Job to completion, and compare declared output completeness. The source
Run's private result path must not be reused.

- [ ] **Step 5: Submit, review, and publish a version**

Submit for review, switch to the authorized reviewer in the frontend, inspect
sanitization diff, exact sbatch, compatibility evidence, reproduction Run, and
risks, then approve with a note. Switch back to `alice` and publish version
`1.0.0`; record immutable release and verification IDs.

- [ ] **Step 6: Adopt and run as `bob`**

Open the release from the market, verify purpose/parameters/compatibility and
verification time, adopt into Bob's private workspace, confirm private path
substitution and Bob's entitlement, inspect the exact personal sbatch, submit,
and observe a separate real Job. The Job must not require Alice's private files.

- [ ] **Step 7: Verify version immutability**

Return as Alice, modify the draft and require a new version path; assert the
released `1.0.0` content digest and Bob's adopted copy do not change.

- [ ] **Step 8: Run and commit template evidence**

Run: `npm run test:ui:live -- --grep "TPL-"`

Expected on the current baseline: draft/session/review APIs may exist, but raw
JSON authoring and manual reproduction evidence are expected blockers. Stop at
the first impossible frontend action and record it; do not manufacture evidence.

```bash
git add tests/ui-live/scientific-journeys.spec.js docs/qa/2026-08-30-frontend-scientific-journey-report.md
git commit -m "test: exercise formal template publication journey"
```

---

### Task 6: Execute the graduate Slurm-array and selective-recovery journey

**Files:**
- Modify: `tests/ui-live/scientific-journeys.spec.js`
- Modify: `docs/qa/2026-08-30-frontend-scientific-journey-report.md`

**Interfaces:**
- Consumes: sbatch-first Studio, vm-slurm array support, Run workflow read model,
  and frontend-visible selective recovery.
- Produces: `ARRAY-01` through `ARRAY-10`, parent Job ID, child-state counts,
  recovery Run ID, recovered task expression, and aggregate completeness state.

- [ ] **Step 1: Enter the exact array script**

Use the design's Ising script with `#SBATCH --array=0-199%8`, fixed task-derived
seeds, `%A_%a` logs, and `CPU-RC/qos_cpu_rc`. Save and reload; compare the text
byte for byte, including comments, quotes, directive order, and shell options.

- [ ] **Step 2: Verify bounded resource interpretation**

Require the preflight summary to explain 200 tasks, maximum concurrency 8, two
CPUs and 6 GiB per task, and maximum concurrent demand of 16 CPUs/48 GiB rather
than multiplying all 200 tasks. Record cluster limit provenance.

- [ ] **Step 3: Submit and inspect parent/child state**

Submit through confirmation, record the parent Job ID, and require a single
overview with counts for pending/running/succeeded/failed/cancelled children.
Filter by task ID and open one child log without opening 200 separate pages.

- [ ] **Step 4: Produce and explain a controlled partial failure**

Use a script parameter that deterministically makes three documented task IDs
omit their declared artifact while exiting through the normal Job path. Require
the frontend to report partial completion, missing tasks, common failure, and
aggregate incompleteness; 197/200 must not become plain success.

- [ ] **Step 5: Recover only failed tasks**

Select "重跑失败任务", inspect a natural-language and structured approval that
names the compressed array expression, confirm it, and require a new Run linked
to the source. Verified successful tasks must be reused, not resubmitted.

- [ ] **Step 6: Verify final aggregate provenance**

After recovery, open the aggregate artifact and require links to all contributing
task attempts, parameter-file digest, script digest, environment, and seed. The
UI must distinguish scheduler completion from scientific validation.

- [ ] **Step 7: Run and commit array evidence**

Run: `npm run test:ui:live -- --grep "ARRAY-"`

Expected on the current baseline: underlying workflow recovery primitives may
exist, but the browser journey is likely blocked before array monitoring or
selective recovery. Record the first absent visible action.

```bash
git add tests/ui-live/scientific-journeys.spec.js docs/qa/2026-08-30-frontend-scientific-journey-report.md
git commit -m "test: exercise graduate array recovery journey"
```

---

### Task 7: Verify Agent authority and human-readable approval at every entry point

**Files:**
- Modify: `tests/ui-live/scientific-journeys.spec.js`
- Modify: `docs/qa/2026-08-30-frontend-scientific-journey-report.md`

**Interfaces:**
- Consumes: the cluster, files, Studio, Run, direct-share, and template-release
  Agent entry points exercised by Tasks 2 through 6.
- Produces: `AUTH-01` through `AUTH-07`, including the proposed action, natural-
  language explanation, approval state, executed object ID, and denied action.

- [ ] **Step 1: Assert entry-point-specific authority copy**

Open the Agent from each entry point and require a visible boundary summary:

```js
const expected = [
  ["/cluster?user=alice", /只读|不会修改集群/],
  ["/files?user=alice", /读取.*路径|写入.*需要确认/],
  ["/studio/new?user=alice", /修改.*草稿|提交.*需要确认/],
  ["/runs?user=alice", /诊断|取消.*需要确认|重跑.*需要确认/],
  ["/templates/new?user=alice", /发布.*需要确认/],
];
for (const [url, boundary] of expected) {
  await page.goto(url);
  await expect(page.locator("main")).toContainText(boundary);
}
```

If an entry point has no Agent, record `NOT_APPLICABLE` only when the approved
design does not assign authority there; the five paths above are required.

- [ ] **Step 2: Assert natural language accompanies structured approval**

For one Studio edit, one Run cancel/rerun draft, one direct-share publication,
and one template-release publication, require both a paragraph explaining the
effect and a structured summary naming the exact target, resources/disclosure,
and immutable digest. A raw object or event payload without prose fails.

- [ ] **Step 3: Assert no mutation before confirmation**

Capture the relevant Run/draft/publication state, ask the Agent to prepare the
action, reload the page, and confirm that no Job, cancellation, share, or release
exists. Then confirm once and require exactly one durable result. Reconfirming a
completed approval must be idempotent rather than duplicating the action.

- [ ] **Step 4: Assert streaming response and folded tools**

Run one read-only platform question and require incremental natural-language
content in the dedicated response area. Tool events must be collapsed by
default but expandable. The terminal response must contain non-whitespace text;
an empty `"\n\n"`, opaque timeout, or tool-only transcript fails `AUTH-06`.

- [ ] **Step 5: Assert a forbidden action remains unavailable**

From the cluster Agent, request a mutation such as cancelling an unrelated Run.
Require a refusal that explains the cluster entry point's read-only boundary and
offers navigation to the correct Run. No approval object may be created.

- [ ] **Step 6: Run and commit authority evidence**

Run: `npm run test:ui:live -- --grep "AUTH-"`

```bash
git add tests/ui-live/scientific-journeys.spec.js docs/qa/2026-08-30-frontend-scientific-journey-report.md
git commit -m "test: verify frontend Agent authority boundaries"
```

---

### Task 8: Consolidate the defect ledger and verify evidence integrity

**Files:**
- Modify: `docs/qa/2026-08-30-frontend-scientific-journey-report.md`
- Modify: `tests/ui-live/scientific-journeys.spec.js`

**Interfaces:**
- Consumes: all `FACT`, `FILE`, `NOVICE`, `RUN`, `SHARE`, `TPL`, `ARRAY`, and
  `AUTH` evidence.
- Produces: a prioritized defect ledger and four bounded remediation-plan inputs.

- [ ] **Step 1: Run the full live suite once without retries**

Run: `npm run test:ui:live`

Expected: one deterministic result per release gate; failed tests retain trace,
screenshot, and video. Do not rerun individual failures until the report records
the original state.

- [ ] **Step 2: Verify evidence completeness**

Run:

```bash
find artifacts/qa/frontend-scientific-journeys -maxdepth 1 -type f -print | sort
```

Expected: Playwright JSON plus evidence for every step reached. Each report row
must link to a local artifact and include the user, visible action, required
information, observed response, recovery, immutable ID when produced, outcome,
and severity.

- [ ] **Step 3: Add the prioritized defect ledger**

Group defects exactly into:

```markdown
## Blockers
## Severe truth or privacy defects
## Normal workflow defects
## Experience defects
## Passed capabilities
## Remediation plan inputs
### Facts and sbatch-first Studio
### Undergraduate lifecycle and direct sharing
### Arrays and selective recovery
### Formal Template Release
```

Deduplicate symptoms that share one cause, but keep independent user impacts as
separate acceptance cases.

- [ ] **Step 4: Run source-level regression checks**

Run:

```bash
npm test -- --run
npm run typecheck
npm run build
```

Expected: existing mocked/unit web suite, TypeScript, and production build pass;
the live acceptance result remains reported separately and is not hidden by the
source checks.

- [ ] **Step 5: Self-review against the approved spec**

Read every section of
`docs/superpowers/specs/2026-08-30-frontend-scientific-journeys-design.md` and
map it to a case ID in the report. Add a missing case before declaring the
baseline complete. Confirm that direct sharing and formal release have separate
results.

- [ ] **Step 6: Commit the completed acceptance report**

```bash
git add tests/ui-live/scientific-journeys.spec.js docs/qa/2026-08-30-frontend-scientific-journey-report.md
git commit -m "docs: report live scientific journey acceptance"
```

At this point, write one implementation plan per remediation group. The first
plan must address authoritative facts and sbatch-first Studio because all later
journeys consume that foundation.
