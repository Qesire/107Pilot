const { expect, test } = require("@playwright/test");

const NOW = "2026-09-04T05:40:00Z";

test("failed Run keeps repair local and loads remediation only after explicit action", async ({ page }) => {
  let remediationReads = 0;
  let healthReads = 0;
  let created = false;
  const repairSession = remediationSessionPayload();

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/v1/web/session") {
      return json(route, { identity_mode: "demo", user: "alice", switchable: true });
    }
    if (url.pathname === "/api/v1/runs/run_failure_recovery") {
      return json(route, runPayload());
    }
    if (url.pathname === "/api/v1/runs/run_failure_recovery/workspace") {
      return json(route, workspacePayload());
    }
    if (url.pathname === "/api/v1/health/ready") {
      healthReads += 1;
      return json(route, {
        status: "ready",
        checks: [{ name: "local_llm", status: "disabled" }],
      });
    }
    if (url.pathname === "/api/v1/remediation-sessions" && request.method() === "GET") {
      remediationReads += 1;
      return json(route, {
        items: created ? [repairSession] : [],
        page: { limit: 50, has_more: false, next_cursor: null },
      });
    }
    if (
      url.pathname === "/api/v1/runs/run_failure_recovery/remediation-sessions"
      && request.method() === "POST"
    ) {
      created = true;
      return json(route, repairSession, 201);
    }
    if (url.pathname === "/api/v1/remediation-sessions/repair_visual") {
      return json(route, repairSession);
    }

    return json(route, { error: { code: "TEST.UNMOCKED", message: url.pathname } }, 404);
  });

  await page.goto("/runs/run_failure_recovery?user=alice");
  await expect(page.getByRole("heading", { name: "运行详情" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "计算失败" })).toBeVisible();
  expect(remediationReads).toBe(0);
  expect(healthReads).toBe(0);

  await page.getByRole("button", { name: /准备修复/ }).click();
  await expect(page.getByRole("heading", { name: "准备修复这次运行" })).toBeVisible();
  await expect(page.getByText(/解释来自 1 条持久化诊断/)).toBeVisible();
  await expect.poll(() => remediationReads).toBeGreaterThan(0);
  await expect.poll(() => healthReads).toBeGreaterThan(0);
  await expect(page).toHaveURL(/\/runs\/run_failure_recovery/);

  await page.getByRole("button", { name: "创建修复会话" }).click();
  await expect(page.getByText("repair_visual", { exact: true })).toBeVisible();
  await expect(page.getByText("形成方案", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/runs\/run_failure_recovery/);
});

function runPayload() {
  return {
    run_id: "run_failure_recovery",
    owner: "alice",
    contract_id: "contract_failure_recovery",
    state: "FAILED",
    job_id: "55123",
    job_name: "dependency-check",
    workdir: "/public/home/alice/failure-recovery",
    collection_state: "succeeded",
    diagnosis_state: "succeeded",
    capsule_state: "none",
    result_status: "failed",
    parent_run_id: null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function workspacePayload() {
  return {
    run: {
      run_id: "run_failure_recovery",
      owner: "alice",
      job_name: "dependency-check",
      created_at: NOW,
      updated_at: NOW,
      exit_code: "1:0",
      terminal_state: "FAILED",
      attempt: 1,
    },
    states: {
      execution: "FAILED",
      collection: "succeeded",
      diagnosis: "succeeded",
      capsule: "none",
      result: "failed",
    },
    outcome: {
      kind: "failed",
      summary: "作业以非成功状态结束；退出码为 1:0。",
    },
    attention: {
      severity: "critical",
      title: "Python 运行环境缺少作业需要的包。",
      detail: "依据已有持久化诊断；请核对其 Evidence 引用。",
    },
    next_action: {
      kind: "prepare_repair",
      label: "查看诊断并准备修复",
      detail: "已有持久化诊断；先核对 Evidence，再决定是否进入受控修复。",
    },
    evidence_summary: {
      object_count: 2,
      result_count: 0,
      diagnosis_count: 1,
      stdout_available: true,
      stderr_available: true,
      capsule_available: false,
    },
    provenance: {
      contract_id: "contract_failure_recovery",
      contract_digest: "d".repeat(64),
      workdir: "/public/home/alice/failure-recovery",
      job_id: "55123",
      parent_run_id: null,
      lineage_reason: null,
      remediation_plan_id: null,
      workspace_revision: null,
      workspace_digest: null,
      source_revision: null,
      platform_snapshot_ref: null,
    },
  };
}

function remediationSessionPayload() {
  return {
    session_id: "repair_visual",
    owner: "alice",
    state: "planning",
    version: 2,
    source_run_id: "run_failure_recovery",
    source_contract_id: "contract_failure_recovery",
    source_diagnosis_digest: "a".repeat(64),
    source_evidence_digest: "b".repeat(64),
    automation_policy: "manual_approval",
    provider: "none",
    lease: { owner: null, expires_at: null },
    budget: {
      max_attempts: 3,
      max_submissions: 1,
      max_wall_time_seconds: 900,
      max_llm_calls: 3,
      max_llm_tokens: 4000,
    },
    usage: {
      attempts: 0,
      submissions: 0,
      wall_time_seconds: 0,
      llm_calls: 0,
      llm_tokens: 0,
    },
    stop_reason: null,
    takeover_reason: null,
    turns: [],
    proposals: [],
    decisions: [],
    executions: [],
    evaluations: [],
    created_at: NOW,
    updated_at: NOW,
  };
}

function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
