import type { AgentMessage } from "@earendil-works/pi-agent-core";
import {
  fauxAssistantMessage,
  fauxText,
  fauxThinking,
  fauxToolCall,
  type ToolResultMessage,
} from "@earendil-works/pi-ai";
import { describe, expect, it } from "vitest";
import { Value } from "typebox/value";

import {
  checkpointFromState,
  computeCheckpointDigest,
  restoreMessages,
} from "../src/checkpoint.js";
import { AgentCheckpointSchema, type AgentCheckpoint } from "../src/protocol.js";
import { interactiveRequest } from "./support/fixtures.js";

function stateWithThinkingToolsAndSecrets(): { messages: AgentMessage[] } {
  const assistant = fauxAssistantMessage(
    [
      fauxThinking("private reasoning secret"),
      fauxText("public answer https://gateway.example/v1?api_key=query-secret"),
      fauxToolCall("emit_result", {
        answer: "safe",
        api_key: "argument-secret",
      }, { id: "call-1" }),
    ],
    { timestamp: 2 },
  );
  const toolResult: ToolResultMessage = {
    role: "toolResult",
    toolCallId: "call-1",
    toolName: "emit_result",
    content: [{ type: "text", text: "accepted; token=result-secret" }],
    details: {
      accepted: true,
      Authorization: "Bearer header-secret",
      nested: {
        workflow: "normal non-secret value",
        pass_word: "not-a-secret-key-name",
        password: "password-secret",
      },
    },
    isError: false,
    timestamp: 3,
  };
  return {
    messages: [
      {
        role: "user",
        content:
          "Explain token budgeting, then use Bearer user-secret at https://gateway.example/v1?token=url-secret",
        timestamp: 1,
      },
      assistant,
      toolResult,
    ],
  };
}

function resign(checkpoint: AgentCheckpoint): AgentCheckpoint {
  return {
    ...checkpoint,
    digest: computeCheckpointDigest(checkpoint),
  };
}

describe("safe checkpoint creation", () => {
  it("preserves public transcript and completed tools but removes secrets and thinking", () => {
    const checkpoint = checkpointFromState(
      interactiveRequest(),
      stateWithThinkingToolsAndSecrets(),
    );
    const serialized = JSON.stringify(checkpoint);

    expect(Value.Check(AgentCheckpointSchema, checkpoint)).toBe(true);
    expect(checkpoint.messages.map((message) => message.role)).toEqual([
      "user",
      "assistant",
      "tool_result",
    ]);
    expect(checkpoint.messages[0]?.content).toContain("Explain token budgeting");
    expect(checkpoint.messages[1]?.content).toBe("public answer https://gateway.example/v1");
    expect(checkpoint.completed_tools).toEqual([
      {
        tool_call_id: "call-1",
        tool_name: "emit_result",
        arguments: { answer: "safe" },
        result: {
          accepted: true,
          nested: {
            workflow: "normal non-secret value",
            pass_word: "not-a-secret-key-name",
          },
        },
        is_error: false,
      },
    ]);
    expect(serialized).not.toContain("private reasoning secret");
    expect(serialized).not.toContain("user-secret");
    expect(serialized).not.toContain("query-secret");
    expect(serialized).not.toContain("argument-secret");
    expect(serialized).not.toContain("result-secret");
    expect(serialized).not.toContain("header-secret");
    expect(serialized).not.toContain("password-secret");
    expect(checkpoint.digest).toMatch(/^[a-f0-9]{64}$/);
  });

  it("uses canonical recursive key ordering for its digest", () => {
    const checkpoint = checkpointFromState(interactiveRequest(), { messages: [] });
    const reordered = {
      digest: checkpoint.digest,
      usage: {
        cache_write_tokens: checkpoint.usage.cache_write_tokens,
        output_tokens: checkpoint.usage.output_tokens,
        cache_read_tokens: checkpoint.usage.cache_read_tokens,
        input_tokens: checkpoint.usage.input_tokens,
      },
      completed_tools: checkpoint.completed_tools,
      messages: checkpoint.messages,
      prompt_profile_id: checkpoint.prompt_profile_id,
      model_profile_id: checkpoint.model_profile_id,
      lineage: checkpoint.lineage,
      turn_id: checkpoint.turn_id,
      schema_version: checkpoint.schema_version,
    } as AgentCheckpoint;

    expect(computeCheckpointDigest(reordered)).toBe(checkpoint.digest);
  });

  it.each([
    [
      "orphan tool result",
      [
        {
          role: "toolResult",
          toolCallId: "call-orphan",
          toolName: "emit_result",
          content: [{ type: "text", text: "accepted" }],
          isError: false,
          timestamp: 2,
        },
      ],
    ],
    [
      "mismatched tool result",
      [
        fauxAssistantMessage(
          [fauxToolCall("emit_result", { answer: "safe" }, { id: "call-1" })],
          { timestamp: 2 },
        ),
        {
          role: "toolResult",
          toolCallId: "call-1",
          toolName: "different_tool",
          content: [{ type: "text", text: "accepted" }],
          isError: false,
          timestamp: 3,
        },
      ],
    ],
  ] as const)("fails closed on a %s", (_label, messages) => {
    expect(() =>
      checkpointFromState(interactiveRequest(), {
        messages: messages as AgentMessage[],
      }),
    ).toThrow("tool");
  });

  it("drops an unfinished tool call so cancellation can still checkpoint", () => {
    const checkpoint = checkpointFromState(interactiveRequest(), {
      messages: [
        fauxAssistantMessage(
          [
            fauxText("public partial answer"),
            fauxToolCall("emit_result", { answer: "safe" }, { id: "call-open" }),
          ],
          { timestamp: 2 },
        ),
      ],
    });

    expect(checkpoint.completed_tools).toEqual([]);
    expect(checkpoint.messages[0]?.content).toBe("public partial answer");
    expect(() => restoreMessages(checkpoint, interactiveRequest())).not.toThrow();
  });
});

