import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  FolderPlus,
  LayoutGrid,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  RotateCw,
  Upload,
  UploadCloud,
  X,
} from "lucide-react";
import { SectionHeading } from "./components";
import { FilesManagerProvider, useFilesManager } from "./files/FilesManagerContext";
import { FileSearchPanel, fileSearchOpenTarget } from "./files/FileSearchPanel";
import { FileWorkspaceStatus } from "./files/FileWorkspaceStatus";
import { PaneManager } from "./files/PaneManager";
import { usePaneLayout } from "./files/usePaneLayout";
import {
  transferTaskStateLabel,
  useTransferManager,
  type ArchivePostAction,
  type TransferTask,
} from "./TransferManager";
import type { LocationState } from "./url";

interface PageProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
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
  const transfers = useTransferManager();
  const [archivePostAction, setArchivePostAction] = useState<ArchivePostAction>("keep");
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragCounter = useRef(0);

  const activeCwd = useCallback(
    () => manager.getActiveController()?.getCwd() ?? homePath,
    [manager, homePath],
  );

  const refreshBackendState = useCallback(() => {
    manager.getActiveController()?.refresh();
    void queryClient.invalidateQueries({ queryKey: ["files-usage", user] });
    void queryClient.invalidateQueries({ queryKey: ["file-uploads", user] });
  }, [manager, queryClient, user]);

  const handleUpload = useCallback((files: FileList) => {
    transfers.enqueueFiles(files, activeCwd(), archivePostAction);
  }, [activeCwd, archivePostAction, transfers]);

  useEffect(() => {
    manager.setUploadTrigger(() => fileInputRef.current?.click());
    return () => manager.setUploadTrigger(null);
  }, [manager]);

  const handleDragEnter = (event: React.DragEvent) => {
    event.preventDefault();
    dragCounter.current++;
    if (event.dataTransfer.types.includes("Files")) setDragOver(true);
  };

  const handleDragLeave = (event: React.DragEvent) => {
    event.preventDefault();
    dragCounter.current--;
    if (dragCounter.current === 0) setDragOver(false);
  };

  const handleDrop = (event: React.DragEvent) => {
    event.preventDefault();
    dragCounter.current = 0;
    setDragOver(false);
    if (event.dataTransfer.files.length) handleUpload(event.dataTransfer.files);
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
      onDragOver={(event) => event.preventDefault()}
      onDrop={handleDrop}
    >
      <SectionHeading
        eyebrow="文件 / 科研资产"
        title="文件工作区"
        detail="上传、检索和组织实验资产；浏览器传输由全局任务管理器持有，服务器负责完整性校验、集群写入与可选解压。"
      />

      <FileWorkspaceStatus user={user} homePath={homePath} />

      {dragOver ? (
        <div className="files-drop-overlay">
          <div className="files-drop-inner">
            <UploadCloud size={48} aria-hidden="true" />
            <strong>释放文件以上传到当前窗格目录</strong>
            <span>{activeCwd()}</span>
          </div>
        </div>
      ) : null}

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
          <button type="button" className="button secondary" onClick={refreshBackendState}>
            <RotateCw size={14} aria-hidden="true" /> 刷新
          </button>
          <button type="button" className="button secondary" onClick={toggleViewMode}>
            <LayoutGrid size={14} aria-hidden="true" /> 切换视图
          </button>
          <button type="button" className="button secondary" onClick={addPane}>
            <Plus size={14} aria-hidden="true" /> 拆分窗格
          </button>
          <button type="button" className="button ghost" onClick={layoutApi.reset}>
            重置布局
          </button>
        </div>

        <div className="files-toolbar-right">
          <label className="file-upload-policy">
            <span>tar.gz / tgz 上传后</span>
            <select
              value={archivePostAction}
              aria-label="压缩包上传后处理"
              onChange={(event) => setArchivePostAction(event.target.value as ArchivePostAction)}
            >
              <option value="keep">仅写入压缩包</option>
              <option value="extract">写入后自动解压</option>
            </select>
          </label>
        </div>

        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            if (event.target.files?.length) handleUpload(event.target.files);
            event.target.value = "";
          }}
        />
      </div>

      <FileSearchPanel
        user={user}
        root={manager.activePath}
        onOpen={(entry) => {
          const target = fileSearchOpenTarget(entry);
          manager.openPath(target.path, target.selectedPath);
        }}
      />

      {transfers.tasks.length > 0 ? (
        <div className="files-upload-tasks" role="status" aria-live="polite">
          {transfers.tasks.map((task) => (
            <TransferTaskCard
              key={task.id}
              task={task}
              onPause={() => transfers.pause(task.id)}
              onResume={() => transfers.resume(task.id)}
              onCancel={() => transfers.cancel(task.id)}
              onDismiss={() => transfers.dismiss(task.id)}
            />
          ))}
        </div>
      ) : null}

      <PaneManager layoutApi={layoutApi} homePath={homePath} />
    </div>
  );
}

function TransferTaskCard({
  task,
  onPause,
  onResume,
  onCancel,
  onDismiss,
}: {
  task: TransferTask;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onDismiss: () => void;
}) {
  const pct = task.size > 0 ? Math.min(100, Math.round((task.sent / task.size) * 100)) : 0;
  const active = task.state === "queued" || task.state === "uploading" || task.state === "completing";
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
        <span className="upload-filename" title={`${task.targetPath}/${task.filename}`}>{task.filename}</span>
        <span className="upload-state">{transferTaskStateLabel(task.state)}</span>
        <span className="upload-card-actions">
          {task.state === "uploading" ? (
            <button type="button" className="icon-button" title="暂停" onClick={onPause}>
              <Pause size={14} aria-hidden="true" />
            </button>
          ) : null}
          {task.state === "paused" ? (
            <button type="button" className="icon-button" title="继续" onClick={onResume}>
              <Play size={14} aria-hidden="true" />
            </button>
          ) : null}
          {task.state === "queued" || task.state === "uploading" || task.state === "paused" || task.state === "error" ? (
            <button type="button" className="icon-button danger" title="取消" onClick={onCancel}>
              <X size={14} aria-hidden="true" />
            </button>
          ) : null}
          {task.state === "done" ? (
            <button type="button" className="icon-button" title="关闭" onClick={onDismiss}>
              <X size={14} aria-hidden="true" />
            </button>
          ) : null}
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
              {task.state === "uploading" && task.speed > 0 ? (
                <span className="upload-speed"> · {formatSpeed(task.speed)}</span>
              ) : null}
            </span>
            <span>{task.state === "completing" ? "服务器处理中…" : `${pct}%`}</span>
          </div>
        </>
      )}
    </div>
  );
}
