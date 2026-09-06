import { afterEach, describe, expect, it, vi } from "vitest";
import { workareaApi } from "./workarea-api";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("workarea binding removal client", () => {
  it("encodes path-valued binding targets and uses DELETE", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    globalThis.fetch = fetchMock as typeof fetch;

    await workareaApi.removeBinding("alice", "wa-1", {
      kind: "asset",
      target_ref: "/home/alice/project/code",
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe(
      "/api/v1/workareas/wa-1/bindings/asset/%2Fhome%2Falice%2Fproject%2Fcode",
    );
    expect(init.method).toBe("DELETE");
    expect((init.headers as Record<string, string>)["X-Pilot107-User"]).toBe("alice");
  });
});
