import { describe, expect, it } from "vitest";
import { Value } from "typebox/value";

import { createProjectTools } from "../src/project-tools.js";
import {
  A2_PROJECT_TOOL_NAMES,
  parseDurableTurnRequest,
  parseToolInvocation,
  type ToolResult,
} from "../src/protocol.js";
import { durableRequest } from "./support/fixtures.js";

function builderRequest() {
  return parseDurableTurnRequest({
    ...durableRequest(),
    task_kind: "experiment_builder",
    prompt_profile_id: "experiment_builder",
    toolset_id: "a2-project",
  });
}

function repairRequest() {
  return parseDurableTurnRequest({
    ...durableRequest(),
    task_kind: "run_diagnosis_repair",
    prompt_profile_id: "run_diagnosis_repair",
    toolset_id: "a2-project",
  });
}

const HEAT_BLUEPRINT = {
  goal: "Verify second-order convergence for a 2D heat equation solver.",
  entrypoints: ["scripts/run_experiment.sh"],
  files: [
    { path: "src/heat2d.c", purpose: "OpenMP solver", classification: "editable" },
    { path: "scripts/run_experiment.sh", purpose: "Slurm entrypoint", classification: "editable" },
  ],
  validations: [
    {
      validation_id: "static-project-check",
      execution: "sandbox",
      argv: ["python3", "scripts/validate_project.py"],
      expected_outputs: [],
    },
    {
      validation_id: "heat-slurm-validation",
      execution: "slurm",
      argv: ["bash", "scripts/run_experiment.sh"],
      expected_outputs: ["convergence.json", "scaling.json", "report.md"],
    },
  ],
  contract_intent: {
    recipe_version_id: "recipe_python_cpu@1.0.0",
    resource_hints: { cpus_per_task: 4, gpus: 0, time_limit: "00:10:00" },
  },
  expected_outputs: [
    { path: "convergence.json", kind: "json", required: true },
    { path: "report.md", kind: "file", required: true },
  ],
  dependencies: [
    { name: "gcc", version: ">=12", source: "system" },
  ],
  open_questions: [],
};

