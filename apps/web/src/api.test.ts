import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiRequestError } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API transport", () => {
  it("encodes identifiers, filters, and trusted identity headers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], page: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await api.runs("alice", { state: "FAILED", q: "空 格", limit: "10" });
    await api.evidenceObject("alice", "run/a", "stderr #1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs?owner=alice&state=FAILED&q=%E7%A9%BA+%E6%A0%BC&limit=10",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/runs/run%2Fa/evidence/objects/stderr%20%231",
      expect.any(Object),
    );
  });

  it("passes opaque Run cursors without changing the authenticated scope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], page: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await api.runs("alice", { limit: "20", cursor: "opaque+/=" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs?owner=alice&limit=20&cursor=opaque%2B%2F%3D",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
  });

  it("sends JSON mutations with the fixed authenticated user", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ run_id: "run_1" }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await api.submitRun("alice", "run_1");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs/run_1/submit",
      expect.objectContaining({
        method: "POST",
        body: "{}",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          "X-Pilot107-User": "alice",
        }),
      }),
    );
  });

  it("surfaces stable API error codes and fallback HTTP codes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ error: { code: "AUTH.FORBIDDEN", message: "not yours" } }, 403),
      )
      .mockResolvedValueOnce(jsonResponse({}, 503, "Unavailable"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.run("bob", "run_alice")).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 403,
      code: "AUTH.FORBIDDEN",
      message: "not yours",
    } satisfies Partial<ApiRequestError>);
    await expect(api.health("alice")).rejects.toMatchObject({
      status: 503,
      code: "HTTP.503",
      message: "Unavailable",
    });
  });

  it("forwards cancellation signals", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "ready" }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await api.health("alice", controller.signal);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/health/ready",
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  it("keeps remediation identity and optimistic version in the request contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ session_id: "session_1" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.createRemediationSession("alice", "run/a");
    await api.approveRemediationAction("alice", "session/1", "proposal 1", 7);
    await api.takeoverRemediationSession("alice", "session/1", 8, "manual handoff");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs/run%2Fa/remediation-sessions",
      expect.objectContaining({
        body: JSON.stringify({
          request_key: "ui:run/a",
          automation_policy: "manual_approval",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/remediation-sessions/session%2F1/approve",
      expect.objectContaining({
        body: JSON.stringify({ proposal_id: "proposal 1", expected_version: 7 }),
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/v1/remediation-sessions/session%2F1/takeover",
      expect.objectContaining({
        body: JSON.stringify({ expected_version: 8, note: "manual handoff" }),
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
  });

  it("binds remediation list identity only through the trusted header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], page: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await api.remediationSessions("alice", "blocked");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/remediation-sessions?state=blocked",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
  });

  it("keeps Run lineage reads and retry mutations owner scoped", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], page: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await api.runEvents("alice", "run/1");
    await api.runLineage("alice", "run/1");
    await api.cancelRun("alice", "run/1");
    await api.prepareRetry("alice", {
      run_id: "run/1",
      contract_id: "contract/1",
      state: "FAILED",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs/run%2F1/events?limit=100",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/runs/run%2F1/lineage",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/v1/runs/run%2F1/cancel",
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/api/v1/runs/prepare",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          contract_id: "contract/1",
          parent_run_id: "run/1",
          lineage_reason: "manual_retry",
        }),
      }),
    );
  });
});

function jsonResponse(payload: unknown, status = 200, statusText = "OK"): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: async () => payload,
  } as Response;
}
