import { Clock3, Home, MapPin } from "lucide-react";
import { useFilesManager } from "./FilesManagerContext";

function relativeLabel(path: string, homePath: string): string {
  if (path === homePath) return "主目录";
  const relative = path.startsWith(`${homePath}/`) ? path.slice(homePath.length + 1) : path;
  const parts = relative.split("/").filter(Boolean);
  return parts.slice(-2).join(" / ") || path;
}

export function FileQuickAccess() {
  const manager = useFilesManager();
  return (
    <aside className="file-quick-access" aria-label="文件快捷访问">
      <div className="file-rail-heading">
        <span>快捷访问</span>
        <small>当前会话</small>
      </div>
      <nav>
        <button
          type="button"
          className={manager.activePath === manager.homePath ? "active" : undefined}
          onClick={() => manager.openPath(manager.homePath)}
        >
          <Home size={15} aria-hidden="true" />
          <span>主目录</span>
        </button>
      </nav>

      <div className="file-rail-section">
        <div className="file-rail-label">
          <Clock3 size={13} aria-hidden="true" />
          <span>本次位置</span>
        </div>
        {manager.sessionPaths.length === 0 ? (
          <p className="file-rail-empty">进入子目录后，这里会保留本次浏览位置。</p>
        ) : (
          <nav>
            {manager.sessionPaths.map((path) => (
              <button
                type="button"
                key={path}
                className={manager.activePath === path ? "active" : undefined}
                title={path}
                onClick={() => manager.openPath(path)}
              >
                <MapPin size={14} aria-hidden="true" />
                <span>{relativeLabel(path, manager.homePath)}</span>
              </button>
            ))}
          </nav>
        )}
      </div>
    </aside>
  );
}
