import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import {
  Blocks,
  Bot,
  ChevronDown,
  CircleUserRound,
  Command,
  FlaskConical,
  FolderKanban,
  FolderOpen,
  LifeBuoy,
  Monitor,
  Moon,
  MoreHorizontal,
  Server,
  Settings,
  SquareTerminal,
  Sun,
} from "lucide-react";
import { StatusBadge } from "./components";
import { ConnectionActionBanner, ConnectionBadge } from "./ConnectionStatus";
import { EnvBoundaryBanner } from "./EnvBoundaryBanner";
import { GlobalTransferIndicator } from "./GlobalTransferIndicator";
import { TransferManagerProvider } from "./TransferManager";
import { useHealth, useWebSession } from "./query";
import { WorkspacePageV2 } from "./WorkspacePageV2";
import { globalNavigationPath, useLocationState, withSearch } from "./url";

interface NavigationItem {
  path: string;
  label: string;
  icon: typeof FolderKanban;
  mobilePrimary?: boolean;
}

const workNavigation: NavigationItem[] = [
  { path: "/projects", label: "工作台", icon: FolderKanban, mobilePrimary: true },
  { path: "/runs", label: "实验", icon: FlaskConical, mobilePrimary: true },
  { path: "/files", label: "文件", icon: FolderOpen, mobilePrimary: true },
  { path: "/market", label: "方案库", icon: Blocks, mobilePrimary: true },
  { path: "/cluster", label: "计算资源", icon: Server },
];

const toolNavigation: NavigationItem[] = [
  { path: "/agent", label: "智能体", icon: Bot },
  { path: "/terminal", label: "终端", icon: SquareTerminal },
];

const navigation = [...workNavigation, ...toolNavigation];

const StudioPage = lazy(() => import("./StudioPage").then((module) => ({ default: module.StudioPage })));
const FilesPage = lazy(() => import("./FilesPage").then((module) => ({ default: module.FilesPage })));
const AgentPage = lazy(() => import("./AgentPage").then((module) => ({ default: module.AgentPage })));
const RunsPage = lazy(() => import("./pages").then((module) => ({ default: module.RunsPage })));
const ClusterPage = lazy(() => import("./pages").then((module) => ({ default: module.ClusterPage })));
const TerminalCollaborationPage = lazy(() => import("./pages").then((module) => ({ default: module.TerminalCollaborationPage })));
const MarketPage = lazy(() => import("./MarketPages").then((module) => ({ default: module.MarketPage })));
const MarketItemDetailPage = lazy(() => import("./MarketPages").then((module) => ({ default: module.MarketItemDetailPage })));
const TemplateDetailPage = lazy(() => import("./MarketPages").then((module) => ({ default: module.TemplateDetailPage })));
const TemplateWorkbenchPage = lazy(() => import("./TemplateWorkbenchPage").then((module) => ({ default: module.TemplateWorkbenchPage })));

type ThemePreference = "system" | "light" | "dark";

