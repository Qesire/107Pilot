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
