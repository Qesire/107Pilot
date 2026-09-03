import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  HardDrive,
  LoaderCircle,
  RefreshCw,
  UploadCloud,
  X,
} from "lucide-react";
import { api } from "../api";
import { formatStorageBytes } from "../resource-summary";
import type { UploadSession, UploadSessionState } from "../types";

const ACTIVE_UPLOAD_STATES = new Set<UploadSessionState>([
  "initialized",
  "uploading",
  "completing",
]);

const FINISHED_UPLOAD_STATES = new Set<UploadSessionState>([
  "completed",
  "written",
]);

export function uploadStateLabel(state: UploadSessionState): string {
  switch (state) {
    case "initialized": return "等待上传";
    case "uploading": return "上传中";
    case "completing": return "校验写入中";
    case "completed": return "上传完成";
    case "written": return "已写入";
    case "aborted": return "已取消";
    case "failed": return "失败";
  }
}

export function uploadProgress(session: UploadSession): number {
  if (session.total_size <= 0) return FINISHED_UPLOAD_STATES.has(session.state) ? 100 : 0;
  return Math.min(100, Math.max(0, Math.round((session.received_bytes / session.total_size) * 100)));
}

function usagePercent(used: number, total: number | null): number | null {
  if (!total || total <= 0) return null;
  return Math.min(100, Math.max(0, (used / total) * 100));
}

function sessionTone(session: UploadSession): "active" | "success" | "danger" | "neutral" {
  if (ACTIVE_UPLOAD_STATES.has(session.state)) return "active";
  if (FINISHED_UPLOAD_STATES.has(session.state)) return "success";
  if (session.state === "failed") return "danger";
  return "neutral";
}

export function FileWorkspaceStatus({ user, homePath }: { user: string; homePath: string }) {
  const queryClient = useQueryClient();
  const usage = useQuery({
    queryKey: ["files-usage", user],
    queryFn: ({ signal }) => api.storageUsage(user, signal),
    staleTime: 60_000,
    retry: false,
  });
  const uploads = useQuery({
    queryKey: ["file-uploads", user],
    queryFn: ({ signal }) => api.uploadSessions(user, signal),
    staleTime: 2_000,
    refetchInterval: (query) => {
      const data = query.state.data as { items: UploadSession[] } | undefined;
      return data?.items.some((item) => ACTIVE_UPLOAD_STATES.has(item.state)) ? 2_000 : 30_000;
    },
    retry: false,
  });
  const abortUpload = useMutation({
    mutationFn: (uploadId: string) => api.uploadAbort(user, uploadId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["file-uploads", user] });
    },
  });

  const sessions = (uploads.data?.items ?? [])
    .filter((session) => !session.is_partial)
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const active = sessions.filter((session) => ACTIVE_UPLOAD_STATES.has(session.state));
  const failures = sessions.filter((session) => session.state === "failed");
  const recent = [...active, ...failures, ...sessions.filter((session) => !ACTIVE_UPLOAD_STATES.has(session.state) && session.state !== "failed")]
    .filter((session, index, items) => items.findIndex((candidate) => candidate.upload_id === session.upload_id) === index)
    .slice(0, 4);
  const usedPercent = usage.data ? usagePercent(usage.data.used_bytes, usage.data.total_bytes) : null;

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["files-usage", user] });
    void queryClient.invalidateQueries({ queryKey: ["file-uploads", user] });
  };

  return (
    <section className="file-workspace-status" aria-label="文件工作区状态">
      <article className="file-status-card storage-card">
        <div className="file-status-icon"><HardDrive aria-hidden="true" /></div>
        <div className="file-status-body">
          <div className="file-status-heading">
            <div>
              <span>个人存储</span>
              <strong>{usage.data ? formatStorageBytes(usage.data.used_bytes) : "读取中…"}</strong>
            </div>
            <button type="button" className="icon-button" title="刷新文件状态" onClick={refresh}>
              <RefreshCw size={15} aria-hidden="true" />
            </button>
          </div>
          {usage.isError ? (
            <p className="file-status-error">无法读取存储用量：{usage.error instanceof Error ? usage.error.message : "未知错误"}</p>
          ) : (
            <>
              <div className="file-storage-track" aria-label="存储用量">
                <span style={{ width: `${usedPercent ?? 0}%` }} />
              </div>
              <p>
                {usage.data?.total_bytes
                  ? `${formatStorageBytes(usage.data.used_bytes)} / ${formatStorageBytes(usage.data.total_bytes)}`
                  : "后端未提供总容量；已显示当前已用空间。"}
              </p>
            </>
          )}
          <code title={homePath}>{homePath}</code>
        </div>
      </article>

      <article className="file-status-card transfer-card">
        <div className="file-status-icon"><UploadCloud aria-hidden="true" /></div>
        <div className="file-status-body">
          <div className="file-status-heading">
            <div>
              <span>后台传输</span>
              <strong>{active.length > 0 ? `${active.length} 个进行中` : failures.length > 0 ? `${failures.length} 个失败` : "没有进行中的任务"}</strong>
            </div>
            {uploads.isFetching ? <LoaderCircle className="spin" size={15} aria-label="正在同步后端传输状态" /> : null}
          </div>

          {uploads.isError ? (
            <p className="file-status-error">无法同步上传会话：{uploads.error instanceof Error ? uploads.error.message : "未知错误"}</p>
          ) : recent.length === 0 ? (
            <p className="file-status-empty"><CheckCircle2 size={14} aria-hidden="true" /> 上传任务会在这里持续显示，即使浏览器上传完成后仍以服务器状态为准。</p>
          ) : (
            <ul className="server-upload-list">
              {recent.map((session) => {
                const pct = uploadProgress(session);
                const canAbort = ACTIVE_UPLOAD_STATES.has(session.state);
                return (
                  <li key={session.upload_id} className={`server-upload-row tone-${sessionTone(session)}`}>
                    <div className="server-upload-main">
                      {session.state === "failed" ? <AlertTriangle size={14} aria-hidden="true" /> : null}
                      <span title={`${session.target_path}/${session.filename}`}>{session.filename}</span>
                      <small>{uploadStateLabel(session.state)} · {pct}%</small>
                    </div>
                    <div className="server-upload-progress" aria-label={`${session.filename} ${pct}%`}><span style={{ width: `${pct}%` }} /></div>
                    {canAbort ? (
                      <button
                        type="button"
                        className="icon-button danger"
                        title={`取消服务器上传 ${session.filename}`}
                        disabled={abortUpload.isPending}
                        onClick={() => abortUpload.mutate(session.upload_id)}
                      >
                        <X size={13} aria-hidden="true" />
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </article>
    </section>
  );
}
