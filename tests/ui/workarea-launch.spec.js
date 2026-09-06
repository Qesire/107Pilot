const { expect, test } = require("@playwright/test");

const WORKAREA_ID = "workarea-ui";
const CONTRACT_ID = "contract-ui";
const CANDIDATE_ID = "launchcand-ui";
const LAUNCH_ID = "launch-ui";
const RUN_ID = "run-ui";

async function installWorkAreaLaunchMock(page) {
  let workarea = null;
  let preflight = null;
  let launch = null;

  await page.unrouteAll({ behavior: "ignoreErrors" });
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
      return json({ items: workarea ? [workarea] : [] });
    }
    if (url.pathname === "/api/v1/workareas" && request.method() === "POST") {
      const body = request.postDataJSON();
      workarea = {
        workarea_id: WORKAREA_ID,
        owner: "alice",
        title: body.title,
        description: body.description,
        created_at: "2026-09-06T09:00:00Z",
        updated_at: "2026-09-06T09:00:00Z",
        bindings: { contracts: [], runs: [], assets: [] },
      };
      return json(workarea, 201);
    }
    if (url.pathname === `/api/v1/workareas/${WORKAREA_ID}` && request.method() === "GET") {
      return json(workarea);
    }
    if (url.pathname === `/api/v1/workareas/${WORKAREA_ID}/launches`) {
      return json({ items: launch ? [launch] : [] });
    }
    if (url.pathname === "/api/v1/contracts" && request.method() === "GET") {
      return json({
        items: [{
          contract_id: CONTRACT_ID,
          recipe_version_id: "recipe_python_cpu@1.0.0",
          digest: "contract-digest",
          created_at: "2026-09-06T09:00:00Z",
        }],
      });
    }
    if (
      url.pathname === `/api/v1/workareas/${WORKAREA_ID}/launch-candidates`
      && request.method() === "POST"
    ) {
      return json({
        candidate_id: CANDIDATE_ID,
        workarea_id: WORKAREA_ID,
        owner: "alice",
        contract_id: CONTRACT_ID,
        title: request.postDataJSON().title,
        note: request.postDataJSON().note,
        candidate_digest: "candidate-digest",
        created_at: "2026-09-06T09:01:00Z",
        updated_at: "2026-09-06T09:01:00Z",
        preflight: null,
      }, 201);
    }
    if (
      url.pathname === `/api/v1/launch-candidates/${CANDIDATE_ID}/preflight`
      && request.method() === "POST"
    ) {
      preflight = {
        preflight_id: "preflight-ui",
        candidate_id: CANDIDATE_ID,
        candidate_digest: "candidate-digest",
        status: "OK",
        findings: [],
        effective_request: {
          workdir: "/public/home/alice/project",
          script: "#!/bin/bash\npython train.py\n",
          run_submit_request: {
            owner: "alice",
            workdir: "/public/home/alice/project",
            script: "#!/bin/bash\npython train.py\n",
            job_name: "ui-launch",
            contract_id: CONTRACT_ID,
            parent_run_id: null,
            lineage_reason: null,
            remediation_plan_id: null,
            workflow: { dependencies: [], retry: { max_attempts: 1, backoff_seconds: 0 } },
            resource_plan: {
              partition: "Students",
              qos: "qos_stu_medium_2gpu",
              nodes: 1,
              ntasks: 1,
              cpus_per_task: 8,
              memory_value: null,
              memory_unit: null,
              gpus_per_node: 1,
              gpus_total: null,
              gpu_type: null,
              time_limit: "01:00:00",
              array: null,
            },
          },
        },
        assessment_digest: "preflight-digest",
        created_at: "2026-09-06T09:01:01Z",
      };
      return json(preflight);
    }
    if (
      url.pathname === `/api/v1/launch-candidates/${CANDIDATE_ID}`
      && request.method() === "GET"
    ) {
      return json({
        candidate_id: CANDIDATE_ID,
        workarea_id: WORKAREA_ID,
        owner: "alice",
        contract_id: CONTRACT_ID,
        title: "UI baseline",
        note: "vertical test",
        candidate_digest: "candidate-digest",
        created_at: "2026-09-06T09:01:00Z",
        updated_at: "2026-09-06T09:01:00Z",
        preflight,
      });
    }
    if (
      url.pathname === `/api/v1/launch-candidates/${CANDIDATE_ID}/commit`
      && request.method() === "POST"
    ) {
      const body = request.postDataJSON();
      if (body.preflight_digest !== "preflight-digest") {
        return json({ error: { code: "LAUNCH.CONFLICT", message: "stale preflight" } }, 409);
      }
      launch = {
        launch_id: LAUNCH_ID,
        candidate_id: CANDIDATE_ID,
        preflight_id: "preflight-ui",
        workarea_id: WORKAREA_ID,
        owner: "alice",
        contract_id: CONTRACT_ID,
        candidate_digest: "candidate-digest",
        preflight_digest: "preflight-digest",
        committed_at: "2026-09-06T09:02:00Z",
        submitted_at: "2026-09-06T09:02:01Z",
        submit_error: null,
        run_ids: [RUN_ID],
      };
      return json({
        launch,
        run: {
          run_id: RUN_ID,
          contract_id: CONTRACT_ID,
          state: "SUBMITTED",
          job_id: "12345",
          workdir: "/public/home/alice/project",
          created_at: "2026-09-06T09:02:00Z",
          updated_at: "2026-09-06T09:02:01Z",
        },
        submit_error: null,
      }, 201);
    }
    if (url.pathname === `/api/v1/launches/${LAUNCH_ID}` && request.method() === "GET") {
      return json(launch);
    }
    if (url.pathname === `/api/v1/runs/${RUN_ID}` && request.method() === "GET") {
      return json({
        run_id: RUN_ID,
        owner: "alice",
        state: "SUBMITTED",
        collection_state: "pending",
        diagnosis_state: "pending",
        capsule_state: "pending",
        result_status: "UNKNOWN",
        job_id: "12345",
        workdir: "/public/home/alice/project",
        script: "#!/bin/bash\npython train.py\n",
        contract_id: CONTRACT_ID,
        created_at: "2026-09-06T09:02:00Z",
        updated_at: "2026-09-06T09:02:01Z",
      });
    }

    return route.continue();
  });
}

