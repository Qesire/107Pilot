import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileDiff, FlaskConical, FolderGit2, Plus, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiRequestError } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import { useAgentChangeSetDiff, useAgentProject, useAgentProjects } from "./query";
import type { AgentProjectOrigin, FormalRunApproval, JsonObject, WorkspaceChangeSet } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";
import { AgentTaskPanel } from "./AgentTaskPanel";

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
          {detail.data ? (
            <ProjectReview
              view={detail.data}
              user={user}
              location={location}
              navigate={navigate}
            />
          ) : null}
        </QueryBoundary>
      </section>
    </div>
  );
}

function ProjectReview({
  view,
  user,
  location,
  navigate,
}: {
  view: NonNullable<ReturnType<typeof useAgentProject>["data"]>;
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}) {
  const queryClient = useQueryClient();
  const [partition, setPartition] = useState("debug");
  const [qos, setQos] = useState("normal");
  const [cpus, setCpus] = useState(1);
  const [memoryMib, setMemoryMib] = useState(1024);
  const [gpus, setGpus] = useState(0);
  const [gpuType, setGpuType] = useState("a100");
  const [walltimeSeconds, setWalltimeSeconds] = useState(300);
  const [publishConfirmed, setPublishConfirmed] = useState(false);
  const [publishTarget, setPublishTarget] = useState("");
  const [validationContractId, setValidationContractId] = useState("");
  const [validationRunId, setValidationRunId] = useState("");
  const [validationEvidenceRef, setValidationEvidenceRef] = useState("");
  const [formalWorkdir, setFormalWorkdir] = useState("");
  const [formalCommand, setFormalCommand] = useState("python main.py");
  const [formalApproval, setFormalApproval] = useState<FormalRunApproval | null>(null);
  const [formalConfirmed, setFormalConfirmed] = useState(false);
  const validationInputValid = isValidationEnvelopeInputValid({
    cpus,
    memoryMib,
    gpus,
    walltimeSeconds,
  });
  const remediationSessionId = location.search.get("repair_session");
  const sourceRunId = location.search.get("repair_run") ?? (
    view.project.origin === "failed_run" && typeof view.project.source?.ref_id === "string"
      ? view.project.source.ref_id
      : null
  );
  const taskSessionId = boundProjectSessionId(location.search, view.project.project_id);
  const startValidation = useMutation({
    mutationFn: async () => {
      const binding = projectAgentProfileBinding({
        origin: view.project.origin,
        projectId: view.project.project_id,
        workspaceId: view.workspace.workspace_id,
        sourceRunId,
        remediationSessionId,
      });
      const session = await api.createAgentSession(user, {
        profile: "campus-default",
        profile_id: binding.profile_id,
        request_key: `ui:builder-session:${crypto.randomUUID()}`,
        source: {
          ...binding.source,
          resource_envelope: buildValidationEnvelope({
            owner: user,
            snapshotDigest: view.workspace.snapshot.digest,
            partition,
            qos,
            cpus,
            memoryMib,
            gpus,
            gpuType,
            walltimeSeconds,
            now: new Date(),
          }),
        },
      });
      await api.createAgentTurn(user, session.session_id, {
        request_key: `ui:builder-turn:${crypto.randomUUID()}`,
        expected_state_version: session.state_version,
        message: view.project.origin === "failed_run"
          ? (
            "Review the bound failed Run evidence, diagnosis, Project, Workspace, ChangeSets, "
            + "and sandbox results. Repair only the diagnosed code in the isolated Workspace. "
            + "If Slurm validation is needed, schedule one validation within the approved "
            + "resource envelope, then end this Turn while the task runs."
          )
          : (
            "Review the bound Project, Workspace, Blueprint, ChangeSets, and sandbox results. "
            + "If Slurm validation is needed, schedule one validation within the approved "
            + "resource envelope, then end this Turn while the task runs."
          ),
      });
      return session;
    },
    onSuccess: (session) => navigate(withSearch("/agent", location.search, {
      mode: "conversation",
      project: view.project.project_id,
      session: session.session_id,
    })),
  });
  const [selectedChangeSet, setSelectedChangeSet] = useState<string | null>(
    view.change_sets[0]?.change_set_id ?? null,
  );
  useEffect(() => {
    setSelectedChangeSet(view.change_sets[0]?.change_set_id ?? null);
    setPublishConfirmed(false);
    setFormalApproval(null);
    setFormalConfirmed(false);
  }, [view.project.project_id, view.change_sets]);
  const diff = useAgentChangeSetDiff(
    user,
    view.project.project_id,
    view.workspace.workspace_id,
    selectedChangeSet,
  );
  const selected = view.change_sets.find((item) => item.change_set_id === selectedChangeSet) ?? null;
  const publishChangeSet = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error("请选择 ChangeSet");
      return api.publishAgentChangeSet(user, selected.change_set_id, {
        project_id: view.project.project_id,
        workspace_id: view.workspace.workspace_id,
        expected_version: selected.version,
        approved_digest: selected.digest,
        ...(view.project.origin === "blank" ? { target_root: publishTarget.trim() } : {}),
      });
    },
    onSuccess: () => {
      setPublishConfirmed(false);
      void queryClient.invalidateQueries({ queryKey: ["agent-project", user, view.project.project_id] });
      void queryClient.invalidateQueries({ queryKey: ["agent-projects", user] });
    },
  });
  const sessionId = location.search.get("session") ?? "";
  const formalInput = () => {
    if (!selected) throw new Error("请选择已发布的 ChangeSet");
    return {
      project_id: view.project.project_id,
      workspace_id: view.workspace.workspace_id,
      session_id: sessionId,
      validation_contract_id: validationContractId.trim(),
      validation_run_id: validationRunId.trim(),
      validation_evidence_refs: [validationEvidenceRef.trim()],
      formal_contract: buildFormalContract({
        name: view.project.goal,
        workdir: formalWorkdir,
        command: formalCommand,
        partition,
        qos,
        cpus,
        memoryMib,
        gpus,
        gpuType,
        walltimeSeconds,
      }),
    };
  };
  const previewFormalRun = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error("请选择已发布的 ChangeSet");
      return api.previewAgentFormalRun(user, selected.change_set_id, formalInput());
    },
    onSuccess: (approval) => {
      setFormalApproval(approval);
      setFormalConfirmed(false);
    },
  });
  const submitFormalRun = useMutation({
    mutationFn: () => {
      if (!selected || !formalApproval) throw new Error("请先生成正式运行审批摘要");
      return api.submitAgentFormalRun(user, selected.change_set_id, {
        ...formalInput(),
        approved_digest: formalApproval.approval_digest,
      });
    },
    onSuccess: () => setFormalConfirmed(false),
  });
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
          发布只会写入当前批准摘要覆盖的文件；远端摘要不匹配时将停止并报告冲突。
        </p>
      </section>
      {selected ? (
        <section className="agent-validation-approval" aria-labelledby="workspace-publish-heading">
          <div>
            <p className="panel-kicker">Explicit workspace approval</p>
            <h3 id="workspace-publish-heading">批准并发布所选 ChangeSet</h3>
            <p className="muted mono wrap-anywhere">digest {selected.digest}</p>
          </div>
          {view.project.origin === "blank" ? (
            <label>
              集群目标目录
              <input
                aria-label="发布目标目录"
                placeholder={`/public/home/${user}/project`}
                value={publishTarget}
                onChange={(event) => setPublishTarget(event.target.value)}
              />
            </label>
          ) : null}
          <label>
            <input
              type="checkbox"
              checked={publishConfirmed}
              onChange={(event) => setPublishConfirmed(event.target.checked)}
            />
            我确认批准以上精确 digest，并理解远端冲突不会被覆盖
          </label>
          <button
            type="button"
            className="button primary"
            disabled={
              publishChangeSet.isPending
              || !view.risk_summary.publish_available
              || !isChangeSetPublishable(selected)
              || !publishConfirmed
              || (view.project.origin === "blank" && !publishTarget.trim())
            }
            onClick={() => publishChangeSet.mutate()}
          >
            <ShieldCheck aria-hidden="true" size={15} />
            {publishChangeSet.isPending ? "正在发布" : "批准精确摘要并发布"}
          </button>
          {selected.state === "published" ? <p role="status">此 ChangeSet 已发布。</p> : null}
          {selected.state === "conflicted" ? <p role="alert">远端工作区已变化，未覆盖外部修改。</p> : null}
          {publishChangeSet.error ? <ProjectMutationError error={publishChangeSet.error} /> : null}
        </section>
      ) : null}
      {selected?.state === "published" ? (
        <section className="agent-validation-approval" aria-labelledby="formal-run-heading">
          <div>
            <p className="panel-kicker">Formal experiment approval</p>
            <h3 id="formal-run-heading">生成正式 Contract 并提交 Run</h3>
            <p className="muted">审批摘要同时绑定发布快照、验证 Evidence 和正式资源；提交时会重新运行 preflight。</p>
          </div>
          <div className="agent-validation-grid">
            <label>Agent Session ID<input value={sessionId} readOnly /></label>
            <label>验证 Contract ID<input value={validationContractId} onChange={(event) => { setValidationContractId(event.target.value); setFormalApproval(null); }} /></label>
            <label>验证 Run ID<input value={validationRunId} onChange={(event) => { setValidationRunId(event.target.value); setFormalApproval(null); }} /></label>
            <label>验证 Evidence ref<input value={validationEvidenceRef} onChange={(event) => { setValidationEvidenceRef(event.target.value); setFormalApproval(null); }} /></label>
            <label>正式工作目录<input value={formalWorkdir} placeholder={`/public/home/${user}/project`} onChange={(event) => { setFormalWorkdir(event.target.value); setFormalApproval(null); }} /></label>
            <label>正式命令<input value={formalCommand} onChange={(event) => { setFormalCommand(event.target.value); setFormalApproval(null); }} /></label>
          </div>
          <button
            type="button"
            className="button secondary"
            disabled={previewFormalRun.isPending || !sessionId || !validationContractId.trim() || !validationRunId.trim() || !validationEvidenceRef.trim() || !formalWorkdir.trim() || !formalCommand.trim() || !validationInputValid}
            onClick={() => previewFormalRun.mutate()}
          >
            {previewFormalRun.isPending ? "正在重算" : "生成精确审批摘要"}
          </button>
          {formalApproval ? (
            <>
              <p className="mono wrap-anywhere">approval {formalApproval.approval_digest}</p>
              <label><input type="checkbox" checked={formalConfirmed} onChange={(event) => setFormalConfirmed(event.target.checked)} />我批准该精确摘要对应的正式资源与提交</label>
              <button type="button" className="button primary" disabled={!formalConfirmed || submitFormalRun.isPending} onClick={() => submitFormalRun.mutate()}>
                {submitFormalRun.isPending ? "正在提交" : "批准并提交正式 Run"}
              </button>
            </>
          ) : null}
          {submitFormalRun.data ? <p role="status">正式 Run <a href={`/runs/${encodeURIComponent(submitFormalRun.data.run.run_id)}`}>{submitFormalRun.data.run.run_id}</a> 已提交，Job {submitFormalRun.data.run.job_id}；Runtime Watch 已建立。</p> : null}
          {previewFormalRun.error ? <ProjectMutationError error={previewFormalRun.error} /> : null}
          {submitFormalRun.error ? <ProjectMutationError error={submitFormalRun.error} /> : null}
        </section>
      ) : null}
      <section className="agent-validation-approval" aria-labelledby="validation-envelope-heading">
        <div>
          <p className="panel-kicker">Explicit validation approval</p>
          <h3 id="validation-envelope-heading">批准一次异步 Slurm 验证额度</h3>
          <p className="muted">额度绑定当前 snapshot，一小时后过期；不会授权正式实验提交或发布。</p>
        </div>
        <div className="agent-validation-grid">
          <label>Partition<input value={partition} onChange={(event) => setPartition(event.target.value)} /></label>
          <label>QoS<input value={qos} onChange={(event) => setQos(event.target.value)} /></label>
          <label>CPU<input type="number" min={1} value={cpus} onChange={(event) => setCpus(Number(event.target.value))} /></label>
          <label>内存 MiB<input type="number" min={1} value={memoryMib} onChange={(event) => setMemoryMib(Number(event.target.value))} /></label>
          <label>GPU<input type="number" min={0} value={gpus} onChange={(event) => setGpus(Number(event.target.value))} /></label>
          {gpus > 0 ? <label>GPU 类型<input value={gpuType} onChange={(event) => setGpuType(event.target.value)} /></label> : null}
          <label>Walltime 秒<input type="number" min={1} max={31_536_000} value={walltimeSeconds} onChange={(event) => setWalltimeSeconds(Number(event.target.value))} /></label>
        </div>
        <button
          type="button"
          className="button primary"
          disabled={
            startValidation.isPending
            || !partition.trim()
            || !qos.trim()
            || !validationInputValid
            || (view.project.origin === "failed_run" && (!remediationSessionId || !sourceRunId))
          }
          onClick={() => startValidation.mutate()}
        >
          <FlaskConical aria-hidden="true" size={15} />
          {startValidation.isPending ? "正在持久化批准" : "批准额度并启动验证 Agent"}
        </button>
        {startValidation.error ? <ProjectMutationError error={startValidation.error} /> : null}
      </section>
      {taskSessionId ? <AgentTaskPanel user={user} sessionId={taskSessionId} /> : null}
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
  return <div className="agent-mutation-error" role="alert"><strong>操作失败</strong><p>{error.message}</p>{code ? <small className="mono">{code}</small> : null}</div>;
}

