from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def patch_query() -> None:
    path = Path("apps/web/src/query.ts")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        'import type { AgentTask } from "./types";\n',
        'import type { AgentTask, RuntimeWatchState } from "./types";\n'
        'import { runtimePollingInterval, type RuntimeViewerVisibility } from "./runtime-polling";\n',
        label="query runtime polling imports",
    )
    old = '''export function useRuntimeWatch(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["runtime-watch", user, runId],
    queryFn: ({ signal }) => api.runtimeWatch(user, runId ?? "", signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: (query) => query.state.data?.state === "stopped" ? false : 5_000,
  });
}

export function useRuntimeWatchLogs(
  user: string,
  runId: string | null,
  stream: "stdout" | "stderr",
) {
  return useQuery({
    queryKey: ["runtime-watch-logs", user, runId, stream],
    queryFn: ({ signal }) => api.runtimeWatchLogs(user, runId ?? "", stream, signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: 5_000,
  });
}

export function useRuntimeWatchAlerts(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["runtime-watch-alerts", user, runId],
    queryFn: ({ signal }) => api.runtimeWatchAlerts(user, runId ?? "", signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: 5_000,
  });
}
'''
    new = '''export function useRuntimeWatch(
  user: string,
  runId: string | null,
  visibility: RuntimeViewerVisibility = "visible",
) {
  return useQuery({
    queryKey: ["runtime-watch", user, runId],
    queryFn: ({ signal }) => api.runtimeWatch(user, runId ?? "", signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: (query) =>
      runtimePollingInterval("summary", query.state.data?.state, visibility),
    refetchIntervalInBackground: false,
  });
}

export function useRuntimeWatchLogs(
  user: string,
  runId: string | null,
  stream: "stdout" | "stderr",
  watchState: RuntimeWatchState | null = null,
  visibility: RuntimeViewerVisibility = "visible",
) {
  return useQuery({
    queryKey: ["runtime-watch-logs", user, runId, stream],
    queryFn: ({ signal }) => api.runtimeWatchLogs(user, runId ?? "", stream, signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: () => runtimePollingInterval("logs", watchState, visibility),
    refetchIntervalInBackground: false,
  });
}

export function useRuntimeWatchAlerts(
  user: string,
  runId: string | null,
  watchState: RuntimeWatchState | null = null,
  visibility: RuntimeViewerVisibility = "visible",
) {
  return useQuery({
    queryKey: ["runtime-watch-alerts", user, runId],
    queryFn: ({ signal }) => api.runtimeWatchAlerts(user, runId ?? "", signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: () => runtimePollingInterval("alerts", watchState, visibility),
    refetchIntervalInBackground: false,
  });
}
'''
    text = replace_once(text, old, new, label="runtime watch query hooks")
    path.write_text(text, encoding="utf-8")


def patch_runtime_panel() -> None:
    path = Path("apps/web/src/RuntimeWatchPanel.tsx")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '} from "./query";\nimport type { RuntimeWatchState } from "./types";\n',
        '} from "./query";\nimport { useDocumentVisibility } from "./runtime-polling";\nimport type { RuntimeWatchState } from "./types";\n',
        label="runtime panel visibility import",
    )
    old = '''export function RuntimeWatchPanel({ user, runId }: { user: string; runId: string }) {
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const watch = useRuntimeWatch(user, runId);
  const logs = useRuntimeWatchLogs(user, runId, stream);
  const alerts = useRuntimeWatchAlerts(user, runId);
  const absent = watch.error instanceof ApiRequestError && watch.error.status === 404;
'''
    new = '''export function RuntimeWatchPanel({ user, runId }: { user: string; runId: string }) {
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const visibility = useDocumentVisibility();
  const watch = useRuntimeWatch(user, runId, visibility);
  const absent = watch.error instanceof ApiRequestError && watch.error.status === 404;
  const childRunId = watch.data && !absent ? runId : null;
  const watchState = watch.data?.state ?? null;
  const logs = useRuntimeWatchLogs(user, childRunId, stream, watchState, visibility);
  const alerts = useRuntimeWatchAlerts(
    user,
    watch.data?.alert_count ? childRunId : null,
    watchState,
    visibility,
  );
'''
    text = replace_once(text, old, new, label="runtime panel gated child queries")
    path.write_text(text, encoding="utf-8")


def patch_visual_spec() -> None:
    path = Path("tests/ui/visual.spec.js")
    text = path.read_text(encoding="utf-8")
    anchor = '''test("run detail makes an omitted workdir explicit", async ({ page }) => {
'''
    tests = '''test("stopped Runtime Watch reads its selected log once without a polling storm", async ({ page }) => {
  let summaryRequests = 0;
  let logRequests = 0;
  let alertRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/runtime-watch") summaryRequests += 1;
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/runtime-watch/logs") logRequests += 1;
    if (url.pathname === "/api/v1/runs/run_alice_succeeded/runtime-watch/alerts") alertRequests += 1;
  });

  await page.goto("/runs/run_alice_succeeded?user=alice&tab=logs");
  await expect(
    page.getByLabel("Runtime Watch 实时日志").getByText("training complete", { exact: false }),
  ).toBeVisible();
  await page.waitForTimeout(5_500);

  expect(summaryRequests).toBe(1);
  expect(logRequests).toBe(1);
  expect(alertRequests).toBe(0);
});

test("absent Runtime Watch does not start log or alert child requests", async ({ page }) => {
  await installMockApi(page, { runtimeWatchAbsent: true });
  let logRequests = 0;
  let alertRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/runs/run_alice_failed/runtime-watch/logs") logRequests += 1;
    if (url.pathname === "/api/v1/runs/run_alice_failed/runtime-watch/alerts") alertRequests += 1;
  });

  await page.goto("/runs/run_alice_failed?user=alice&tab=logs");
  await expect(page.getByText("尚未建立 Runtime Watch", { exact: true })).toBeVisible();
  expect(logRequests).toBe(0);
  expect(alertRequests).toBe(0);
});

'''
    text = replace_once(text, anchor, tests + anchor, label="runtime polling browser tests")
    old_route = '''    if (url.pathname === "/api/v1/runs/run_alice_failed/runtime-watch") {
      return json(route, runtimeWatchPayload("run_alice_failed"));
    }
'''
    new_route = '''    if (url.pathname === "/api/v1/runs/run_alice_failed/runtime-watch") {
      if (options.runtimeWatchAbsent) {
        return json(route, { error: { code: "NOT_FOUND", message: "runtime watch not found" } }, 404);
      }
      return json(route, runtimeWatchPayload("run_alice_failed"));
    }
'''
    text = replace_once(text, old_route, new_route, label="runtime watch absent mock")
    path.write_text(text, encoding="utf-8")


def main() -> None:
    patch_query()
    patch_runtime_panel()
    patch_visual_spec()


if __name__ == "__main__":
    main()
