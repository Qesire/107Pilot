import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RunResourceFacts, formatObservedMeasure } from "./RunResourcePanel";
import type { ObservedMeasure, RunResources } from "./types";

const measured = (
  value: number | string | null,
  unit: string,
  availability: ObservedMeasure["availability"] = "available",
): ObservedMeasure => ({
  value,
  unit,
  availability,
  source_adapter: "slurm_cli",
  source_operation: "sacct",
  captured_at: "2026-08-19T00:00:00Z",
  quality: value === null ? "unavailable" : "verified",
  coverage: value === null ? null : 1,
  warning: value === null ? "field unavailable" : null,
});

describe("RunResourcePanel", () => {
  it("formats units and keeps unavailable distinct from zero", () => {
    expect(formatObservedMeasure(measured(1_073_741_824, "bytes"))).toBe("1.00 GiB");
    expect(formatObservedMeasure(measured(0, "ratio"))).toBe("0.0%");
    expect(formatObservedMeasure(measured(null, "ratio", "unsupported"))).toBe("不支持");
    expect(formatObservedMeasure(measured(null, "bytes", "not_collected"))).toBe(
      "未采集",
    );
  });

  it("renders allocation pairs, provenance, freshness, and cautious evaluations", () => {
    const data: RunResources = {
      observation_id: "summary-run1",
      kind: "run_resource_summary",
      connection_id: "connection1",
      owner: "alice",
      run_id: "run1",
      attempt: 0,
      cycle_id: "cycle1",
      captured_at: "2026-08-19T00:00:00Z",
      freshness: "terminal",
      partial: false,
      warnings: [],
      used: {
        max_rss: measured(1_073_741_824, "bytes"),
        total_cpu: measured(120, "seconds"),
        gpu_utilization: measured(null, "ratio", "unsupported"),
      },
      allocated: {
        allocated_memory: measured(17_179_869_184, "bytes"),
        allocated_cpus: measured(4, "cpu"),
      },
      evaluations: [
        {
          evaluation_id: "evaluation1",
          rule_id: "CPU_UNDERUTILIZED",
          severity: "warning",
          confidence: "high",
          summary: "检查并行度后再考虑减少 CPU。",
          evidence_refs: ["resource-summary:summary-run1"],
          suggested_contract_patch: { "resources.cpus_per_task": 1 },
        },
      ],
    };

    const html = renderToStaticMarkup(<RunResourceFacts data={data} />);

    expect(html).toContain("资源账本");
    expect(html).toContain("1.00 GiB");
    expect(html).toContain("16.00 GiB");
    expect(html).toContain("slurm_cli · sacct");
    expect(html).toContain("不支持");
    expect(html).toContain("CPU_UNDERUTILIZED");
    expect(html).toContain("建议仅为提案");
  });
});
