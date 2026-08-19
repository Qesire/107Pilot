import { describe, expect, it } from "vitest";
import {
  agentPageMode,
  defaultProvider,
  llmConfiguredFromHealth,
  proposalPatchRows,
  providerLabel,
  remediationStateLabel,
  remediationStateTone,
  sessionProviderValue,
} from "./AgentPage";
import type { HealthReady, RemediationState } from "./types";

describe("Agent remediation state presentation", () => {
  it.each<[RemediationState, string, string]>([
    ["waiting_evidence", "等待证据", "info"],
    ["diagnosing", "诊断中", "info"],
    ["planning", "规划中", "info"],
    ["awaiting_input", "等待输入", "warning"],
    ["awaiting_approval", "等待审批", "warning"],
    ["ready", "已批准", "info"],
    ["preparing", "准备执行", "info"],
    ["executing", "执行中", "info"],
    ["evaluating", "评价中", "info"],
    ["succeeded", "已验证成功", "success"],
    ["exhausted", "预算耗尽", "danger"],
    ["blocked", "需要接管", "warning"],
    ["failed", "会话失败", "danger"],
    ["cancelled", "已取消", "neutral"],
  ])("maps %s to %s/%s", (state, label, tone) => {
    expect(remediationStateLabel(state)).toBe(label);
    expect(remediationStateTone(state)).toBe(tone);
  });
});

describe("Agent page mode", () => {
  it("defaults to durable conversations and selects builder or repair explicitly", () => {
    expect(agentPageMode(new URLSearchParams())).toBe("conversation");
    expect(agentPageMode(new URLSearchParams("mode=conversation"))).toBe("conversation");
    expect(agentPageMode(new URLSearchParams("mode=repair"))).toBe("repair");
    expect(agentPageMode(new URLSearchParams("mode=builder"))).toBe("builder");
    expect(agentPageMode(new URLSearchParams("mode=unknown"))).toBe("conversation");
  });
});

describe("Agent proposal diff", () => {
  it("extracts deterministic rule patches and marks unresolved values", () => {
    expect(proposalPatchRows({
      proposed_patch: {
        "resources.partition": "Students",
        "entry.command": null,
      },
    })).toEqual([
      { field: "entry.command", value: "需要输入" },
      { field: "resources.partition", value: "Students" },
    ]);
  });

  it("extracts model proposal patch parameters without treating other fields as diffs", () => {
    expect(proposalPatchRows({
      parameters: { patch: { "resources.qos": "qos_stu_default" } },
      rationale: "bounded",
    })).toEqual([{ field: "resources.qos", value: "qos_stu_default" }]);
    expect(proposalPatchRows({ parameters: { probe_kind: "cuda" } })).toEqual([]);
  });
});

describe("LLM provider selection", () => {
  it("defaults to local when LLM is configured", () => {
    expect(defaultProvider({ llmConfigured: true })).toBe("local");
  });

  it("defaults to none when LLM is unconfigured", () => {
    expect(defaultProvider({ llmConfigured: false })).toBe("none");
  });

  it("labels providers in Chinese", () => {
    expect(providerLabel("local")).toBe("USTC LLM (glm-5.2-107)");
    expect(providerLabel("none")).toBe("确定性规则（无 LLM）");
  });
});

describe("llmConfiguredFromHealth", () => {
  it("returns true when the local_llm check is configured", () => {
    const health: HealthReady = {
      status: "ready",
      checks: { local_llm: { status: "configured" } },
    };
    expect(llmConfiguredFromHealth(health)).toBe(true);
  });

  it("accepts the array shape emitted by the current readiness endpoint", () => {
    const health: HealthReady = {
      status: "ready",
      checks: [{ name: "local_llm", status: "configured", required: false }],
    };
    expect(llmConfiguredFromHealth(health)).toBe(true);
  });

  it("returns false when the local_llm check is disabled", () => {
    const health: HealthReady = {
      status: "ready",
      checks: { local_llm: { status: "disabled" } },
    };
    expect(llmConfiguredFromHealth(health)).toBe(false);
  });

  it("returns false when health has not loaded yet", () => {
    expect(llmConfiguredFromHealth(undefined)).toBe(false);
  });

  it("returns false when the local_llm check is missing", () => {
    const health: HealthReady = { status: "ready", checks: {} };
    expect(llmConfiguredFromHealth(health)).toBe(false);
  });
});

describe("sessionProviderValue", () => {
  it("passes through known provider values", () => {
    expect(sessionProviderValue("local", "none")).toBe("local");
    expect(sessionProviderValue("none", "local")).toBe("none");
  });

  it("falls back when the persisted provider is missing or unknown", () => {
    expect(sessionProviderValue(undefined, "local")).toBe("local");
    expect(sessionProviderValue("", "none")).toBe("none");
    expect(sessionProviderValue("rule", "local")).toBe("local");
  });
});