function initialThemePreference(): ThemePreference {
  const value = document.documentElement.dataset.themePreference;
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

function resolveTheme(preference: ThemePreference): "light" | "dark" {
  if (preference !== "system") return preference;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function nextTheme(preference: ThemePreference): ThemePreference {
  if (preference === "system") return "light";
  if (preference === "light") return "dark";
  return "system";
}

function themeLabel(preference: ThemePreference): string {
  if (preference === "light") return "浅色";
  if (preference === "dark") return "深色";
  return "跟随系统";
}

function ThemeIcon({ preference }: { preference: ThemePreference }) {
  if (preference === "light") return <Sun aria-hidden="true" />;
  if (preference === "dark") return <Moon aria-hidden="true" />;
  return <Monitor aria-hidden="true" />;
}

function canvasClass(pathname: string): "decision-canvas" | "workspace-canvas" {
  if (
    pathname === "/files"
    || pathname.startsWith("/runs/")
    || pathname.startsWith("/studio/")
    || pathname === "/agent"
    || pathname === "/terminal"
    || pathname.startsWith("/templates/draft/")
  ) return "workspace-canvas";
  return "decision-canvas";
}

function routeLabel(pathname: string): string {
  const item = navigation.find((entry) => pathname === entry.path || pathname.startsWith(`${entry.path}/`));
  if (item) return item.label;
  if (pathname.startsWith("/studio/")) return "实验配置";
  if (pathname.startsWith("/templates/")) return "方案库";
  return "科研工作区";
}

function RouteFallback({ label }: { label: string }) {
  return <div className="query-state" role="status"><span>正在加载{label}…</span></div>;
}

export default function App() {
  const [location, navigate] = useLocationState();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [themePreference, setThemePreference] = useState<ThemePreference>(initialThemePreference);
  const requestedUser = location.search.get("user") || "alice";
  const normalizedRequestedUser = requestedUser === "bob" ? "bob" : "alice";
  const session = useWebSession(normalizedRequestedUser);
  const user = session.data?.user ?? normalizedRequestedUser;
  const health = useHealth(user);

  useEffect(() => {
    if (location.pathname === "/") navigate(withSearch("/projects", location.search, { user }), { replace: true });
    else if (session.isSuccess && requestedUser !== user) navigate(withSearch(location.pathname, location.search, { user }), { replace: true });
  }, [location.pathname, location.search, navigate, requestedUser, session.isSuccess, user]);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const resolved = resolveTheme(themePreference);
      document.documentElement.dataset.theme = resolved;
      document.documentElement.dataset.themePreference = themePreference;
      localStorage.setItem("107pilot-theme", themePreference);
      const meta = document.querySelector('meta[name="theme-color"]');
      meta?.setAttribute("content", resolved === "dark" ? "#121418" : "#f6f7f9");
    };
    apply();
    if (themePreference !== "system") return undefined;
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [themePreference]);

  const activePath = useMemo(
    () => navigation.find((item) => location.pathname === item.path || location.pathname.startsWith(`${item.path}/`))?.path,
    [location.pathname],
  );
  const currentLabel = routeLabel(location.pathname);
  const mainClass = canvasClass(location.pathname);

  const setUser = (nextUser: string) => {
    navigate(withSearch(location.pathname, location.search, { user: nextUser }), { replace: true });
  };
  const go = (path: string) => {
    setMobileNavOpen(false);
    navigate(globalNavigationPath(path, user));
  };

  const renderNavigationItem = (item: NavigationItem, forceMobileHidden = false) => {
    const Icon = item.icon;
    const active = item.path === activePath;
    const mobileHidden = forceMobileHidden || !item.mobilePrimary;
    return (
      <a
        key={item.path}
        href={globalNavigationPath(item.path, user)}
        className={`${active ? "active" : ""}${mobileHidden ? " mobile-primary-hidden" : ""}`.trim() || undefined}
        aria-label={item.label}
        aria-current={active ? "page" : undefined}
        onClick={(event) => { event.preventDefault(); go(item.path); }}
      >
        <Icon aria-hidden="true" />
        <span>{item.label}</span>
        {item.path === "/terminal" ? <small>安全协同</small> : null}
      </a>
    );
  };

  const mobileMoreNavigation = [workNavigation[4]!, ...toolNavigation];
  const mobileMoreActive = mobileMoreNavigation.some((item) => item.path === activePath);

  return (
    <TransferManagerProvider key={user} user={user}>
      <div className="product-shell">
      <EnvBoundaryBanner />
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className="app-sidebar">
        <button className="brand" type="button" onClick={() => go("/projects")} aria-label="107Pilot 工作台">
          <span className="brand-glyph"><Command aria-hidden="true" /></span>
          <span><strong>107Pilot</strong><small>科研计算工作台</small></span>
        </button>

        <nav className="primary-nav" aria-label="主要导航">
          <div className="nav-group">
            <p className="nav-group-label">工作</p>
            {workNavigation.map((item) => renderNavigationItem(item))}
          </div>
          <div className="nav-group">
            <p className="nav-group-label">工具</p>
            {toolNavigation.map((item) => renderNavigationItem(item, true))}
          </div>

          <div className="mobile-more">
            <button
              type="button"
              className={`mobile-more-trigger${mobileNavOpen || mobileMoreActive ? " is-active" : ""}`}
              aria-expanded={mobileNavOpen}
              aria-controls="mobile-more-menu"
              onClick={() => setMobileNavOpen((open) => !open)}
            >
              <MoreHorizontal aria-hidden="true" />
              <span>更多</span>
            </button>
            {mobileNavOpen ? (
              <div id="mobile-more-menu" className="mobile-more-menu">
                {mobileMoreNavigation.map((item) => {
                  const Icon = item.icon;
                  const active = item.path === activePath;
                  return (
                    <a
                      key={item.path}
                      href={globalNavigationPath(item.path, user)}
                      className={active ? "active" : undefined}
                      aria-current={active ? "page" : undefined}
                      onClick={(event) => { event.preventDefault(); go(item.path); }}
                    >
                      <Icon aria-hidden="true" /><span>{item.label}</span>
                    </a>
                  );
                })}
              </div>
            ) : null}
          </div>
        </nav>

        <div className="sidebar-bottom">
          <button type="button" disabled title="帮助中心将在后续切片接入"><LifeBuoy aria-hidden="true" /><span>帮助与文档</span></button>
          <button type="button" disabled title="设置将在后续切片接入"><Settings aria-hidden="true" /><span>设置</span></button>
          <p>UX v2 foundation · compatibility mode</p>
        </div>
      </aside>

      <div className="app-stage">
        <header className="app-topbar">
          <div className="research-context">
            <strong>{currentLabel}</strong>
            <span>科研工作区</span>
          </div>
          <div className="topbar-actions">
            <ConnectionBadge user={user} />
            <GlobalTransferIndicator user={user} onOpen={() => go("/files")} />
            <StatusBadge
              label={health.isPending ? "系统检查中" : health.isError ? "系统异常" : "系统正常"}
              tone={health.isPending ? "neutral" : health.isError ? "danger" : "success"}
            />
            <button
              className="theme-switcher"
              type="button"
              aria-label={`外观：${themeLabel(themePreference)}。点击切换。`}
              title={`外观：${themeLabel(themePreference)}`}
              onClick={() => setThemePreference((current) => nextTheme(current))}
            >
              <ThemeIcon preference={themePreference} />
              <span>{themeLabel(themePreference)}</span>
            </button>
            <label className="user-switcher">
              <CircleUserRound aria-hidden="true" />
              <span className="sr-only">当前用户</span>
              <select
                value={user}
                disabled={!session.data?.switchable}
                title={session.data?.switchable ? "切换演示用户" : "身份由部署配置固定"}
                onChange={(event) => setUser(event.target.value)}
              >
                {session.data?.switchable ? <>
                  <option value="alice">alice</option>
                  <option value="bob">bob</option>
                </> : <option value={user}>{user}</option>}
              </select>
              <ChevronDown aria-hidden="true" />
            </label>
          </div>
        </header>

        <main id="main-content" className={`page-transition ${mainClass}`} key={location.pathname} tabIndex={-1}>
          {session.isSuccess ? <ConnectionActionBanner user={user} /> : null}
          {session.isPending ? <div className="query-state" role="status"><span>正在确认当前身份…</span></div> : null}
          {session.isError ? <div className="query-state error" role="alert"><strong>身份不可用</strong><span>{session.error.message}</span></div> : null}
          {session.isSuccess ? (
            <Suspense fallback={<RouteFallback label={currentLabel} />}>
              {location.pathname === "/" || location.pathname === "/projects" ? <WorkspacePageV2 user={user} location={location} navigate={navigate} /> : null}
              {location.pathname === "/runs" || location.pathname.startsWith("/runs/") ? <RunsPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname === "/files" ? <FilesPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname === "/cluster" ? <ClusterPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname === "/market" ? <MarketPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname.startsWith("/market/") ? <MarketItemDetailPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname === "/templates" || location.pathname === "/templates/new" || location.pathname === "/templates/reviews" || location.pathname.startsWith("/templates/draft/") ? <TemplateWorkbenchPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname.startsWith("/templates/") && !location.pathname.startsWith("/templates/draft/") ? <TemplateDetailPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname.startsWith("/studio/") ? <StudioPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname === "/agent" ? <AgentPage user={user} location={location} navigate={navigate} /> : null}
              {location.pathname === "/terminal" ? <TerminalCollaborationPage user={user} location={location} navigate={navigate} terminalDeepLink={session.data.terminal_deep_link} /> : null}
              {!isKnownPath(location.pathname) ? (
                <section className="query-state" role="status">
                  <strong>找不到这个页面</strong>
                  <span>当前地址不属于 107Pilot 已知工作区。</span>
                  <button className="button secondary" type="button" onClick={() => go("/projects")}>返回工作台</button>
                </section>
              ) : null}
            </Suspense>
          ) : null}
        </main>
      </div>
    </div>
    </TransferManagerProvider>
  );
}

function isKnownPath(pathname: string): boolean {
  return pathname === "/" || pathname === "/projects" || pathname === "/runs" || pathname.startsWith("/runs/") || pathname === "/files" || pathname === "/cluster" || pathname === "/agent" || pathname === "/terminal" || pathname.startsWith("/market") || pathname === "/templates" || pathname.startsWith("/templates/") || pathname.startsWith("/studio/");
}
