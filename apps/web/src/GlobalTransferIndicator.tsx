import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, LoaderCircle, UploadCloud } from "lucide-react";
import { api } from "./api";
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
  const active = sessions.filter((item) => ACTIVE_SERVER_STATES.has(stateOf(item))).length;
  const failed = sessions.filter((item) => stateOf(item) === "failed").length;
  const label = active > 0
    ? `${active} 个后台任务`
    : failed > 0
      ? `${failed} 个任务失败`
      : "后台任务";

  return (
    <button
      type="button"
      className={`global-transfer-indicator${active > 0 ? " is-active" : ""}${failed > 0 ? " has-error" : ""}`}
      aria-label={`${label}，打开文件工作区查看`}
      title="服务器文件上传、校验、写入与解压状态"
      onClick={onOpen}
    >
      {failed > 0 ? (
        <AlertTriangle aria-hidden="true" />
      ) : uploads.isFetching && active > 0 ? (
        <LoaderCircle className="spin" aria-hidden="true" />
      ) : (
        <UploadCloud aria-hidden="true" />
      )}
      <span>{label}</span>
      {active > 0 || failed > 0 ? <strong>{active > 0 ? active : failed}</strong> : null}
    </button>
  );
}
