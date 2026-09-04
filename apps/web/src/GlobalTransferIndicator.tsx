import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, LoaderCircle, UploadCloud } from "lucide-react";
import { api } from "./api";
import { useTransferManager } from "./TransferManager";
import type { UploadSession } from "./types";

const ACTIVE_SERVER_STATES = new Set([
  "initialized",
  "uploading",
  "assembled",
  "verified",
  "completing",
]);

function stateOf(session: UploadSession): string {
  return String(session.state);
}

export function GlobalTransferIndicator({
  user,
  onOpen,
}: {
  user: string;
  onOpen: () => void;
}) {
  const client = useTransferManager();
  const uploads = useQuery({
    queryKey: ["file-uploads", user],
    queryFn: ({ signal }) => api.uploadSessions(user, signal),
    staleTime: 3_000,
    refetchInterval: (query) => {
      const data = query.state.data as { items: UploadSession[] } | undefined;
      return data?.items.some((item) => !item.is_partial && ACTIVE_SERVER_STATES.has(stateOf(item)))
        ? 3_000
        : 30_000;
    },
    retry: false,
  });

  const sessions = (uploads.data?.items ?? []).filter((item) => !item.is_partial);
  const serverActive = sessions.filter((item) => ACTIVE_SERVER_STATES.has(stateOf(item))).length;
  const serverFailed = sessions.filter((item) => stateOf(item) === "failed").length;
  const hasFailure = client.failedCount > 0 || serverFailed > 0;
  const totalVisible = client.activeCount > 0 ? client.activeCount : serverActive;
  const label = client.activeCount > 0 && serverActive > 0
    ? `传输 ${client.activeCount} · 服务器 ${serverActive}`
    : client.activeCount > 0
      ? `${client.activeCount} 个传输任务`
      : serverActive > 0
        ? `${serverActive} 个服务器任务`
        : hasFailure
          ? "文件任务需处理"
          : "后台任务";

  return (
    <button
      type="button"
      className={`global-transfer-indicator${client.activeCount > 0 || serverActive > 0 ? " is-active" : ""}${hasFailure ? " has-error" : ""}`}
      aria-label={`${label}，打开文件工作区查看`}
      title="浏览器传输与服务器校验、写入、解压状态"
      onClick={onOpen}
    >
      {hasFailure ? (
        <AlertTriangle aria-hidden="true" />
      ) : uploads.isFetching && serverActive > 0 ? (
        <LoaderCircle className="spin" aria-hidden="true" />
      ) : (
        <UploadCloud aria-hidden="true" />
      )}
      <span>{label}</span>
      {totalVisible > 0 || hasFailure ? (
        <strong>{totalVisible > 0 ? totalVisible : client.failedCount + serverFailed}</strong>
      ) : null}
    </button>
  );
}
