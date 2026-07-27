import type { RunState } from "./types";

export type RunTone = "neutral" | "info" | "success" | "warning" | "danger";

export function runTone(state: RunState): RunTone {
  if (state === "SUCCEEDED") return "success";
  if (["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "AUTH_REQUIRED"].includes(state)) {
    return "danger";
  }
  if (["CANCELLED", "UNKNOWN", "SUBMISSION_UNCERTAIN", "EVIDENCE_PARTIAL", "ORPHANED"].includes(state)) {
    return "warning";
  }
  if (["SUBMITTING", "SUBMITTED", "PENDING", "RUNNING", "COMPLETING"].includes(state)) {
    return "info";
  }
  return "neutral";
}

export function runStateLabel(state: RunState): string {
  const labels: Partial<Record<RunState, string>> = {
    VALIDATED: "待提交",
    SUBMITTING: "提交中",
    SUBMITTED: "已提交",
    PENDING: "排队中",
    RUNNING: "运行中",
    COMPLETING: "收尾中",
    SUCCEEDED: "已成功",
    FAILED: "已失败",
    CANCELLED: "已取消",
    UNKNOWN: "状态未知",
    SUBMIT_FAILED: "提交失败",
    COLLECTION_FAILED: "采集失败",
    AUTH_REQUIRED: "需要认证",
    SUBMISSION_UNCERTAIN: "提交待确认",
    EVIDENCE_PARTIAL: "证据不完整",
    ORPHANED: "后端作业待接管",
  };
  return labels[state] ?? state;
}
