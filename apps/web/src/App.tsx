import { lazy, Suspense, useEffect, useState } from "react";
import {
  Blocks,
  Bot,
  Braces,
  ChevronDown,
  CircleUserRound,
  Command,
  FolderKanban,
  FolderOpen,
  LifeBuoy,
  ListTree,
  MoreHorizontal,
  Server,
  Settings,
  SquareTerminal,
} from "lucide-react";
import { StatusBadge } from "./components";
import { ConnectionActionBanner, ConnectionBadge } from "./ConnectionStatus";
import { AgentPage } from "./AgentPage";
import { EnvBoundaryBanner } from "./EnvBoundaryBanner";
import { useHealth, useWebSession } from "./query";
import { ClusterPage, NotFoundPage, RunsPage, TerminalCollaborationPage, WorkspacePage } from "./pages";
import { FilesPage } from "./FilesPage";
import { MarketItemDetailPage, MarketPage, TemplateDetailPage } from "./MarketPages";
import { globalNavigationPath, useLocationState, withSearch } from "./url";

const navigation = [
  { path: "/projects", label: "工作台", icon: FolderKanban },
  { path: "/runs", label: "作业", icon: ListTree },
  { path: "/files", label: "文件", icon: FolderOpen },
  { path: "/market", label: "作业市场", icon: Blocks },
  { path: "/studio/new", label: "Contract Studio", icon: Braces },
  { path: "/agent", label: "Agent", icon: Bot },
  { path: "/cluster", label: "集群", icon: Server },
  { path: "/terminal", label: "终端", icon: SquareTerminal },
];

const StudioPage = lazy(() =>
  import("./StudioPage").then((module) => ({ default: module.StudioPage })),
);

