import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  FolderPlus,
  LayoutGrid,
  List,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  RotateCw,
  Upload,
  UploadCloud,
  X,
} from "lucide-react";
import * as tus from "tus-js-client";
import { api } from "./api";
import { RefreshButton, SectionHeading } from "./components";
import { FilesManagerProvider, useFilesManager } from "./files/FilesManagerContext";
import { PaneManager } from "./files/PaneManager";
import { usePaneLayout } from "./files/usePaneLayout";
import type { LocationState } from "./url";

interface PageProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

// tus resumable-upload tuning (unchanged from the single-pane page).
const TUS_ENDPOINT_PATH = "/api/v1/files/tus";
const TUS_CHUNK_SIZE = 8 * 1024 * 1024;
const TUS_PARALLEL_UPLOADS = 5;
const PARALLEL_THRESHOLD = 16 * 1024 * 1024;
const AUTO_DISMISS_DELAY_MS = 2500;

type UploadTaskState = "uploading" | "paused" | "completing" | "done" | "error";

interface UploadTask {
  id: string;
  filename: string;
  size: number;
  sent: number;
  speed: number;
  state: UploadTaskState;
  error?: string;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

function formatSpeed(bytesPerSec: number): string {
  return `${formatSize(bytesPerSec)}/s`;
}

function uploadStateLabel(state: UploadTaskState): string {
  switch (state) {
    case "uploading": return "上传中";
    case "paused": return "已暂停";
    case "completing": return "校验写入中";
    case "done": return "已完成";
    case "error": return "失败";
  }
}

export function FilesPage({ user }: PageProps) {
  const homePath = `/public/home/${user}`;
  const layoutApi = usePaneLayout();
  return (
    <FilesManagerProvider user={user} homePath={homePath}>
      <FilesShell user={user} homePath={homePath} layoutApi={layoutApi} />
    </FilesManagerProvider>
  );
}

function FilesShell({
  user,
  homePath,
  layoutApi,
}: {
  user: string;
  homePath: string;
  layoutApi: ReturnType<typeof usePaneLayout>;
}) {
  const queryClient = useQueryClient();
  const manager = useFilesManager();
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragCounter = useRef(0);
  const uploadRefs = useRef<Map<string, tus.Upload>>(new Map());
  const progressMeta = useRef<
    Map<string, { sent: number; time: number; speed: number; lastUi: number }>
  >(new Map());

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["files-list", user] });
  }, [queryClient, user]);

  const activeCwd = useCallback(
    () => manager.getActiveController()?.getCwd() ?? homePath,
    [manager, homePath],
  );

  const updateTask = (id: string, patch: Partial<UploadTask>) => {
    setTasks((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  };

  const handleProgress = (taskId: string, bytesSent: number, bytesTotal: number) => {
    const now = Date.now();
    const meta = progressMeta.current.get(taskId) ?? { sent: 0, time: now, speed: 0, lastUi: 0 };
    let speed = meta.speed;
    const dt = now - meta.time;
    if (dt > 0) {
      const instant = ((bytesSent - meta.sent) / dt) * 1000;
      speed = meta.speed === 0 ? instant : meta.speed * 0.7 + instant * 0.3;
    }
    const next = { sent: bytesSent, time: now, speed, lastUi: meta.lastUi };
    progressMeta.current.set(taskId, next);
    if (now - meta.lastUi >= 200 || bytesSent >= bytesTotal) {
      next.lastUi = now;
      updateTask(taskId, { sent: bytesSent, size: bytesTotal, speed });
    }
  };

  const removeTaskRefs = (taskId: string) => {
    uploadRefs.current.delete(taskId);
    progressMeta.current.delete(taskId);
    setTasks((prev) => prev.filter((t) => t.id !== taskId));
  };

  const finalizeTask = async (taskId: string, upload: tus.Upload) => {
    updateTask(taskId, { state: "completing" });
    const uploadId = (upload.url ?? "").replace(/\/+$/, "").split("/").pop() ?? "";
    try {
      await api.uploadComplete(user, uploadId);
      updateTask(taskId, { state: "done" });
      invalidate();
      window.setTimeout(() => removeTaskRefs(taskId), AUTO_DISMISS_DELAY_MS);
    } catch (err) {
      updateTask(taskId, {
        state: "error",
        error: err instanceof Error ? err.message : "校验/写入失败",
      });
    }
  };

  const startUploadTask = async (file: File, targetPath: string) => {
    const taskId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const autoExtract = file.name.endsWith(".tar.gz") || file.name.endsWith(".tgz");
    const task: UploadTask = {
      id: taskId,
      filename: file.name,
      size: file.size,
      sent: 0,
      speed: 0,
      state: "uploading",
    };
    const upload = new tus.Upload(file, {
      endpoint: `${window.location.origin}${TUS_ENDPOINT_PATH}`,
      chunkSize: TUS_CHUNK_SIZE,
      parallelUploads: file.size > PARALLEL_THRESHOLD ? TUS_PARALLEL_UPLOADS : 1,
      retryDelays: [0, 1000, 3000, 5000],
      storeFingerprintForResuming: true,
      removeFingerprintOnSuccess: true,
      metadata: {
        filename: file.name,
        target_path: targetPath,
        auto_extract: autoExtract ? "true" : "false",
      },
      headers: { "X-Pilot107-User": user },
      onProgress: (bytesSent, bytesTotal) => handleProgress(taskId, bytesSent, bytesTotal),
      onSuccess: () => void finalizeTask(taskId, upload),
      onError: (err) =>
        updateTask(taskId, { state: "error", error: err instanceof Error ? err.message : String(err) }),
    });
    uploadRefs.current.set(taskId, upload);
    setTasks((prev) => [...prev, task]);
    try {
      const previous = await upload.findPreviousUploads();
      const first = previous[0];
      if (first) upload.resumeFromPreviousUpload(first);
    } catch {
      // Fresh upload if resume lookup fails.
    }
    upload.start();
  };

  const handleUpload = useCallback(
    (files: FileList) => {
      const target = activeCwd();
      for (const file of Array.from(files)) void startUploadTask(file, target);
    },
    // activeCwd is stable enough; startUploadTask closes over stable refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeCwd],
  );

  // Expose the native picker to the manager (global "上传" button).
  useEffect(() => {
    manager.setUploadTrigger(() => fileInputRef.current?.click());
    return () => manager.setUploadTrigger(null);
  }, [manager]);

  const pauseTask = (taskId: string) => {
    const upload = uploadRefs.current.get(taskId);
    if (!upload) return;
    void upload.abort().then(() => updateTask(taskId, { state: "paused", speed: 0 }));
  };
  const resumeTask = (taskId: string) => {
    const upload = uploadRefs.current.get(taskId);
    if (!upload) return;
    updateTask(taskId, { state: "uploading" });
    upload.start();
  };
  const cancelTask = (taskId: string) => {
    const upload = uploadRefs.current.get(taskId);
    removeTaskRefs(taskId);
    if (upload) void upload.abort(true).catch(() => undefined);
  };

  // -- drag & drop upload onto the whole page (targets active pane cwd) --
  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    dragCounter.current++;
    if (e.dataTransfer.types.includes("Files")) setDragOver(true);
  };
  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    dragCounter.current--;
    if (dragCounter.current === 0) setDragOver(false);
  };
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    dragCounter.current = 0;
    setDragOver(false);
    if (e.dataTransfer.files.length) handleUpload(e.dataTransfer.files);
  };

  const toggleViewMode = () => {
    const controller = manager.getActiveController();
    if (!controller) return;
    controller.setViewMode(controller.getViewMode() === "grid" ? "list" : "grid");
  };

  const addPane = () => {
    const target = manager.activePaneId ?? layoutApi.paneIds[0];
    if (target) layoutApi.split(target, "horizontal");
  };

  return (
    <div
      className="page files-page files-multibase"
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={(e) => e.preventDefault()}
      onDrop={handleDrop}
    >
      <SectionHeading
        eyebrow="文件系统"
        title="远程文件管理"
        detail="多窗格浏览集群文件：框选、跨窗格拖动移动、拖放上传。"
        action={<RefreshButton onClick={invalidate} pending={false} />}
      />

      {dragOver && (
        <div className="files-drop-overlay">
          <div className="files-drop-inner">
            <UploadCloud size={48} aria-hidden="true" />
            <strong>释放文件以上传到当前窗格目录</strong>
            <span>{activeCwd()}</span>
          </div>
        </div>
      )}

      <div className="files-toolbar files-global-toolbar">
        <div className="files-toolbar-left">
          <button type="button" className="button primary" onClick={() => fileInputRef.current?.click()}>
            <Upload size={14} aria-hidden="true" /> 上传文件
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={() => manager.getActiveController()?.openMkdir()}
          >
            <FolderPlus size={14} aria-hidden="true" /> 新建目录
          </button>
          <button type="button" className="button secondary" onClick={() => manager.getActiveController()?.refresh()}>
            <RotateCw size={14} aria-hidden="true" /> 刷新
          </button>
          <button type="button" className="button secondary" onClick={toggleViewMode}>
            <LayoutGrid size={14} aria-hidden="true" /> 视图
          </button>
          <button type="button" className="button secondary" onClick={addPane}>
            <Plus size={14} aria-hidden="true" /> 拆分窗格
          </button>
          <button type="button" className="button ghost" onClick={layoutApi.reset}>
            重置布局
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files?.length) handleUpload(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {tasks.length > 0 && (
        <div className="files-upload-tasks" role="status" aria-live="polite">
          {tasks.map((task) => (
            <UploadTaskCard
              key={task.id}
              task={task}
              onPause={() => pauseTask(task.id)}
              onResume={() => resumeTask(task.id)}
              onCancel={() => cancelTask(task.id)}
              onDismiss={() => removeTaskRefs(task.id)}
            />
          ))}
        </div>
      )}

      <PaneManager layoutApi={layoutApi} homePath={homePath} />
    </div>
  );
}

