import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { Upload } from "tus-js-client";
import { api } from "./api";

const TUS_ENDPOINT_PATH = "/api/v1/files/tus";
const TUS_CHUNK_SIZE = 8 * 1024 * 1024;
const PARALLEL_THRESHOLD = 16 * 1024 * 1024;
const MAX_ACTIVE_FILES = 3;
const MAX_PARALLEL_CHUNKS_PER_FILE = 2;
const AUTO_DISMISS_DELAY_MS = 2500;

export type ArchivePostAction = "keep" | "extract";
export type TransferTaskState = "queued" | "uploading" | "paused" | "completing" | "done" | "error";

export interface TransferTask {
  id: string;
  filename: string;
  targetPath: string;
  size: number;
  sent: number;
  speed: number;
  state: TransferTaskState;
  autoExtract: boolean;
  error?: string;
}

interface TransferManagerValue {
  tasks: TransferTask[];
  activeCount: number;
  failedCount: number;
  enqueueFiles: (files: FileList | File[], targetPath: string, archivePostAction: ArchivePostAction) => void;
  pause: (taskId: string) => void;
  resume: (taskId: string) => void;
  cancel: (taskId: string) => void;
  dismiss: (taskId: string) => void;
}

const TransferManagerContext = createContext<TransferManagerValue | null>(null);

function isAutoExtractArchive(filename: string): boolean {
  const lower = filename.toLowerCase();
  return lower.endsWith(".tar.gz") || lower.endsWith(".tgz");
}

export function transferTaskStateLabel(state: TransferTaskState): string {
  switch (state) {
    case "queued": return "等待传输";
    case "uploading": return "浏览器传输中";
    case "paused": return "已暂停";
    case "completing": return "等待服务器完成";
    case "done": return "已交给服务器";
    case "error": return "失败";
  }
}