export default function App() {
  const [location, navigate] = useLocationState();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const requestedUser = location.search.get("user") || "alice";
  const normalizedRequestedUser = requestedUser === "bob" ? "bob" : "alice";
  const session = useWebSession(normalizedRequestedUser);
  const user = session.data?.user ?? normalizedRequestedUser;
  const health = useHealth(user);

  useEffect(() => {
    if (location.pathname === "/") navigate(withSearch("/projects", location.search, { user }), { replace: true });
    else if (session.isSuccess && requestedUser !== user) navigate(withSearch(location.pathname, location.search, { user }), { replace: true });
  }, [location.pathname, location.search, navigate, requestedUser, session.isSuccess, user]);

  const setUser = (nextUser: string) => {
    navigate(withSearch(location.pathname, location.search, { user: nextUser }), { replace: true });
  };
  const go = (path: string) => {
    setMobileNavOpen(false);
    navigate(globalNavigationPath(path, user));
  };
  const activePath = navigation.find((item) => location.pathname === item.path || location.pathname.startsWith(`${item.path}/`))?.path;

  return (
    <div className="product-shell">
      <EnvBoundaryBanner />
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className="app-sidebar">
        <button className="brand" type="button" onClick={() => go("/projects")} aria-label="107Pilot 工作台">
          <span className="brand-glyph"><Command aria-hidden="true" /></span>
          <span><strong>107Pilot</strong><small>计算工作台</small></span>
        </button>
        <nav className="primary-nav" aria-label="主要导航">
          {navigation.map((item) => {
            const Icon = item.icon;
            const active = item.path === activePath;
            return (
              <a
                key={item.path}
                href={globalNavigationPath(item.path, user)}
                className={active ? "active" : undefined}
                aria-label={item.label}
                aria-current={active ? "page" : undefined}
                onClick={(event) => { event.preventDefault(); go(item.path); }}
              >
                <Icon aria-hidden="true" />
                <span>{item.label}</span>
                {item.path === "/terminal" ? <small>安全协同</small> : null}
              </a>
            );
          })}
          <div className="mobile-more">
            <button
              type="button"
              className={`mobile-more-trigger${mobileNavOpen || navigation.slice(4).some((item) => item.path === activePath) ? " is-active" : ""}`}
              aria-expanded={mobileNavOpen}
              aria-controls="mobile-more-menu"
              onClick={() => setMobileNavOpen((open) => !open)}
            >
              <MoreHorizontal aria-hidden="true" />
              <span>更多</span>
            </button>
            {mobileNavOpen ? <div id="mobile-more-menu" className="mobile-more-menu">
              {navigation.slice(4).map((item) => {
                const Icon = item.icon;
                const active = item.path === activePath;
                return <a
                  key={item.path}
                  href={globalNavigationPath(item.path, user)}
                  className={active ? "active" : undefined}
                  aria-current={active ? "page" : undefined}
                  onClick={(event) => { event.preventDefault(); go(item.path); }}
                ><Icon aria-hidden="true" /><span>{item.label}</span></a>;
              })}
            </div> : null}
          </div>
        </nav>
        <div className="sidebar-bottom">
          <button type="button" disabled title="帮助中心将在后续切片接入"><LifeBuoy aria-hidden="true" /><span>帮助与文档</span></button>
          <button type="button" disabled title="设置将在后续切片接入"><Settings aria-hidden="true" /><span>设置</span></button>
          <p>Phase 3F · remediation workbench</p>
        </div>
      </aside>

      <div className="app-stage">
        <header className="app-topbar">
          <div className="breadcrumb">
            <span>107</span><span>/</span><strong>{navigation.find((item) => item.path === activePath)?.label ?? "页面"}</strong>
          </div>
          <div className="topbar-actions">
            <ConnectionBadge user={user} />
            <StatusBadge
              label={health.isPending ? "API 检查中" : health.isError ? "API 不可用" : "API ready"}
              tone={health.isPending ? "neutral" : health.isError ? "danger" : "success"}
            />
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
        <main id="main-content" className="page-transition" key={location.pathname} tabIndex={-1}>
          {session.isSuccess ? <ConnectionActionBanner user={user} /> : null}
          {session.isPending ? <div className="query-state" role="status"><span>正在确认当前身份…</span></div> : null}
          {session.isError ? <div className="query-state error" role="alert"><strong>身份不可用</strong><span>{session.error.message}</span></div> : null}
          {session.isSuccess ? <>
            {location.pathname === "/" || location.pathname === "/projects" ? <WorkspacePage user={user} location={location} navigate={navigate} /> : null}
            {location.pathname === "/runs" || location.pathname.startsWith("/runs/") ? <RunsPage user={user} location={location} navigate={navigate} /> : null}
            {location.pathname === "/files" ? <FilesPage user={user} location={location} navigate={navigate} /> : null}
            {location.pathname === "/cluster" ? <ClusterPage user={user} location={location} navigate={navigate} /> : null}
            {location.pathname === "/market" ? <MarketPage user={user} location={location} navigate={navigate} /> : null}
            {location.pathname.startsWith("/market/") ? <MarketItemDetailPage user={user} location={location} navigate={navigate} /> : null}
            {location.pathname.startsWith("/templates/") ? <TemplateDetailPage user={user} location={location} navigate={navigate} /> : null}
            {location.pathname.startsWith("/studio/") ? (
              <Suspense fallback={<div className="query-state" role="status"><span>正在加载 Contract Studio…</span></div>}>
                <StudioPage user={user} location={location} navigate={navigate} />
              </Suspense>
            ) : null}
            {location.pathname === "/agent" ? <AgentPage user={user} location={location} navigate={navigate} /> : null}
            {location.pathname === "/terminal" ? <TerminalCollaborationPage user={user} location={location} navigate={navigate} terminalDeepLink={session.data.terminal_deep_link} /> : null}
            {!isKnownPath(location.pathname) ? <NotFoundPage user={user} location={location} navigate={navigate} /> : null}
          </> : null}
        </main>
      </div>
    </div>
  );
}

function isKnownPath(pathname: string): boolean {
  return pathname === "/" || pathname === "/projects" || pathname === "/runs" || pathname.startsWith("/runs/") || pathname === "/files" || pathname === "/cluster" || pathname === "/agent" || pathname === "/terminal" || pathname.startsWith("/market") || pathname.startsWith("/templates/") || pathname.startsWith("/studio/");
}