test.beforeEach(async ({ page }) => {
  await installWorkAreaLaunchMock(page);
});

test("creates a WorkArea, reviews effective request, commits once, and reaches Run authority", async ({ page }) => {
  await page.goto("/workareas?user=alice");

  await expect(page.getByRole("heading", { name: "研究区" })).toBeVisible();
  await page.getByRole("button", { name: "新建研究区" }).click();
  await page.getByLabel("名称").fill("Competition vertical");
  await page.getByLabel("说明").fill("WorkArea to Launch to Run");
  await page.getByRole("button", { name: "创建并进入" }).click();
  await expect(page).toHaveURL(new RegExp(`/workareas/${WORKAREA_ID}`));

  await expect(page.getByRole("heading", { name: "Competition vertical" })).toBeVisible();
  await page.getByRole("button", { name: "新建运行" }).click();
  await expect(page).toHaveURL(new RegExp(`/workareas/${WORKAREA_ID}/launch/new`));

  await page.getByLabel("Contract").selectOption(CONTRACT_ID);
  await page.getByLabel("Launch 标题").fill("UI baseline");
  await page.getByLabel("备注").fill("vertical test");
  await page.getByRole("button", { name: "生成预检并进入 Review" }).click();
  await expect(page).toHaveURL(new RegExp(`/launches/${CANDIDATE_ID}/review`));

  await expect(page.getByRole("heading", { name: "Effective Slurm Request" })).toBeVisible();
  await expect(page.getByText("Students / qos_stu_medium_2gpu")).toBeVisible();
  await expect(page.getByDisplayValue(/python train\.py/)).toBeVisible();
  await expect(page.getByText("preflight-digest")).toBeVisible();

  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Commit 并提交 Run" }).click();
  await expect(page).toHaveURL(new RegExp(`/launches/${LAUNCH_ID}`));

  await expect(page.getByRole("heading", { name: LAUNCH_ID })).toBeVisible();
  await expect(page.getByText("12345")).toBeVisible();
  await expect(page.getByText(RUN_ID)).toBeVisible();

  await page.getByRole("button", { name: /打开 Run、日志与 Evidence/ }).click();
  await expect(page).toHaveURL(new RegExp(`/runs/${RUN_ID}`));
});
