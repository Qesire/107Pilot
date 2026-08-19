import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileDiff, FlaskConical, FolderGit2, Plus, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiRequestError } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import { useAgentChangeSetDiff, useAgentProject, useAgentProjects } from "./query";
import type { AgentProjectOrigin, WorkspaceChangeSet } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

interface AgentProjectPanelProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

export function AgentProjectPanel({ user, location, navigate }: AgentProjectPanelProps) {
  const queryClient = useQueryClient();
  const projects = useAgentProjects(user);
  const requestedProject = location.search.get("project");
  const selectedId = requestedProject ?? projects.data?.items[0]?.project.project_id ?? null;
  const detail = useAgentProject(user, selectedId);
  const [origin, setOrigin] = useState<"blank" | "existing">("blank");
  const [goal, setGoal] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [requestKey, setRequestKey] = useState<string | null>(null);
  const createProject = useMutation({
    mutationFn: () => {
      const stableKey = requestKey ?? `ui:agent-project:${crypto.randomUUID()}`;
      if (!requestKey) setRequestKey(stableKey);
      return api.createAgentProject(user, {
        origin,
        goal: goal.trim(),
        request_key: stableKey,
        ...(origin === "existing" ? { source_ref: sourceRef.trim() } : {}),
      });
    },
    onSuccess: (view) => {
      setGoal("");
      setSourceRef("");
      setRequestKey(null);
      void queryClient.invalidateQueries({ queryKey: ["agent-projects", user] });
      navigate(withSearch("/agent", location.search, {
        mode: "builder",
        project: view.project.project_id,
      }));
    },
  });
  const selectProject = (projectId: string) => navigate(withSearch(
    "/agent",
    location.search,
    { mode: "builder", project: projectId },
  ));

  return (
    <div className="agent-layout agent-project-layout">
      <section className="panel agent-queue" aria-labelledby="project-queue-heading">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">Isolated projects</p>
            <h2 id="project-queue-heading">{projects.data?.items.length ?? 0} 个工程</h2>
          </div>
        </div>
        <div className="agent-readonly-note">
          <ShieldCheck aria-hidden="true" size={17} />
          <p><strong>隔离编辑</strong>模型只修改应用侧 Workspace；集群真源、发布和 Slurm 提交均不在本阶段授权内。</p>
        </div>
        <form
          className="agent-new-session"
          onSubmit={(event) => {
            event.preventDefault();
            if (goal.trim() && (origin === "blank" || sourceRef.trim())) createProject.mutate();
          }}
        >
          <label className="select-field">
            <FolderGit2 aria-hidden="true" size={16} />
            <span className="sr-only">工程来源</span>
            <select
              aria-label="工程来源"
              value={origin}
              onChange={(event) => {
                setOrigin(event.target.value as "blank" | "existing");
                setRequestKey(null);
              }}
            >
              <option value="blank">空白工程</option>
              <option value="existing">现有集群目录</option>
            </select>
          </label>
          <textarea
            aria-label="工程目标"
            rows={3}
            maxLength={64_000}
            value={goal}
            placeholder="描述实验目标、输入、输出和验证要求…"
            onChange={(event) => {
              setGoal(event.target.value);
              setRequestKey(null);
            }}
          />
          {origin === "existing" ? (
            <input
              aria-label="集群源目录"
              value={sourceRef}
              placeholder="/public/home/alice/project"
              onChange={(event) => {
                setSourceRef(event.target.value);
                setRequestKey(null);
              }}
            />
          ) : null}
          <button
            className="button primary"
            type="submit"
            disabled={createProject.isPending || !goal.trim() || (origin === "existing" && !sourceRef.trim())}
          >
            <Plus aria-hidden="true" size={15} />
            {createProject.isPending ? "正在创建" : "创建隔离工程"}
          </button>
          {createProject.error ? <ProjectMutationError error={createProject.error} /> : null}
        </form>
        <QueryBoundary
          pending={projects.isPending}
          error={projects.error}
          empty={(projects.data?.items.length ?? 0) === 0}
          emptyTitle="还没有实验工程"
          emptyDetail="从空白工程开始，或以只读快照导入现有集群目录。"
        >
          <div className="agent-session-list">
            {(projects.data?.items ?? []).map((view) => (
              <button
                key={view.project.project_id}
                type="button"
                className={view.project.project_id === selectedId ? "active" : undefined}
                onClick={() => selectProject(view.project.project_id)}
              >
                <span>
                  <StatusBadge label={projectStateLabel(view.project.state)} tone="info" />
                  <small>{formatTimestamp(view.project.updated_at)}</small>
                </span>
                <strong>{view.project.goal}</strong>
                <small>{originLabel(view.project.origin)} · {view.change_sets.length} ChangeSet</small>
              </button>
            ))}
          </div>
        </QueryBoundary>
      </section>
      <section className="panel agent-detail" aria-labelledby="project-detail-heading">
        <div className="panel-heading">
          <div><p className="panel-kicker">Review workspace</p><h2 id="project-detail-heading">Blueprint 与变更审阅</h2></div>
        </div>
        <QueryBoundary
          pending={Boolean(selectedId) && detail.isPending}
          error={detail.error}
          empty={!selectedId}
          emptyTitle="选择一个工程"
          emptyDetail="这里展示 Blueprint、变更文件、统一 diff、Sandbox 结果与风险摘要。"
        >
          {detail.data ? <ProjectReview view={detail.data} user={user} /> : null}
        </QueryBoundary>
      </section>
    </div>
  );
}

