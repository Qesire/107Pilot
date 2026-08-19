import { readFile } from "node:fs/promises";

import { Value } from "typebox/value";
import { describe, expect, it } from "vitest";

const ROOTS = {
  agent: new URL("../../../schemas/agent/v2/", import.meta.url),
  runtime: new URL("../../../schemas/runtime-watch/v1/", import.meta.url),
  observability: new URL("../../../schemas/observability/v1/", import.meta.url),
} as const;

const CASES = [
  {
    root: ROOTS.agent,
    name: "project-session.schema.json",
    payload: {
      schema_version: "pilot107.experiment-project-session/v1",
      project_id: "project-1",
      owner: "alice",
      origin: "blank",
      state: "drafting",
      version: 0,
      goal: "Create a small numerical Slurm experiment.",
      source: null,
      blueprint: {
        goal: "Read parameters and sum a numeric series.",
        entrypoints: ["main.py"],
        files: [
          { path: "main.py", purpose: "Experiment entrypoint", classification: "editable" },
        ],
        validations: [
          {
            validation_id: "syntax",
            execution: "sandbox",
            argv: ["python", "-m", "py_compile", "main.py"],
            expected_outputs: [],
          },
        ],
        contract_intent: {
          recipe_version_id: "recipe_python_cpu@1.0.0",
          resource_hints: { cpus_per_task: 1, time_limit: "00:05:00" },
        },
        expected_outputs: [{ path: "result.json", kind: "json", required: true }],
        dependencies: [{ name: "python", version: ">=3.12", source: "runtime" }],
        open_questions: [],
      },
      created_at: "2026-08-19T00:00:00Z",
      updated_at: "2026-08-19T00:00:00Z",
    },
  },
  {
    root: ROOTS.agent,
    name: "workspace-changeset.schema.json",
    payload: {
      schema_version: "pilot107.workspace-changeset/v1",
      change_set_id: "changeset-1",
      project_id: "project-1",
      workspace_id: "workspace-1",
      owner: "alice",
      base_snapshot_digest: "a".repeat(64),
      digest: "b".repeat(64),
      state: "reviewable",
      version: 2,
      files: [
        {
          path: "main.py",
          operation: "create",
          before_sha256: null,
          after_sha256: "c".repeat(64),
          diff_sha256: "d".repeat(64),
          size_bytes: 128,
        },
      ],
      sandbox_results: [
        {
          result_id: "sandbox-1",
          argv: ["python", "-m", "py_compile", "main.py"],
          status: "succeeded",
          exit_code: 0,
          stdout_sha256: "e".repeat(64),
          stderr_sha256: "f".repeat(64),
        },
      ],
      approval: null,
      created_at: "2026-08-19T00:00:00Z",
      updated_at: "2026-08-19T00:01:00Z",
    },
  },
  {
    root: ROOTS.agent,
    name: "agent-task.schema.json",
    payload: {
      schema_version: "pilot107.agent-task/v1",
      task_id: "task-1",
      owner: "alice",
      session_id: "session-1",
      turn_id: "turn-1",
      project_id: "project-1",
      workspace_id: "workspace-1",
      task_kind: "slurm_validation",
      state: "pending",
      version: 0,
      request_key: "validate-1",
      resource_envelope: {
        partition: "debug",
        qos: "normal",
        cpus: 1,
        memory_mib: 1024,
        gpu_type: null,
        gpus: 0,
        walltime_seconds: 300,
        max_tasks: 1,
        max_submissions: 1,
        workspace_snapshot_digest: "a".repeat(64),
        expires_at: "2026-08-19T01:00:00Z",
        approved_by: "alice",
      },
      linked_run_id: null,
      result: null,
      lease: null,
      created_at: "2026-08-19T00:00:00Z",
      updated_at: "2026-08-19T00:00:00Z",
    },
  },
  {
    root: ROOTS.runtime,
    name: "runtime-watch.schema.json",
    payload: {
      schema_version: "pilot107.runtime-watch/v1",
      watch_id: "watch-1",
      run_id: "run-1",
      owner: "alice",
      connection_id: "simulator",
      state: "watching",
      version: 0,
      next_poll_at: "2026-08-19T00:00:05Z",
      lease_owner: null,
      lease_expires_at: null,
      fencing_token: 0,
      cursors: [
        {
          stream: "stdout",
          generation: 0,
          offset: 0,
          source_size: 0,
          source_mtime: null,
          source_file_identity: null,
          source_prefix_fingerprint: null,
          decoder_remainder_base64: "",
          last_data_at: null,
          last_checked_at: null,
          quiet_polls: 0,
          version: 0,
        },
      ],
      created_at: "2026-08-19T00:00:00Z",
      updated_at: "2026-08-19T00:00:00Z",
      stopped_at: null,
      last_error_code: null,
      last_error_at: null,
    },
  },
  {
    root: ROOTS.observability,
    name: "resource-observation.schema.json",
    payload: {
      schema_version: "pilot107.resource-observation/v1",
      observation_id: "observation-1",
      kind: "run_resource_summary",
      connection_id: "simulator",
      owner: "alice",
      run_id: "run-1",
      attempt: 0,
      cycle_id: "cycle-1",
      captured_at: "2026-08-19T00:05:00Z",
      freshness: "terminal",
      partial: false,
      warnings: [],
      measures: {
        allocated_cpus: {
          value: 1,
          unit: "count",
          availability: "available",
          source_adapter: "slurm_cli",
          source_operation: "sacct",
          captured_at: "2026-08-19T00:05:00Z",
          quality: "verified",
          coverage: 1,
          warning: null,
        },
        gpu_utilization: {
          value: null,
          unit: "percent",
          availability: "unsupported",
          source_adapter: "slurm_cli",
          source_operation: "sacct",
          captured_at: "2026-08-19T00:05:00Z",
          quality: "unavailable",
          coverage: null,
          warning: "GPU accounting is not configured.",
        },
      },
      evaluations: [],
    },
  },
] as const;

describe("lifecycle wire schemas", () => {
  for (const testCase of CASES) {
    it(`accepts the closed ${testCase.name} golden payload`, async () => {
      const schema = JSON.parse(await readFile(new URL(testCase.name, testCase.root), "utf8"));

      expect(schema.$schema).toBe("https://json-schema.org/draft/2020-12/schema");
      expect(Value.Check(schema, testCase.payload)).toBe(true);
      expect(Value.Check(schema, { ...testCase.payload, authorization: "Bearer secret" })).toBe(
        false,
      );
    });
  }
});
