import { describe, expect, it } from "vitest";
import { Value } from "typebox/value";

import { createProjectTools } from "../src/project-tools.js";
import {
  A2_PROJECT_TOOL_NAMES,
  BUILDER_WORKFLOW_TOOL_NAMES,
  PROJECT_WORKSPACE_TOOL_NAMES,
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
    input: {
      message: "build the experiment",
      context_refs: ["project:project-1", "workspace:workspace-1"],
    },
  });
}

function repairRequest() {
  return parseDurableTurnRequest({
    ...durableRequest(),
    task_kind: "run_diagnosis_repair",
    prompt_profile_id: "run_diagnosis_repair",
    toolset_id: "a2-project",
    input: {
      message: "repair the experiment",
      context_refs: ["project:project-1", "workspace:workspace-1"],
    },
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
  it("exposes only the closed phase-aware Builder facade when enabled", async () => {
    const request = builderRequest();
    const forwarded: Array<{ name: string; arguments: Record<string, unknown> }> = [];
    const tools = createProjectTools(request, {
      invoke: async (_request, _callId, name, arguments_) => {
        forwarded.push({ name, arguments: arguments_ });
        return successResult(
          name === "builder_build_submit"
            ? { status: "repair_required", phase: "sandbox_failed" }
            : { phase: "drafting" },
        );
      },
    }, { phaseAwareBuilder: true });

    expect(tools.map((tool) => tool.name)).toEqual([
      ...BUILDER_WORKFLOW_TOOL_NAMES,
    ]);
    const context = tools[0];
    const submit = tools[1];
    if (context === undefined || submit === undefined) {
      throw new Error("missing Builder facade tools");
    }
    expect(Value.Check(context.parameters, {})).toBe(true);
    expect(Value.Check(context.parameters, { project_id: "project-2" })).toBe(false);
    const valid = {
      request_key: "build-1",
      approval_summary_zh: "创建热扩散实验，并在沙箱通过后提交受限验证。",
      expected_project_version: 1,
      expected_workspace_snapshot_digest: "a".repeat(64),
      base_change_set_id: null,
      blueprint: HEAT_BLUEPRINT,
      patches: [{
        path: "src/heat2d.c",
        expected_source_digest: null,
        operation: "create",
        content: "int main(void) { return 0; }\n",
      }],
    };
    expect(Value.Check(submit.parameters, valid)).toBe(true);
    expect(Value.Check(submit.parameters, {
      ...valid,
      blueprint: {
        ...HEAT_BLUEPRINT,
        validations: HEAT_BLUEPRINT.validations.filter(
          (validation) => validation.execution === "slurm",
        ),
      },
    })).toBe(false);
    expect(Value.Check(submit.parameters, { ...valid, cpus: 4 })).toBe(false);

    await context.execute("call-context", {});
    const repair = await submit.execute("call-submit", valid);
    expect(forwarded[0]).toMatchObject({
      name: "builder_context_get",
      arguments: {
        project_id: "project-1",
        workspace_id: "workspace-1",
        session_id: request.session_id,
      },
    });
    expect(forwarded[1]).toMatchObject({
      name: "builder_build_submit",
      arguments: {
        project_id: "project-1",
        workspace_id: "workspace-1",
        session_id: request.session_id,
        turn_id: request.turn_id,
      },
    });
    expect(repair.terminate).toBe(false);
  });

  it("terminates the facade only after validation is scheduled", async () => {
    const [context, submit] = createProjectTools(builderRequest(), {
      invoke: async (_request, _callId, name) => successResult(
        name === "builder_build_submit"
          ? { status: "scheduled", phase: "validation_scheduled" }
          : { phase: "drafting" },
      ),
    }, { phaseAwareBuilder: true });
    if (context === undefined || submit === undefined) {
      throw new Error("missing Builder facade tools");
    }

    expect((await context.execute("call-context", {})).terminate).toBe(false);
    expect((await submit.execute("call-submit", {
      request_key: "build-1",
      approval_summary_zh: "创建热扩散实验，并在沙箱通过后提交受限验证。",
      expected_project_version: 1,
      expected_workspace_snapshot_digest: "a".repeat(64),
      base_change_set_id: null,
      blueprint: HEAT_BLUEPRINT,
      patches: [{
        path: "main.py",
        expected_source_digest: null,
        operation: "create",
        content: "print(1)\n",
      }],
    })).terminate).toBe(true);
  });

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
      approval_summary_zh: "创建 main.py，供用户审阅。",
      patches: [{
        path: "main.py",
        expected_source_digest: null,
        operation: "create",
        content: "print(1)\n",
      }],
    };
    expect(Value.Check(patch.parameters, valid)).toBe(true);
    expect(Value.Check(patch.parameters, { ...valid, project_id: "project-2" })).toBe(false);
    expect(Value.Check(patch.parameters, { ...valid, workspace_id: "workspace-2" })).toBe(false);
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
      expected_version: 1,
      blueprint: HEAT_BLUEPRINT,
    };

    expect(Value.Check(save.parameters, valid)).toBe(true);
    expect(Value.Check(save.parameters, { ...valid, project_id: "project-2" })).toBe(false);
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

  it("gives the repair profile only bounded Workspace editing tools", () => {
    const request = repairRequest();
    expect(request.task_kind).toBe("run_diagnosis_repair");
    expect(request.prompt_profile_id).toBe("run_diagnosis_repair");
    expect(createProjectTools(request, {
      invoke: async () => successResult(),
    }).map((tool) => tool.name)).toEqual([...PROJECT_WORKSPACE_TOOL_NAMES]);
  });

  it.each(["market_application", "template_publication"] as const)(
    "gives the %s profile only bounded Workspace editing tools",
    (profile) => {
      const request = parseDurableTurnRequest({
        ...durableRequest(),
        task_kind: profile,
        prompt_profile_id: profile,
        toolset_id: "a2-project",
      });
      expect(createProjectTools(request, {
        invoke: async () => successResult(),
      }).map((tool) => tool.name)).toEqual([...PROJECT_WORKSPACE_TOOL_NAMES]);
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
      project_id: "project-1",
      workspace_id: "workspace-1",
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

    const result = await tool.execute("call-project", {});

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

function successResult(result: Record<string, unknown> = { ok: true }): ToolResult {
  return {
    schema_version: "pilot107.agent-tool-result/v1",
    invocation_id: "inv-1",
    result,
    error: null,
    evidence_refs: [],
    bytes_returned: 11,
  };
}
