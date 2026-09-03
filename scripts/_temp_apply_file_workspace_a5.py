from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# FilesManagerContext: expose the active pane selection reactively and retain
# only session-local path history. No persistent/recent-file claim is made.
# ---------------------------------------------------------------------------
path = "apps/web/src/files/FilesManagerContext.tsx"
text = read(path)
text = replace_once(
    text,
    '''  activePaneId: string | null;\n  activePath: string;\n  setActivePane: (paneId: string) => void;\n''',
    '''  activePaneId: string | null;\n  activePath: string;\n  activeSelection: FileEntry[];\n  sessionPaths: string[];\n  setActivePane: (paneId: string) => void;\n  setPaneSelection: (paneId: string, entries: FileEntry[]) => void;\n''',
    "manager interface",
)
text = replace_once(
    text,
    '''  const [activePaneId, setActivePaneId] = useState<string | null>(null);\n  const [panePaths, setPanePaths] = useState<Record<string, string>>({});\n  const dragPayloadRef = useRef<DragPayload | null>(null);\n''',
    '''  const [activePaneId, setActivePaneId] = useState<string | null>(null);\n  const [panePaths, setPanePaths] = useState<Record<string, string>>({});\n  const [paneSelections, setPaneSelections] = useState<Record<string, FileEntry[]>>({});\n  const [sessionPaths, setSessionPaths] = useState<string[]>([]);\n  const dragPayloadRef = useRef<DragPayload | null>(null);\n''',
    "manager state",
)
text = replace_once(
    text,
    '''  const unregisterPane = useCallback((paneId: string) => {\n    controllers.current.delete(paneId);\n    setPanePaths((current) => {\n      const next = { ...current };\n      delete next[paneId];\n      return next;\n    });\n    setActivePaneId((prev) => (prev === paneId ? null : prev));\n  }, []);\n''',
    '''  const unregisterPane = useCallback((paneId: string) => {\n    controllers.current.delete(paneId);\n    setPanePaths((current) => {\n      const next = { ...current };\n      delete next[paneId];\n      return next;\n    });\n    setPaneSelections((current) => {\n      if (!(paneId in current)) return current;\n      const next = { ...current };\n      delete next[paneId];\n      return next;\n    });\n    setActivePaneId((prev) => (prev === paneId ? null : prev));\n  }, []);\n''',
    "unregister selection cleanup",
)
text = replace_once(
    text,
    '''  const setPanePath = useCallback((paneId: string, path: string) => {\n    setPanePaths((current) => current[paneId] === path\n      ? current\n      : { ...current, [paneId]: path });\n  }, []);\n''',
    '''  const setPanePath = useCallback((paneId: string, path: string) => {\n    setPanePaths((current) => current[paneId] === path\n      ? current\n      : { ...current, [paneId]: path });\n    if (path !== homePath) {\n      setSessionPaths((current) => [path, ...current.filter((item) => item !== path)].slice(0, 6));\n    }\n  }, [homePath]);\n\n  const setPaneSelection = useCallback((paneId: string, entries: FileEntry[]) => {\n    setPaneSelections((current) => {\n      const previous = current[paneId] ?? [];\n      const unchanged = previous.length === entries.length && previous.every((entry, index) => {\n        const next = entries[index];\n        return next !== undefined\n          && entry.path === next.path\n          && entry.kind === next.kind\n          && entry.size === next.size\n          && entry.modified === next.modified;\n      });\n      return unchanged ? current : { ...current, [paneId]: entries };\n    });\n  }, []);\n''',
    "reactive pane selection",
)
text = replace_once(
    text,
    '''  const activePath = activePaneId\n    ? panePaths[activePaneId] ?? controllers.current.get(activePaneId)?.getCwd() ?? homePath\n    : homePath;\n\n  const invalidateFilePaths = useCallback((paths: string[]) => {\n''',
    '''  const activePath = activePaneId\n    ? panePaths[activePaneId] ?? controllers.current.get(activePaneId)?.getCwd() ?? homePath\n    : homePath;\n  const activeSelection = activePaneId ? paneSelections[activePaneId] ?? [] : [];\n\n  const invalidateFilePaths = useCallback((paths: string[]) => {\n''',
    "active selection derivation",
)
text = replace_once(
    text,
    '''      activePaneId,\n      activePath,\n      setActivePane,\n      registerPane,\n''',
    '''      activePaneId,\n      activePath,\n      activeSelection,\n      sessionPaths,\n      setActivePane,\n      setPaneSelection,\n      registerPane,\n''',
    "manager value fields",
)
text = replace_once(
    text,
    '''      activePaneId,\n      activePath,\n      setActivePane,\n      registerPane,\n''',
    '''      activePaneId,\n      activePath,\n      activeSelection,\n      sessionPaths,\n      setActivePane,\n      setPaneSelection,\n      registerPane,\n''',
    "manager memo dependencies",
)
write(path, text)