describe("A2 Project tools", () => {
  it("accepts only the experiment_builder pairing and tool names", () => {
    const request = builderRequest();
    expect(request.task_kind).toBe("experiment_builder");
    expect(() => parseDurableTurnRequest({ ...request, toolset_id: "a1-readonly" }))
      .toThrow(/pairing/i);
    expect(() => parseToolInvocation({
      schema_version: "pilot107.agent-tool-invocation/v1",
      invocation_id: "inv-1",
      idempotency_key: "idem-1",
      owner: request.owner,
      session_id: request.session_id,
      turn_id: request.turn_id,
      state_version: request.state_version,
      profile_id: "experiment_builder",
      tool_name: "run_get",
      arguments: { run_id: "run-1" },
      deadline: "2026-08-19T00:00:20Z",
    })).toThrow(/pairing/i);
  });

  it("registers the closed, sequential Project tool set", () => {
    const tools = createProjectTools(builderRequest(), {
      invoke: async () => successResult(),
    });
    expect(tools.map((tool) => tool.name)).toEqual([...A2_PROJECT_TOOL_NAMES]);
    expect(tools.every((tool) => tool.executionMode === "sequential")).toBe(true);
    const patch = tools.find((tool) => tool.name === "workspace_patch");
    if (patch === undefined) throw new Error("missing workspace_patch");
    const valid = {
      project_id: "project-1",
      workspace_id: "workspace-1",
      patches: [{
        path: "main.py",
        expected_source_digest: null,
        operation: "create",
        content: "print(1)\n",
      }],
    };
    expect(Value.Check(patch.parameters, valid)).toBe(true);
    expect(Value.Check(patch.parameters, { ...valid, shell: "rm -rf" })).toBe(false);
    expect(Value.Check(patch.parameters, {
      ...valid,
      patches: [{ ...valid.patches[0], shell: "rm -rf" }],
    })).toBe(false);
  });

  it("exposes a closed, typed Blueprint save tool", () => {
    const tools = createProjectTools(builderRequest(), {
      invoke: async () => successResult(),
    });
    const save = tools.find((tool) => tool.name === "project_blueprint_save");
    if (save === undefined) throw new Error("missing project_blueprint_save");
    const valid = {
      project_id: "project-1",
      workspace_id: "workspace-1",
      expected_version: 1,
      blueprint: HEAT_BLUEPRINT,
    };

    expect(Value.Check(save.parameters, valid)).toBe(true);
    expect(Value.Check(save.parameters, { ...valid, project_id: "project-2" })).toBe(true);
    expect(Value.Check(save.parameters, { ...valid, extra: true })).toBe(false);
    expect(Value.Check(save.parameters, {
      ...valid,
      blueprint: { ...HEAT_BLUEPRINT, entrypoints: ["/tmp/run.sh"] },
    })).toBe(false);
    expect(Value.Check(save.parameters, {
      ...valid,
      blueprint: {
        ...HEAT_BLUEPRINT,
        contract_intent: {
          ...HEAT_BLUEPRINT.contract_intent,
          resource_hints: { nodes: 2 },
        },
      },
    })).toBe(false);
    expect(Value.Check(save.parameters, {
      ...valid,
      blueprint: { ...HEAT_BLUEPRINT, unknown: true },
    })).toBe(false);
  });

  it("gives the repair profile the same closed Project tools", () => {
    const request = repairRequest();
    expect(request.task_kind).toBe("run_diagnosis_repair");
    expect(request.prompt_profile_id).toBe("run_diagnosis_repair");
    expect(createProjectTools(request, {
      invoke: async () => successResult(),
    }).map((tool) => tool.name)).toEqual([...A2_PROJECT_TOOL_NAMES]);
  });

  it.each(["market_application", "template_publication"] as const)(
    "gives the %s profile the same closed Project tools",
    (profile) => {
      const request = parseDurableTurnRequest({
        ...durableRequest(),
        task_kind: profile,
        prompt_profile_id: profile,
        toolset_id: "a2-project",
      });
      expect(createProjectTools(request, {
        invoke: async () => successResult(),
      }).map((tool) => tool.name)).toEqual([...A2_PROJECT_TOOL_NAMES]);
    },
  );

  it("binds validation to the authoritative Turn and terminates immediately", async () => {
    const request = builderRequest();
    let forwarded: Record<string, unknown> | undefined;
    const tools = createProjectTools(request, {
      invoke: async (_request, _callId, _name, arguments_) => {
        forwarded = arguments_;
        return successResult();
      },
    });
    const validation = tools.find((tool) => tool.name === "validation_schedule");
    if (validation === undefined) throw new Error("missing validation_schedule");

    const result = await validation.execute("call-validation-1", {
      project_id: "project-1",
      workspace_id: "workspace-1",
      request_key: "validation-1",
      cpus: 1,
      memory_mib: 1024,
      gpus: 0,
      walltime_seconds: 300,
      tasks: 1,
      submissions: 1,
      script: "true\n",
      job_name: "validation",
    });

    expect(forwarded).toMatchObject({
      session_id: request.session_id,
      turn_id: request.turn_id,
    });
    expect(result.terminate).toBe(true);
  });

  it("returns a readable, sanitized structured Project tool failure", async () => {
    const tools = createProjectTools(builderRequest(), {
      invoke: async () => ({
        schema_version: "pilot107.agent-tool-result/v1",
        invocation_id: "inv-private",
        result: null,
        error: {
          code: "workspace_not_bound",
          message: "No Workspace is bound.",
          retryable: false,
          stack: "private stack",
          authorization: "Bearer private-secret",
        },
        evidence_refs: [],
        bytes_returned: 0,
      }),
    });
    const tool = tools.find((candidate) => candidate.name === "project_get");
    if (tool === undefined) throw new Error("missing project_get");

    const result = await tool.execute("call-project", {
      project_id: "project-1",
      workspace_id: "workspace-1",
    });

    expect(result).toEqual({
      content: [{ type: "text", text: "No Workspace is bound." }],
      details: {
        error: {
          code: "workspace_not_bound",
          message: "No Workspace is bound.",
          retryable: false,
        },
      },
    });
    expect(JSON.stringify(result)).not.toContain("private");
  });
});

function successResult(): ToolResult {
  return {
    schema_version: "pilot107.agent-tool-result/v1",
    invocation_id: "inv-1",
    result: { ok: true },
    error: null,
    evidence_refs: [],
    bytes_returned: 11,
  };
}
