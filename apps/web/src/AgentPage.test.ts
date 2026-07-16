import { describe, expect, it } from "vitest";
import { remediationStateLabel, remediationStateTone } from "./AgentPage";
import type { RemediationState } from "./types";

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