describe("checkpoint restoration", () => {
  it("verifies the digest and rejects transcript tampering", () => {
    const request = interactiveRequest();
    const checkpoint = checkpointFromState(request, stateWithThinkingToolsAndSecrets());
    const tampered = structuredClone(checkpoint);
    tampered.messages[0]!.content = "tampered";

    expect(() => restoreMessages(tampered, request)).toThrow("digest");
  });

  it.each([
    ["model profile", { model_profile_id: "campus-default" }],
    ["prompt profile", { prompt_profile_id: "agent-explain-v1" }],
  ])("rejects a %s mismatch", (_label, requestPatch) => {
    const request = interactiveRequest();
    const checkpoint = checkpointFromState(request, { messages: [] });

    expect(() =>
      restoreMessages(checkpoint, { ...request, ...requestPatch }),
    ).toThrow("does not match");
  });

  it("rejects invalid lineage even when the digest is valid", () => {
    const request = interactiveRequest();
    const checkpoint = checkpointFromState(request, { messages: [] });
    const invalid = resign({
      ...checkpoint,
      lineage: [checkpoint.turn_id],
    });

    expect(() => restoreMessages(invalid, request)).toThrow("lineage");
  });

  it("enforces a serialized byte limit before restoration", () => {
    const request = interactiveRequest();
    const checkpoint = checkpointFromState(request, { messages: [] });

    expect(() => restoreMessages(checkpoint, request, { maxBytes: 32 })).toThrow(
      "size limit",
    );
  });

  it("maps normalized tool_result records back to Pi toolResult messages", () => {
    const request = interactiveRequest();
    const checkpoint = checkpointFromState(request, stateWithThinkingToolsAndSecrets());
    const restored = restoreMessages(checkpoint, request);
    const toolCallMessageIndex = restored.findIndex(
      (message) =>
        message.role === "assistant" &&
        message.content.some(
          (content) => content.type === "toolCall" && content.id === "call-1",
        ),
    );
    const toolResultIndex = restored.findIndex(
      (message) => message.role === "toolResult" && message.toolCallId === "call-1",
    );
    const toolResult = restored.find((message) => message.role === "toolResult");

    expect(toolCallMessageIndex).toBeGreaterThanOrEqual(0);
    expect(toolResultIndex).toBe(toolCallMessageIndex + 1);
    expect(toolResult).toMatchObject({
      role: "toolResult",
      toolCallId: "call-1",
      toolName: "emit_result",
      isError: false,
      details: {
        accepted: true,
        nested: {
          workflow: "normal non-secret value",
          pass_word: "not-a-secret-key-name",
        },
      },
    });
    expect(JSON.stringify(restored)).not.toContain("private reasoning secret");
  });
});
