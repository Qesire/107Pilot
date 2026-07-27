import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Cable, CheckCircle2, RefreshCw } from "lucide-react";
import { api } from "./api";
import { formatTimestamp, StatusBadge } from "./components";
import { usePlatformConnections } from "./query";
import type { PlatformConnection } from "./types";

export function ConnectionBadge({ user }: { user: string }) {
  const query = usePlatformConnections(user);
  const connection = query.data?.items[0];
  if (query.isPending || query.isError || !connection) return null;
  return (
    <StatusBadge
      label={connectionLabel(connection)}
      tone={connectionTone(connection)}
    />
  );
}

export function ConnectionActionBanner({ user }: { user: string }) {
  const query = usePlatformConnections(user);
  const connection = query.data?.items[0];
  const check = useConnectionCheck(user, connection);
  if (!connection || connection.state === "active") return null;
  return (
    <div className="connection-banner" role="alert">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>{connectionLabel(connection)}</strong>
        <span>{connection.message}</span>
      </div>
      <button
        className="button secondary"
        type="button"
        disabled={check.isPending}
        onClick={() => check.mutate()}
      >
        <RefreshCw
          aria-hidden="true"
          className={check.isPending ? "spin" : undefined}
        />
        重新检查
      </button>
    </div>
  );
}

export function ConnectionPanel({ user }: { user: string }) {
  const query = usePlatformConnections(user);
  const connection = query.data?.items[0];
  const check = useConnectionCheck(user, connection);
  if (query.isPending) {
    return <p className="limitation">正在读取真实平台连接状态…</p>;
  }
  if (query.isError) {
    return (
      <p className="limitation" role="alert">
        连接状态读取失败：{query.error.message}
      </p>
    );
  }
  if (!connection) {
    return <p className="limitation">当前部署未配置真实平台 SSH 连接。</p>;
  }
  const active = connection.state === "active";
  return (
    <div className="connection-panel">
      <div className={`connection-state ${active ? "is-active" : "needs-action"}`}>
        {active
          ? <CheckCircle2 aria-hidden="true" />
          : <AlertTriangle aria-hidden="true" />}
        <div>
          <strong>{connectionLabel(connection)}</strong>
          <p>{connection.message}</p>
        </div>
      </div>
      <dl className="fact-list">
        <div><dt>Target</dt><dd>{connection.target_id}</dd></div>
        <div><dt>Scope</dt><dd>仅当前用户</dd></div>
        <div>
          <dt>Checked</dt>
          <dd>{formatTimestamp(connection.checked_at)}</dd>
        </div>
      </dl>
      <button
        className="button secondary"
        type="button"
        disabled={check.isPending}
        onClick={() => check.mutate()}
      >
        <Cable aria-hidden="true" />
        {check.isPending ? "检查中" : "检查连接"}
      </button>
      {check.isError
        ? <p className="limitation" role="alert">{check.error.message}</p>
        : null}
    </div>
  );
}

function useConnectionCheck(
  user: string,
  connection: PlatformConnection | undefined,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => {
      if (!connection) throw new Error("当前没有可检查的连接");
      return api.checkPlatformConnection(user, connection.connection_id);
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(["platform-connections", user], {
        items: [updated],
      });
    },
  });
}

function connectionLabel(connection: PlatformConnection): string {
  switch (connection.state) {
    case "active":
      return "真实 107 已连接";
    case "auth_required":
    case "expired":
      return "真实 107 需要 MFA";
    case "revoked":
      return "真实 107 连接已撤销";
    default:
      return "真实 107 暂不可用";
  }
}

function connectionTone(
  connection: PlatformConnection,
): "success" | "warning" | "danger" {
  if (connection.state === "active") return "success";
  if (connection.state === "unavailable") return "danger";
  return "warning";
}