# ---------------------------------------------------------------------------
# useFilePane: publish the already-computed selectedEntries into the manager.
# ---------------------------------------------------------------------------
path = "apps/web/src/files/useFilePane.ts"
text = read(path)
text = replace_once(
    text,
    '''  const selectedEntries = useMemo(\n    () => entries.filter((entry) => selectedSet.has(entry.path)),\n    [entries, selectedSet],\n  );\n\n  useEffect(() => {\n    manager.setPanePath(paneId, cwd);\n  }, [cwd, manager, paneId]);\n''',
    '''  const selectedEntries = useMemo(\n    () => entries.filter((entry) => selectedSet.has(entry.path)),\n    [entries, selectedSet],\n  );\n\n  useEffect(() => {\n    manager.setPaneSelection(paneId, selectedEntries);\n  }, [manager.setPaneSelection, paneId, selectedEntries]);\n\n  useEffect(() => {\n    manager.setPanePath(paneId, cwd);\n  }, [cwd, manager.setPanePath, paneId]);\n''',
    "publish active selection",
)
write(path, text)

# ---------------------------------------------------------------------------
# New workspace rails.
# ---------------------------------------------------------------------------
write(
    "apps/web/src/files/FileQuickAccess.tsx",
    r'''import { Clock3, Home, MapPin } from "lucide-react";
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
''',
)

write(
    "apps/web/src/files/FileInspector.tsx",
    r'''import { Copy, File, Folder, Link2 } from "lucide-react";
import type { FileEntry } from "../types";
import { useFilesManager } from "./FilesManagerContext";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

function kindLabel(kind: FileEntry["kind"]): string {
  if (kind === "directory") return "目录";
  if (kind === "symlink") return "符号链接";
  return "文件";
}

function EntryIcon({ entry }: { entry: FileEntry }) {
  if (entry.kind === "directory") return <Folder size={18} aria-hidden="true" />;
  if (entry.kind === "symlink") return <Link2 size={18} aria-hidden="true" />;
  return <File size={18} aria-hidden="true" />;
}

function CopyPathButton({ path }: { path: string }) {
  return (
    <button
      type="button"
      className="file-inspector-copy"
      title="复制路径"
      aria-label="复制路径"
      onClick={() => { void navigator.clipboard?.writeText(path); }}
    >
      <Copy size={14} aria-hidden="true" />
    </button>
  );
}

export function FileInspector() {
  const manager = useFilesManager();
  const selected = manager.activeSelection;

  if (selected.length === 0) {
    return (
      <aside className="file-inspector" aria-label="文件属性">
        <div className="file-rail-heading">
          <span>属性</span>
          <small>当前目录</small>
        </div>
        <dl className="file-inspector-list">
          <div>
            <dt>位置</dt>
            <dd className="file-inspector-path">
              <span title={manager.activePath}>{manager.activePath}</span>
              <CopyPathButton path={manager.activePath} />
            </dd>
          </div>
        </dl>
        <p className="file-inspector-hint">选择文件或目录以查看属性。运行来源与实验关联将在资产链接阶段接入。</p>
      </aside>
    );
  }

  if (selected.length > 1) {
    const fileBytes = selected.reduce((total, entry) => total + (entry.kind === "file" ? entry.size : 0), 0);
    return (
      <aside className="file-inspector" aria-label="文件属性">
        <div className="file-rail-heading">
          <span>属性</span>
          <small>多选</small>
        </div>
        <div className="file-inspector-summary">
          <strong>{selected.length} 项已选</strong>
          <span>文件合计 {formatSize(fileBytes)}</span>
        </div>
        <p className="file-inspector-hint">批量操作仍从当前窗格的选择操作栏执行。</p>
      </aside>
    );
  }

  const entry = selected[0];
  return (
    <aside className="file-inspector" aria-label="文件属性">
      <div className="file-rail-heading">
        <span>属性</span>
        <small>{kindLabel(entry.kind)}</small>
      </div>
      <div className="file-inspector-entry">
        <EntryIcon entry={entry} />
        <strong title={entry.name}>{entry.name}</strong>
      </div>
      <dl className="file-inspector-list">
        <div><dt>类型</dt><dd>{kindLabel(entry.kind)}</dd></div>
        <div><dt>大小</dt><dd>{entry.kind === "file" ? formatSize(entry.size) : "—"}</dd></div>
        <div><dt>修改时间</dt><dd>{entry.modified || "—"}</dd></div>
        <div>
          <dt>路径</dt>
          <dd className="file-inspector-path">
            <span title={entry.path}>{entry.path}</span>
            <CopyPathButton path={entry.path} />
          </dd>
        </div>
      </dl>
    </aside>
  );
}
''',
)