function UploadTaskCard({
  task,
  onPause,
  onResume,
  onCancel,
  onDismiss,
}: {
  task: UploadTask;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onDismiss: () => void;
}) {
  const pct = task.size > 0 ? Math.min(100, Math.round((task.sent / task.size) * 100)) : 0;
  const active = task.state === "uploading" || task.state === "completing";
  return (
    <div className={`files-upload-card upload-${task.state}`}>
      <div className="upload-card-header">
        {active ? (
          <LoaderCircle size={16} className="spin upload-spinner" aria-hidden="true" />
        ) : task.state === "done" ? (
          <CheckCircle2 size={16} className="upload-icon-done" aria-hidden="true" />
        ) : task.state === "error" ? (
          <AlertCircle size={16} className="upload-icon-error" aria-hidden="true" />
        ) : (
          <Pause size={16} className="upload-icon-paused" aria-hidden="true" />
        )}
        <span className="upload-filename" title={task.filename}>{task.filename}</span>
        <span className="upload-state">{uploadStateLabel(task.state)}</span>
        <span className="upload-card-actions">
          {task.state === "uploading" && (
            <button type="button" className="icon-button" title="暂停" onClick={onPause}>
              <Pause size={14} aria-hidden="true" />
            </button>
          )}
          {task.state === "paused" && (
            <button type="button" className="icon-button" title="继续" onClick={onResume}>
              <Play size={14} aria-hidden="true" />
            </button>
          )}
          {(task.state === "uploading" || task.state === "paused" || task.state === "error") && (
            <button type="button" className="icon-button danger" title="取消" onClick={onCancel}>
              <X size={14} aria-hidden="true" />
            </button>
          )}
          {task.state === "done" && (
            <button type="button" className="icon-button" title="关闭" onClick={onDismiss}>
              <X size={14} aria-hidden="true" />
            </button>
          )}
        </span>
      </div>
      {task.state === "error" ? (
        <div className="upload-error-msg">{task.error}</div>
      ) : (
        <>
          <div className="upload-progress-track">
            <div className="upload-progress-fill" style={{ width: `${pct}%` }} />
          </div>
          <div className="upload-card-footer">
            <span>
              {formatSize(task.sent)} / {formatSize(task.size)}
              {task.state === "uploading" && task.speed > 0 && (
                <span className="upload-speed"> · {formatSpeed(task.speed)}</span>
              )}
            </span>
            <span>{task.state === "completing" ? "校验写入中…" : `${pct}%`}</span>
          </div>
        </>
      )}
    </div>
  );
}
