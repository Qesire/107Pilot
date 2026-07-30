import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import {
  Archive,
  ChevronRight,
  Download,
  File,
  FileText,
  Folder,
  FolderPlus,
  Home,
  Link2,
  LoaderCircle,
  Trash2,
  Upload,
} from "lucide-react";
import { api, ApiRequestError } from "./api";
import type { FileEntry, UploadSession } from "./types";
import { QueryBoundary, RefreshButton, SectionHeading } from "./components";
import type { LocationState } from "./url";

interface PageProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

const CHUNK_SIZE = 512 * 1024; // 512 KiB per chunk

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

function parentPath(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const idx = trimmed.lastIndexOf("/");
  if (idx <= 0) return "/";
  return trimmed.slice(0, idx);
}

function pathSegments(path: string): Array<{ label: string; path: string }> {
  const parts = path.split("/").filter(Boolean);
  const segments: Array<{ label: string; path: string }> = [{ label: "/", path: "/" }];
  let accumulated = "";
  for (const part of parts) {
    accumulated += `/${part}`;
    segments.push({ label: part, path: accumulated });
  }
  return segments;
}

export function FilesPage({ user }: PageProps) {
  const queryClient = useQueryClient();
  const homePath = `/public/home/${user}`;
  const [currentPath, setCurrentPath] = useState(homePath);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [mkdirName, setMkdirName] = useState("");
  const [uploadProgress, setUploadProgress] = useState<{
    filename: string;
    sent: number;
    total: number;
    state: string;
  } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const listing = useQuery({
    queryKey: ["files-list", user, currentPath],
    queryFn: ({ signal }) => api.fileList(user, currentPath, signal),
    retry: false,
  });

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["files-list", user] });
  }, [queryClient, user]);

  const navigateTo = (path: string) => {
    setCurrentPath(path);
    setSelected(new Set());
    setActionError(null);
  };

  const toggleSelect = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  // -- mkdir --
  const mkdirMutation = useMutation({
    mutationFn: (name: string) => {
      const target = `${currentPath.replace(/\/+$/, "")}/${name}`;
      return api.fileMkdir(user, target);
    },
    onSuccess: () => {
      setMkdirOpen(false);
      setMkdirName("");
      invalidate();
    },
    onError: (err) => setActionError(err instanceof Error ? err.message : "创建目录失败"),
  });

  // -- delete --
  const deleteMutation = useMutation({
    mutationFn: (path: string) => api.fileDelete(user, path),
    onSuccess: () => {
      setSelected(new Set());
      invalidate();
    },
    onError: (err) => setActionError(err instanceof Error ? err.message : "删除失败"),
  });

  // -- archive --
  const archiveMutation = useMutation({
    mutationFn: (paths: string[]) => api.fileArchive(user, paths, currentPath),
    onSuccess: () => {
      setSelected(new Set());
      invalidate();
    },
    onError: (err) => setActionError(err instanceof Error ? err.message : "打包失败"),
  });

  // -- upload --
  const handleUpload = async (files: FileList) => {
    setActionError(null);
    for (const file of Array.from(files)) {
      try {
        await uploadOneFile(file);
      } catch (err) {
        setActionError(err instanceof Error ? err.message : "上传失败");
        break;
      }
    }
    invalidate();
  };

  const uploadOneFile = async (file: File) => {
    const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
    setUploadProgress({ filename: file.name, sent: 0, total: file.size, state: "初始化" });

    const session: UploadSession = await api.uploadInit(user, {
      target_path: currentPath,
      filename: file.name,
      total_size: file.size,
      chunk_size: CHUNK_SIZE,
      auto_extract: file.name.endsWith(".tar.gz") || file.name.endsWith(".tgz"),
    });

    for (let i = 0; i < totalChunks; i++) {
      const start = i * CHUNK_SIZE;
      const end = Math.min(start + CHUNK_SIZE, file.size);
      const slice = file.slice(start, end);
      const buffer = await slice.arrayBuffer();
      const b64 = arrayBufferToBase64(buffer);
      setUploadProgress({
        filename: file.name,
        sent: end,
        total: file.size,
        state: `上传中 ${i + 1}/${totalChunks}`,
      });
      await api.uploadChunk(user, session.upload_id, i, b64);
    }

    setUploadProgress({ filename: file.name, sent: file.size, total: file.size, state: "校验完成中" });
    await api.uploadComplete(user, session.upload_id);
    setUploadProgress(null);
  };

  const entries: FileEntry[] = listing.data?.entries ?? [];
  const sortedEntries = [...entries].sort((a, b) => {
    if (a.kind === "directory" && b.kind !== "directory") return -1;
    if (a.kind !== "directory" && b.kind === "directory") return 1;
    return a.name.localeCompare(b.name);
  });

  return (
    <div className="page files-page">
      <SectionHeading
        eyebrow="文件系统"
        title="远程文件管理"
        detail="浏览、上传、下载和管理集群上的文件。"
        action={<RefreshButton onClick={invalidate} pending={listing.isFetching} />}
      />

      {/* Breadcrumb */}
      <nav className="files-breadcrumb" aria-label="路径导航">
        <button type="button" className="breadcrumb-home" onClick={() => navigateTo(homePath)} title="主目录">
          <Home size={14} aria-hidden="true" />
        </button>
        {pathSegments(currentPath).slice(1).map((seg) => (
          <span key={seg.path} className="breadcrumb-seg">
            <ChevronRight size={12} aria-hidden="true" />
            <button type="button" onClick={() => navigateTo(seg.path)}>{seg.label}</button>
          </span>
        ))}
      </nav>

      {/* Toolbar */}
      <div className="files-toolbar">
        <button
          type="button"
          className="button secondary"
          onClick={() => fileInputRef.current?.click()}
        >
          <Upload size={14} aria-hidden="true" /> 上传文件
        </button>
        <button
          type="button"
          className="button secondary"
          onClick={() => setMkdirOpen(true)}
        >
          <FolderPlus size={14} aria-hidden="true" /> 新建目录
        </button>
        {selected.size > 0 && (
          <>
            <button
              type="button"
              className="button secondary"
              onClick={() => archiveMutation.mutate([...selected])}
              disabled={archiveMutation.isPending}
            >
              <Archive size={14} aria-hidden="true" /> 打包 ({selected.size})
            </button>
            <button
              type="button"
              className="button danger"
              onClick={() => {
                if (window.confirm(`确认删除选中的 ${selected.size} 个项目？`)) {
                  // delete sequentially
                  const paths = [...selected];
                  const run = async () => {
                    for (const p of paths) await deleteMutation.mutateAsync(p);
                  };
                  void run();
                }
              }}
              disabled={deleteMutation.isPending}
            >
              <Trash2 size={14} aria-hidden="true" /> 删除 ({selected.size})
            </button>
          </>
        )}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files?.length) void handleUpload(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {/* mkdir inline form */}
      {mkdirOpen && (
        <form
          className="files-mkdir-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (mkdirName.trim()) mkdirMutation.mutate(mkdirName.trim());
          }}
        >
          <input
            autoFocus
            value={mkdirName}
            onChange={(e) => setMkdirName(e.target.value)}
            placeholder="目录名称"
            aria-label="新目录名称"
          />
          <button type="submit" className="button primary" disabled={mkdirMutation.isPending}>
            创建
          </button>
          <button type="button" className="button secondary" onClick={() => { setMkdirOpen(false); setMkdirName(""); }}>
            取消
          </button>
        </form>
      )}

      {/* Upload progress */}
      {uploadProgress && (
        <div className="files-upload-progress" role="status">
          <LoaderCircle size={14} className="spin" aria-hidden="true" />
          <span>{uploadProgress.filename}</span>
          <span>{uploadProgress.state}</span>
          <progress
            value={uploadProgress.sent}
            max={uploadProgress.total}
            aria-label="上传进度"
          />
          <span>{formatSize(uploadProgress.sent)} / {formatSize(uploadProgress.total)}</span>
        </div>
      )}

      {/* Error banner */}
      {actionError && (
        <div className="files-error" role="alert">
          <span>{actionError}</span>
          <button type="button" onClick={() => setActionError(null)}>✕</button>
        </div>
      )}

      {/* File listing */}
      <QueryBoundary
        pending={listing.isPending}
        error={listing.error}
        empty={sortedEntries.length === 0}
        emptyTitle="空目录"
        emptyDetail="此目录下没有文件或子目录。"
      >
        <table className="files-table">
          <thead>
            <tr>
              <th className="col-check" aria-label="选择" />
              <th>名称</th>
              <th className="col-size">大小</th>
              <th className="col-time">修改时间</th>
              <th className="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            {currentPath !== homePath && (
              <tr className="file-row parent-row" onClick={() => navigateTo(parentPath(currentPath))}>
                <td />
                <td colSpan={4}>
                  <Folder size={14} aria-hidden="true" />
                  <span>..</span>
                </td>
              </tr>
            )}
            {sortedEntries.map((entry) => (
              <tr
                key={entry.path}
                className={`file-row${selected.has(entry.path) ? " selected" : ""}`}
                onClick={() => {
                  if (entry.kind === "directory") navigateTo(entry.path);
                  else toggleSelect(entry.path);
                }}
              >
                <td className="col-check">
                  <input
                    type="checkbox"
                    checked={selected.has(entry.path)}
                    onChange={() => toggleSelect(entry.path)}
                    onClick={(e) => e.stopPropagation()}
                    aria-label={`选择 ${entry.name}`}
                  />
                </td>
                <td className="col-name">
                  <EntryIcon kind={entry.kind} />
                  <span className="entry-name" title={entry.path}>{entry.name}</span>
                </td>
                <td className="col-size">{entry.kind === "directory" ? "—" : formatSize(entry.size)}</td>
                <td className="col-time">{formatTime(entry.modified)}</td>
                <td className="col-actions">
                  {entry.kind === "file" && (
                    <button
                      type="button"
                      className="icon-button"
                      title="下载"
                      onClick={(e) => { e.stopPropagation(); void downloadFile(user, entry); }}
                    >
                      <Download size={14} aria-hidden="true" />
                    </button>
                  )}
                  <button
                    type="button"
                    className="icon-button danger"
                    title="删除"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (window.confirm(`确认删除 ${entry.name}？`)) deleteMutation.mutate(entry.path);
                    }}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </QueryBoundary>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function EntryIcon({ kind }: { kind: FileEntry["kind"] }) {
  if (kind === "directory") return <Folder size={14} aria-hidden="true" className="icon-folder" />;
  if (kind === "symlink") return <Link2 size={14} aria-hidden="true" className="icon-link" />;
  return <FileText size={14} aria-hidden="true" className="icon-file" />;
}

function formatTime(iso: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function downloadFile(user: string, entry: FileEntry) {
  // Chunked download: assemble base64 slices into a Blob
  const chunks: Uint8Array[] = [];
  let offset = 0;
  const READ_LEN = 1024 * 1024;
  let totalSize = entry.size;

  while (offset < totalSize || chunks.length === 0) {
    const resp = await api.fileContent(user, entry.path, offset, READ_LEN);
    totalSize = resp.size;
    const raw = atob(resp.data_b64);
    const arr = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    chunks.push(arr);
    offset += arr.length;
    if (arr.length === 0) break;
  }

  const blob = new Blob(chunks);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = entry.name;
  a.click();
  URL.revokeObjectURL(url);
}
