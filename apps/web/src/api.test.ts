import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiRequestError } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API transport", () => {
  it("reads and checks the current user's SSH platform connection", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({
        connection_id: "real107",
        state: "active",
      }));
    vi.stubGlobal("fetch", fetchMock);

    await api.platformConnections("alice");
    await api.checkPlatformConnection("alice", "real107");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/platform/connections",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/platform/connections/real107/check",
      expect.objectContaining({
        method: "POST",
        body: "{}",
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
  });

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

  it("uses the successful-Run market contract without sending private source fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], page: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await api.successfulRunMarket("bob", { q: "训练", visibility: "campus", tag: "ml" });
    await api.publishSuccessfulRun("alice", "run/a", {
      request_key: "publish-a",
      title: "训练作业",
      description: "仅供参考",
      visibility: "campus",
      tags: ["ml"],
      reproduction_note: "采用后替换自己的目录",
      confirm_share: true,
      share_manifest: {
        description: true,
        resource_summary: false,
        result_summary: true,
        contract_for_adaptation: false,
        script: false,
        evidence_previews: false,
        small_assets: [],
      },
    });
    await api.adoptSuccessfulRun("bob", "runpub/a", "adopt-a");
    await api.marketItems("bob", { kind: "run_publication", q: "训练" });
    await api.marketItem("bob", "runpub/a");
    await api.adoptMarketItem("bob", "runpub/a", "adopt-unified-a");
    await api.withdrawMarketItem("alice", "runpub/a", "superseded");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/market?q=%E8%AE%AD%E7%BB%83&visibility=campus&tag=ml&limit=20",
      expect.objectContaining({ method: "GET", headers: expect.objectContaining({ "X-Pilot107-User": "bob" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/runs/run%2Fa/publish",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          request_key: "publish-a",
          title: "训练作业",
          description: "仅供参考",
          visibility: "campus",
          tags: ["ml"],
          reproduction_note: "采用后替换自己的目录",
          confirm_share: true,
          share_manifest: {
            description: true,
            resource_summary: false,
            result_summary: true,
            contract_for_adaptation: false,
            script: false,
            evidence_previews: false,
            small_assets: [],
          },
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/v1/market/runpub%2Fa/adopt",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ request_key: "adopt-a" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/api/v1/market/items?kind=run_publication&q=%E8%AE%AD%E7%BB%83&limit=20",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "/api/v1/market/items/runpub%2Fa",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "/api/v1/market/items/runpub%2Fa/adopt",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ request_key: "adopt-unified-a" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      7,
      "/api/v1/market/items/runpub%2Fa/withdraw",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ reason: "superseded" }) }),
    );
  });

  it("uses typed market application and template publication sessions", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ session_id: "session-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.startMarketApplication("bob", {
      source_kind: "curated_template",
      source_item_id: "release/a",
      user_intent: "adapt privately",
      request_key: "start-application",
    });
    await api.confirmMarketApplication("bob", "session/1", {
      expected_version: 1,
      confirmation_digest: "a".repeat(64),
      request_key: "finish-application",
    });
    await api.startTemplatePublication("alice", "run/a", {
      request_key: "start-publication",
      title: "Reusable training",
      description: "Sanitized bundle",
      visibility: "campus",
      compatibility: { partitions: ["Students"], gpu: false },
      publication: { license: "MIT" },
    });
    await api.recordTemplateReproduction("alice", "publication/1", {
      expected_version: 1,
      evidence_ref: "evidence://runs/reproduction/manifest/manifest.json",
      evidence_digest: "b".repeat(64),
      environment: "docker",
      release_version: "1.0.0",
    });
    await api.confirmTemplatePublication("alice", "publication/1", {
      expected_version: 2,
      confirmation_digest: "c".repeat(64),
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/market/applications",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          source_kind: "curated_template",
          source_item_id: "release/a",
          user_intent: "adapt privately",
          request_key: "start-application",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/market/applications/session%2F1/confirmation",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/v1/runs/run%2Fa/template-publication-sessions",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/api/v1/template-publication-sessions/publication%2F1/responses",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "/api/v1/template-publication-sessions/publication%2F1/confirmation",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("surfaces stable API error codes and fallback HTTP codes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ error: { code: "AUTH.FORBIDDEN", message: "not yours" } }, 403),
      )
      .mockResolvedValueOnce(jsonResponse({}, 503, "Unavailable"))
      .mockResolvedValueOnce(jsonResponse({ error: { code: "TEMPLATE.FORBIDDEN" } }, 403));
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
    await expect(api.adoptTemplate("alice", "template_1", "1.0.0", "key_1")).rejects.toMatchObject({
      status: 403,
      code: "TEMPLATE.FORBIDDEN",
      message: "当前账号没有采用此模板 release 的权限。请返回模板市场选择可见的 release，或申请课程/发布权限。",
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

    await api.createRemediationSession("alice", "run/a", "ui:run/a:request-1");
    await api.approveRemediationAction("alice", "session/1", "proposal 1", 7);
    await api.takeoverRemediationSession("alice", "session/1", 8, "manual handoff");
    await api.startRemediationRepairProject("alice", "session/1", {
      proposal_id: "proposal 1",
      expected_version: 8,
      request_key: "ui:repair-project:1",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs/run%2Fa/remediation-sessions",
      expect.objectContaining({
        body: JSON.stringify({
          request_key: "ui:run/a:request-1",
          automation_policy: "manual_approval",
          provider: "local",
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
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/api/v1/remediation-sessions/session%2F1/repair-project",
      expect.objectContaining({
        body: JSON.stringify({
          proposal_id: "proposal 1",
          expected_version: 8,
          request_key: "ui:repair-project:1",
        }),
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

  it("uses the durable Agent Session contract and resumes by event id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], page: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await api.agentSessions("alice");
    await api.createAgentSession("alice", {
      profile: "campus-default",
      request_key: "ui:session:1",
    });
    await api.agentSession("alice", "session/1");
    await api.createAgentTurn("alice", "session/1", {
      message: "解释 run-1 为什么排队",
      request_key: "ui:turn:1",
      expected_state_version: 3,
    });
    await api.cancelAgentTurn("alice", "session/1", "turn/1", 4);
    await api.agentSessionEvents("alice", "session/1", 7);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/agent-sessions",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/agent-sessions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          request_key: "ui:session:1",
          model_profile_id: "campus-default",
          source: {},
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/v1/agent-sessions/session%2F1",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/api/v1/agent-sessions/session%2F1/turns",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          message: "解释 run-1 为什么排队",
          request_key: "ui:turn:1",
          expected_state_version: 3,
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "/api/v1/agent-sessions/session%2F1/turns/turn%2F1/cancel",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ expected_state_version: 4 }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "/api/v1/agent-sessions/session%2F1/events?after_event_id=7&limit=100",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("surfaces owner-scoped Agent Session misses without adding an owner override", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      error: { code: "AGENT.SESSION.NOT_FOUND", message: "Agent Session not found" },
    }, 404));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.agentSession("bob", "session-alice")).rejects.toMatchObject({
      status: 404,
      code: "AGENT.SESSION.NOT_FOUND",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/agent-sessions/session-alice",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-Pilot107-User": "bob" }),
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

describe("suggestContractPatch contract agent suggest", () => {
  it("posts current contract + intent + provider to the agent endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        suggested_patch: { "entry.command": "python3 train.py" },
        explanation_zh: "已将 command 改为训练入口。",
        needs_user_confirmation: true,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.suggestContractPatch(
      "alice",
      { schema_version: "pilot107.contract/v2" },
      "recipe_python_cpu@1.0.0",
      "我要跑一个 python 训练脚本",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/contracts/agent/suggest",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          current_contract: { schema_version: "pilot107.contract/v2" },
          recipe_version_id: "recipe_python_cpu@1.0.0",
          user_intent: "我要跑一个 python 训练脚本",
          provider: "local",
        }),
        headers: expect.objectContaining({ "X-Pilot107-User": "alice" }),
      }),
    );
    expect(result.suggested_patch).toEqual({ "entry.command": "python3 train.py" });
    expect(result.explanation_zh).toBe("已将 command 改为训练入口。");
    expect(result.needs_user_confirmation).toBe(true);
  });

  it("forwards provider=none when explicitly requested", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        suggested_patch: {},
        explanation_zh: "无需改动。",
        needs_user_confirmation: false,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.suggestContractPatch(
      "alice",
      {},
      "recipe_python_cpu@1.0.0",
      "describe",
      "none",
    );

    const body = JSON.parse((fetchMock.mock.calls[0]![1] as RequestInit).body as string);
    expect(body.provider).toBe("none");
  });
});

describe("advanceRemediationSession provider passthrough", () => {
  it("sends provider=local by default", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ session_id: "s1", state: "diagnosing" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.advanceRemediationSession("alice", "sess_123");

    const call = fetchMock.mock.calls[0];
    const body = JSON.parse((call![1] as RequestInit).body as string);
    expect(body.provider).toBe("local");
  });

  it("sends provider=none when explicitly requested", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ session_id: "s1", state: "diagnosing" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.advanceRemediationSession("alice", "sess_123", undefined, { provider: "none" });

    const body = JSON.parse((fetchMock.mock.calls[0]![1] as RequestInit).body as string);
    expect(body.provider).toBe("none");
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
