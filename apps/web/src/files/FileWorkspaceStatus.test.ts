import { describe, expect, it } from "vitest";
import type { UploadSession } from "../types";
import { uploadProgress, uploadStateLabel } from "./FileWorkspaceStatus";

function session(overrides: Partial<UploadSession> = {}): UploadSession {
  return {
    upload_id: "upload-1",
    owner: "alice",
    target_path: "/public/home/alice",
    filename: "dataset.tar.gz",
    total_size: 100,
    is_partial: false,
    received_bytes: 25,
    sha256_expected: null,
    sha256_actual: null,
    state: "uploading",
    auto_extract: false,
    created_at: "2026-09-03T00:00:00Z",
    written_path: null,
    extracted_members: null,
    error: null,
    ...overrides,
  };
}

describe("FileWorkspaceStatus backend mapping", () => {
  it("uses the server-reported byte count for progress", () => {
    expect(uploadProgress(session())).toBe(25);
    expect(uploadProgress(session({ received_bytes: 500 }))).toBe(100);
  });

  it("treats zero-byte written sessions as complete", () => {
    expect(uploadProgress(session({ total_size: 0, received_bytes: 0, state: "written" }))).toBe(100);
    expect(uploadProgress(session({ total_size: 0, received_bytes: 0, state: "initialized" }))).toBe(0);
  });

  it("keeps backend states explicit in Chinese", () => {
    expect(uploadStateLabel("initialized")).toBe("等待上传");
    expect(uploadStateLabel("completing")).toBe("校验写入中");
    expect(uploadStateLabel("written")).toBe("已写入");
    expect(uploadStateLabel("failed")).toBe("失败");
  });
});