function ProjectReview({ view, user }: { view: NonNullable<ReturnType<typeof useAgentProject>["data"]>; user: string }) {
  const [selectedChangeSet, setSelectedChangeSet] = useState<string | null>(
    view.change_sets[0]?.change_set_id ?? null,
  );
  useEffect(() => {
    setSelectedChangeSet(view.change_sets[0]?.change_set_id ?? null);
  }, [view.project.project_id, view.change_sets]);
  const diff = useAgentChangeSetDiff(
    user,
    view.project.project_id,
    view.workspace.workspace_id,
    selectedChangeSet,
  );
  const selected = view.change_sets.find((item) => item.change_set_id === selectedChangeSet) ?? null;
  const blueprint = view.project.blueprint;
  return (
    <div className="agent-conversation">
      <header className="agent-conversation-heading">
        <div>
          <StatusBadge label={projectStateLabel(view.project.state)} tone="info" />
          <p className="mono wrap-anywhere">{view.project.project_id}</p>
        </div>
        <small className="mono">snapshot {view.workspace.snapshot.digest.slice(0, 12)}</small>
      </header>
      <section className="agent-readonly-note" aria-label="风险摘要">
        <FlaskConical aria-hidden="true" size={17} />
        <p>
          <strong>{riskLabel(view.risk_summary.level)}</strong>
          {view.risk_summary.changed_files} 个文件变更，{view.risk_summary.sandbox_failures} 次 Sandbox 失败。
          发布入口尚未开放。
        </p>
      </section>
      <section>
        <h3>Blueprint</h3>
        {blueprint ? (
          <div className="detail-grid">
            <div><span>目标</span><strong>{blueprint.goal}</strong></div>
            <div><span>入口</span><strong className="mono">{blueprint.entrypoints.join(", ") || "—"}</strong></div>
            <div><span>验证</span><strong>{blueprint.validations.length}</strong></div>
            <div><span>未决问题</span><strong>{blueprint.open_questions.length}</strong></div>
          </div>
        ) : <p className="muted">Agent 尚未保存 Blueprint。</p>}
      </section>
      <section>
        <h3>ChangeSets</h3>
        {view.change_sets.length ? (
          <div className="agent-session-list">
            {view.change_sets.map((item) => (
              <button
                type="button"
                key={item.change_set_id}
                className={item.change_set_id === selectedChangeSet ? "active" : undefined}
                onClick={() => setSelectedChangeSet(item.change_set_id)}
              >
                <span>
                  <StatusBadge label={changeSetStateLabel(item.state)} tone={changeSetTone(item)} />
                  <small>{formatTimestamp(item.updated_at)}</small>
                </span>
                <strong className="mono wrap-anywhere">{item.change_set_id}</strong>
                <small>{item.files.map((file) => `${file.operation} ${file.path}`).join(" · ")}</small>
              </button>
            ))}
          </div>
        ) : <p className="muted">暂无文件变更。</p>}
      </section>
      {selected ? (
        <section className="agent-event-stream" aria-label="ChangeSet 审阅">
          <article className="agent-event">
            <div className="agent-event-sequence"><FileDiff aria-hidden="true" size={18} /></div>
            <div>
              <header><strong>Unified diff</strong><small>{selected.files.length} files</small></header>
              {diff.isPending ? <p>正在加载 diff…</p> : null}
              {diff.error ? <p role="alert">{diff.error.message}</p> : null}
              {diff.data ? <pre><code>{diff.data.unified_diff || "（无文本差异）"}</code></pre> : null}
            </div>
          </article>
          {selected.sandbox_results.map((result) => (
            <article className="agent-event" key={result.result_id}>
              <div className="agent-event-sequence"><FlaskConical aria-hidden="true" size={18} /></div>
              <div>
                <header><strong>Sandbox {result.status}</strong><small>exit {result.exit_code ?? "—"}</small></header>
                <p className="mono wrap-anywhere">{result.argv.join(" ")}</p>
              </div>
            </article>
          ))}
        </section>
      ) : null}
    </div>
  );
}

function ProjectMutationError({ error }: { error: Error }) {
  const code = error instanceof ApiRequestError ? error.code : null;
  return <div className="agent-mutation-error" role="alert"><strong>无法创建工程</strong><p>{error.message}</p>{code ? <small className="mono">{code}</small> : null}</div>;
}

export function originLabel(origin: AgentProjectOrigin): string {
  return ({ blank: "空白", existing: "现有目录", template: "模板", failed_run: "失败 Run" })[origin];
}

export function projectStateLabel(state: string): string {
  return ({
    drafting: "规划中",
    editing: "编辑中",
    validating: "验证中",
    awaiting_approval: "等待审批",
    publishing: "发布中",
    ready: "已就绪",
    blocked: "需要接管",
    cancelled: "已取消",
  } as Record<string, string>)[state] ?? state;
}

export function changeSetStateLabel(state: WorkspaceChangeSet["state"]): string {
  return ({
    draft: "草稿",
    reviewable: "可审阅",
    approved: "已批准",
    publishing: "发布中",
    published: "已发布",
    conflicted: "冲突",
    failed: "验证失败",
    cancelled: "已取消",
  })[state];
}

export function changeSetTone(changeSet: WorkspaceChangeSet): "success" | "warning" | "danger" | "neutral" {
  if (changeSet.state === "reviewable" || changeSet.state === "published") return "success";
  if (changeSet.state === "failed" || changeSet.state === "conflicted") return "danger";
  if (changeSet.state === "draft" || changeSet.state === "approved") return "warning";
  return "neutral";
}

export function riskLabel(level: string): string {
  return level === "high" ? "高风险" : level === "medium" ? "中风险" : "低风险";
}