write(
    "apps/web/src/files/file-workspace-shell.css",
    r'''.file-workspace-shell {
  display: grid;
  grid-template-columns: 184px minmax(0, 1fr) 248px;
  min-width: 0;
  gap: 12px;
  align-items: stretch;
}

.file-workspace-main { min-width: 0; }

.file-quick-access,
.file-inspector {
  min-width: 0;
  height: calc(100vh - 340px);
  min-height: 480px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  padding: 12px;
}

.file-rail-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 3px 10px;
  border-bottom: 1px solid var(--line);
}
.file-rail-heading span { color: var(--ink); font-size: 12px; font-weight: 800; }
.file-rail-heading small { color: var(--ink-faint); font-size: 9px; }

.file-quick-access nav { display: grid; gap: 3px; margin-top: 10px; }
.file-quick-access nav button {
  display: grid;
  width: 100%;
  min-height: 34px;
  grid-template-columns: 16px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--ink-soft);
  padding: 0 8px;
  text-align: left;
  cursor: pointer;
  font-size: 11px;
}
.file-quick-access nav button:hover { background: var(--surface-soft); color: var(--ink); }
.file-quick-access nav button.active { background: var(--teal-soft); color: #116451; }
.file-quick-access nav button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-rail-section { margin-top: 18px; }
.file-rail-label { display: flex; align-items: center; gap: 6px; padding: 0 4px; color: var(--ink-faint); font-size: 9px; font-weight: 750; }
.file-rail-empty { margin: 10px 4px 0; color: var(--ink-faint); font-size: 10px; line-height: 1.55; }

.file-inspector-entry {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  margin: 14px 2px 4px;
  color: var(--teal);
}
.file-inspector-entry strong { overflow: hidden; color: var(--ink); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.file-inspector-list { display: grid; gap: 0; margin: 12px 0 0; }
.file-inspector-list > div { display: grid; gap: 5px; padding: 10px 2px; border-top: 1px solid var(--line); }
.file-inspector-list dt { color: var(--ink-faint); font-size: 9px; font-weight: 750; }
.file-inspector-list dd { min-width: 0; margin: 0; color: var(--ink-soft); font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
.file-inspector-path { display: grid; grid-template-columns: minmax(0, 1fr) 26px; align-items: start; gap: 5px; }
.file-inspector-path span { min-width: 0; overflow-wrap: anywhere; }
.file-inspector-copy { display: grid; width: 26px; height: 26px; place-items: center; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--ink-faint); cursor: pointer; }
.file-inspector-copy:hover { background: var(--surface-soft); color: var(--ink); }
.file-inspector-hint { margin: 14px 2px 0; color: var(--ink-faint); font-size: 9px; line-height: 1.65; }
.file-inspector-summary { display: grid; gap: 4px; margin: 16px 2px 0; }
.file-inspector-summary strong { font-size: 13px; }
.file-inspector-summary span { color: var(--ink-soft); font-size: 10px; }

@media (max-width: 1199px) {
  .file-workspace-shell { grid-template-columns: 164px minmax(0, 1fr) 220px; gap: 10px; }
}

@media (max-width: 999px) {
  .file-workspace-shell { display: block; }
  .file-quick-access,
  .file-inspector { display: none; }
}
''',
)