export function TransferManagerProvider({ user, children }: { user: string; children: ReactNode }) {
  const queryClient = useQueryClient();
  const [tasks, setTasks] = useState<TransferTask[]>([]);
  const fileRefs = useRef(new Map<string, File>());
  const uploadRefs = useRef(new Map<string, Upload>());
  const startingRefs = useRef(new Set<string>());
  const progressMeta = useRef(new Map<string, { sent: number; time: number; speed: number; lastUi: number }>());
  const mountedRef = useRef(true);

  useEffect(() => () => {
    mountedRef.current = false;
  }, []);

  const updateTask = useCallback((id: string, patch: Partial<TransferTask>) => {
    if (!mountedRef.current) return;
    setTasks((current) => current.map((task) => task.id === id ? { ...task, ...patch } : task));
  }, []);

  const invalidateBackendState = useCallback((targetPath: string) => {
    void queryClient.invalidateQueries({ queryKey: ["files-list", user, targetPath], exact: true });
    void queryClient.invalidateQueries({ queryKey: ["files-usage", user] });
    void queryClient.invalidateQueries({ queryKey: ["file-uploads", user] });
  }, [queryClient, user]);

  const removeTaskRefs = useCallback((taskId: string) => {
    fileRefs.current.delete(taskId);
    uploadRefs.current.delete(taskId);
    startingRefs.current.delete(taskId);
    progressMeta.current.delete(taskId);
    if (mountedRef.current) setTasks((current) => current.filter((task) => task.id !== taskId));
  }, []);

  const handleProgress = useCallback((taskId: string, bytesSent: number, bytesTotal: number) => {
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
  }, [updateTask]);

  const completeTask = useCallback(async (taskId: string, upload: Upload, targetPath: string) => {
    updateTask(taskId, { state: "completing", speed: 0 });
    const uploadId = (upload.url ?? "").replace(/\/+$/, "").split("/").pop() ?? "";
    if (!uploadId) {
      updateTask(taskId, { state: "error", error: "服务器未返回可识别的上传会话 ID" });
      return;
    }
    try {
      await api.uploadComplete(user, uploadId);
      updateTask(taskId, { state: "done" });
      invalidateBackendState(targetPath);
      window.setTimeout(() => removeTaskRefs(taskId), AUTO_DISMISS_DELAY_MS);
    } catch (error) {
      updateTask(taskId, {
        state: "error",
        error: error instanceof Error ? error.message : "服务器校验、写入或解压失败",
      });
      void queryClient.invalidateQueries({ queryKey: ["file-uploads", user] });
    }
  }, [invalidateBackendState, queryClient, removeTaskRefs, updateTask, user]);

  const startTask = useCallback(async (task: TransferTask) => {
    if (startingRefs.current.has(task.id) || uploadRefs.current.has(task.id)) return;
    const file = fileRefs.current.get(task.id);
    if (!file) {
      updateTask(task.id, { state: "error", error: "浏览器已丢失待上传文件引用" });
      return;
    }
    startingRefs.current.add(task.id);
    try {
      const tus = await import("tus-js-client");
      const upload = new tus.Upload(file, {
        endpoint: `${window.location.origin}${TUS_ENDPOINT_PATH}`,
        chunkSize: TUS_CHUNK_SIZE,
        parallelUploads: file.size > PARALLEL_THRESHOLD ? MAX_PARALLEL_CHUNKS_PER_FILE : 1,
        retryDelays: [0, 1000, 3000, 5000],
        storeFingerprintForResuming: true,
        removeFingerprintOnSuccess: true,
        metadata: {
          filename: file.name,
          target_path: task.targetPath,
          auto_extract: task.autoExtract ? "true" : "false",
        },
        headers: { "X-Pilot107-User": user },
        onProgress: (bytesSent, bytesTotal) => handleProgress(task.id, bytesSent, bytesTotal),
        onSuccess: () => void completeTask(task.id, upload, task.targetPath),
        onError: (error) => {
          updateTask(task.id, {
            state: "error",
            speed: 0,
            error: error instanceof Error ? error.message : String(error),
          });
          void queryClient.invalidateQueries({ queryKey: ["file-uploads", user] });
        },
      });
      uploadRefs.current.set(task.id, upload);
      updateTask(task.id, { state: "uploading" });
      try {
        const previous = await upload.findPreviousUploads();
        if (previous[0]) upload.resumeFromPreviousUpload(previous[0]);
      } catch {
        // Resume discovery is opportunistic; a fresh tus session remains valid.
      }
      upload.start();
    } catch (error) {
      updateTask(task.id, {
        state: "error",
        error: error instanceof Error ? error.message : "无法加载可恢复上传模块",
      });
    } finally {
      startingRefs.current.delete(task.id);
    }
  }, [completeTask, handleProgress, queryClient, updateTask, user]);

  useEffect(() => {
    const uploading = tasks.filter((task) => task.state === "uploading").length;
    const available = Math.max(0, MAX_ACTIVE_FILES - uploading - startingRefs.current.size);
    if (available <= 0) return;
    const queued = tasks.filter((task) => task.state === "queued").slice(0, available);
    for (const task of queued) void startTask(task);
  }, [startTask, tasks]);

  const enqueueFiles = useCallback((files: FileList | File[], targetPath: string, archivePostAction: ArchivePostAction) => {
    const next = Array.from(files).map((file) => {
      const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
      fileRefs.current.set(id, file);
      return {
        id,
        filename: file.name,
        targetPath,
        size: file.size,
        sent: 0,
        speed: 0,
        state: "queued" as const,
        autoExtract: archivePostAction === "extract" && isAutoExtractArchive(file.name),
      };
    });
    if (next.length > 0) setTasks((current) => [...current, ...next]);
  }, []);

  const pause = useCallback((taskId: string) => {
    const upload = uploadRefs.current.get(taskId);
    if (!upload) {
      updateTask(taskId, { state: "paused", speed: 0 });
      return;
    }
    void upload.abort().then(() => updateTask(taskId, { state: "paused", speed: 0 }));
  }, [updateTask]);

  const resume = useCallback((taskId: string) => {
    const upload = uploadRefs.current.get(taskId);
    if (upload) {
      uploadRefs.current.delete(taskId);
    }
    updateTask(taskId, { state: "queued", error: undefined });
  }, [updateTask]);

  const cancel = useCallback((taskId: string) => {
    const upload = uploadRefs.current.get(taskId);
    removeTaskRefs(taskId);
    if (!upload) return;
    void upload.abort(true)
      .catch(() => undefined)
      .finally(() => {
        void queryClient.invalidateQueries({ queryKey: ["file-uploads", user] });
      });
  }, [queryClient, removeTaskRefs, user]);

  const value = useMemo<TransferManagerValue>(() => ({
    tasks,
    activeCount: tasks.filter((task) => task.state === "queued" || task.state === "uploading" || task.state === "completing").length,
    failedCount: tasks.filter((task) => task.state === "error").length,
    enqueueFiles,
    pause,
    resume,
    cancel,
    dismiss: removeTaskRefs,
  }), [cancel, enqueueFiles, pause, removeTaskRefs, resume, tasks]);

  return <TransferManagerContext.Provider value={value}>{children}</TransferManagerContext.Provider>;
}

export function useTransferManager(): TransferManagerValue {
  const value = useContext(TransferManagerContext);
  if (!value) throw new Error("useTransferManager must be used inside TransferManagerProvider");
  return value;
}