export function originLabel(origin: AgentProjectOrigin): string {
  return ({ blank: "空白", existing: "现有目录", template: "模板", failed_run: "失败 Run" })[origin];
}

export function boundProjectSessionId(
  search: URLSearchParams,
  selectedProjectId: string,
): string | null {
  const projectId = search.get("project");
  const sessionId = search.get("session");
  return projectId === selectedProjectId && sessionId ? sessionId : null;
}

export function projectAgentProfileBinding(input: {
  origin: AgentProjectOrigin;
  projectId: string;
  workspaceId: string;
  sourceRunId?: string | null;
  remediationSessionId?: string | null;
}) {
  if (input.origin === "failed_run") {
    if (!input.sourceRunId || !input.remediationSessionId) {
      throw new Error("失败 Run 修复工程缺少已批准的 Remediation 绑定");
    }
    return {
      profile_id: "run_diagnosis_repair" as const,
      source: {
        project_id: input.projectId,
        workspace_id: input.workspaceId,
        run_id: input.sourceRunId,
        remediation_session_id: input.remediationSessionId,
      },
    };
  }
  return {
    profile_id: "experiment_builder" as const,
    source: {
      project_id: input.projectId,
      workspace_id: input.workspaceId,
    },
  };
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

export function isChangeSetPublishable(changeSet: WorkspaceChangeSet): boolean {
  return changeSet.state === "reviewable";
}

export function riskLabel(level: string): string {
  return level === "high" ? "高风险" : level === "medium" ? "中风险" : "低风险";
}

export function buildValidationEnvelope(input: {
  owner: string;
  snapshotDigest: string;
  partition: string;
  qos: string;
  cpus: number;
  memoryMib: number;
  gpus: number;
  gpuType?: string;
  walltimeSeconds: number;
  now: Date;
}) {
  return {
    partition: input.partition.trim(),
    qos: input.qos.trim(),
    cpus: input.cpus,
    memory_mib: input.memoryMib,
    gpu_type: input.gpus > 0 ? (input.gpuType?.trim() || "generic") : null,
    gpus: input.gpus,
    walltime_seconds: input.walltimeSeconds,
    max_tasks: 1,
    max_submissions: 1,
    workspace_snapshot_digest: input.snapshotDigest,
    expires_at: new Date(input.now.getTime() + 60 * 60 * 1000).toISOString(),
    approved_by: input.owner,
  };
}

export function buildFormalContract(input: {
  name: string;
  workdir: string;
  command: string;
  partition: string;
  qos: string;
  cpus: number;
  memoryMib: number;
  gpus: number;
  gpuType: string;
  walltimeSeconds: number;
}): JsonObject {
  const hours = Math.floor(input.walltimeSeconds / 3600);
  const minutes = Math.floor((input.walltimeSeconds % 3600) / 60);
  const seconds = input.walltimeSeconds % 60;
  return {
    recipe_version_id: "recipe_python_cpu@1.0.0",
    project: { name: input.name.slice(0, 128), workdir: input.workdir.trim() },
    entry: { command: input.command.trim(), expected_outputs: ["result.txt"] },
    resources: {
      partition: input.partition.trim(),
      qos: input.qos.trim(),
      nodes: 1,
      ntasks: 1,
      cpus_per_task: input.cpus,
      memory: `${input.memoryMib}M`,
      gpus_total: input.gpus,
      gpu_type: input.gpus > 0 ? input.gpuType.trim() : null,
      time_limit: [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":"),
    },
  };
}

export function isValidationEnvelopeInputValid(input: {
  cpus: number;
  memoryMib: number;
  gpus: number;
  walltimeSeconds: number;
}): boolean {
  return Number.isInteger(input.cpus)
    && input.cpus >= 1
    && input.cpus <= 1_048_576
    && Number.isInteger(input.memoryMib)
    && input.memoryMib >= 1
    && input.memoryMib <= Number.MAX_SAFE_INTEGER
    && Number.isInteger(input.gpus)
    && input.gpus >= 0
    && input.gpus <= 1_048_576
    && Number.isInteger(input.walltimeSeconds)
    && input.walltimeSeconds >= 1
    && input.walltimeSeconds <= 31_536_000;
}