# ---------------------------------------------------------------------------
# FilesPage: place the existing PaneManager between the two new rails.
# ---------------------------------------------------------------------------
path = "apps/web/src/FilesPage.tsx"
text = read(path)
text = replace_once(
    text,
    '''import { FileSearchPanel, fileSearchOpenTarget } from "./files/FileSearchPanel";\nimport { FileWorkspaceStatus } from "./files/FileWorkspaceStatus";\nimport { PaneManager } from "./files/PaneManager";\n''',
    '''import { FileInspector } from "./files/FileInspector";\nimport { FileQuickAccess } from "./files/FileQuickAccess";\nimport { FileSearchPanel, fileSearchOpenTarget } from "./files/FileSearchPanel";\nimport { FileWorkspaceStatus } from "./files/FileWorkspaceStatus";\nimport { PaneManager } from "./files/PaneManager";\nimport "./files/file-workspace-shell.css";\n''',
    "FilesPage imports",
)
text = replace_once(
    text,
    '''      <PaneManager layoutApi={layoutApi} homePath={homePath} />\n''',
    '''      <div className="file-workspace-shell">\n        <FileQuickAccess />\n        <div className="file-workspace-main">\n          <PaneManager layoutApi={layoutApi} homePath={homePath} />\n        </div>\n        <FileInspector />\n      </div>\n''',
    "FilesPage workspace shell",
)
write(path, text)

# ---------------------------------------------------------------------------
# Dedicated browser coverage: rails, reactive inspector, quick return home,
# and mobile collapse. Keep all existing file behavior tests intact.
# ---------------------------------------------------------------------------
path = "tests/ui/files.spec.js"
text = read(path)
addition = r'''

test("desktop file workspace exposes quick access and a reactive inspector", async ({ page }) => {
  await page.goto("/files?user=alice");
  const pane = page.locator(".file-pane").first();

  await expect(page.getByRole("complementary", { name: "文件快捷访问" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "文件属性" })).toBeVisible();

  await pane.locator(".file-row", { hasText: "readme.md" }).click();
  const inspector = page.getByRole("complementary", { name: "文件属性" });
  await expect(inspector.getByText("readme.md", { exact: true })).toBeVisible();
  await expect(inspector.getByText("2.0 KiB", { exact: true })).toBeVisible();
  await expect(inspector).toContainText(`${HOME}/readme.md`);
});

test("quick access returns the active pane home and retains session locations", async ({ page }) => {
  await page.goto("/files?user=alice");
  const pane = page.locator(".file-pane").first();
  await pane.locator(".file-row", { hasText: "docs" }).dblclick();
  await expect(pane).toHaveAttribute("data-pane-cwd", `${HOME}/docs`);

  const quick = page.getByRole("complementary", { name: "文件快捷访问" });
  await expect(quick.getByRole("button", { name: /docs/ })).toBeVisible();
  await quick.getByRole("button", { name: "主目录" }).click();
  await expect(pane).toHaveAttribute("data-pane-cwd", HOME);
  await expect(pane.locator(".file-row", { hasText: "readme.md" })).toBeVisible();
});

test("file workspace rails collapse below desktop width without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/files?user=alice");

  await expect(page.locator(".file-quick-access")).toBeHidden();
  await expect(page.locator(".file-inspector")).toBeHidden();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflow).toBe(false);
});
'''
if "desktop file workspace exposes quick access" in text:
    raise RuntimeError("A5 file workspace tests already staged")
text += addition
write(path, text)

print("A5 file workspace shell staged")
