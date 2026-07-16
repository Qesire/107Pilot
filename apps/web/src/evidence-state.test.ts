import { describe, expect, it } from "vitest";
import {
  formatBytes,
  parseJsonObject,
  previewContent,
  selectActiveEvidenceObject,
} from "./evidence-state";
import type { EvidenceObject, EvidenceObjectPreview } from "./types";

const stdout = object("ev_stdout", "logs", "logs/stdout.tail.json");
const stderr = object("ev_stderr", "logs", "logs/stderr.tail.json");
const output = object("ev_output", "outputs", "outputs/result.txt");

describe("Evidence view state", () => {
  it("uses stdout as the deterministic log default and honors a requested stderr", () => {
    const objects = [stderr, output, stdout];

    expect(selectActiveEvidenceObject(objects, "logs", null)?.object_id).toBe("ev_stdout");
    expect(selectActiveEvidenceObject(objects, "logs", "ev_stderr")?.object_id).toBe("ev_stderr");
  });

  it("does not carry a requested object across incompatible tabs", () => {
    expect(selectActiveEvidenceObject([stdout, output], "results", "ev_stdout")).toBeNull();
    expect(selectActiveEvidenceObject([stdout, output], "results", "ev_output")).toBe(output);
    expect(selectActiveEvidenceObject([stdout], "overview", "ev_stdout")).toBeNull();
  });

  it("extracts the bounded log tail but preserves raw Evidence content", () => {
    const preview = {
      ...stdout,
      preview: {
        available: true,
        content: JSON.stringify({ stream: "stdout", tail: "line one\nline two\n" }),
        max_bytes: 131072,
      },
    } satisfies EvidenceObjectPreview;

    expect(previewContent(preview, "log")).toBe("line one\nline two\n");
    expect(previewContent(preview, "raw")).toContain('"tail"');
    expect(parseJsonObject("not-json")).toBeNull();
  });

  it("formats exact Evidence byte sizes", () => {
    expect(formatBytes(53)).toBe("53 B");
    expect(formatBytes(1536)).toBe("1.5 KiB");
  });
});

function object(objectId: string, category: string, logicalPath: string): EvidenceObject {
  return {
    object_id: objectId,
    category,
    logical_path: logicalPath,
    source_uri: `evidence://runs/run_test/${logicalPath}`,
    sha256: "a".repeat(64),
    size_bytes: 12,
    mime_type: "application/json",
    collection_status: "collected",
    mutable_during_run: false,
    finalized_at: "2026-07-16T00:00:00Z",
  };
}
