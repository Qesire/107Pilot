import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  FilePlus2,
  FolderOpen,
  Plus,
  RefreshCw,
  Rocket,
  ShieldCheck,
} from "lucide-react";
import { useState } from "react";
import { api } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import { FilePickerDialog } from "./files/FilePickerDialog";
import "./files/file-workspace-shell.css";
import type { LocationState } from "./url";
import {
  workareaApi,
  type LaunchPreflight,
  type LaunchRecord,
  type WorkAreaDetail,
} from "./workarea-api";

interface PageProps {
  user: string;
  location: LocationState;
  navigate: (path: string, options?: { replace?: boolean }) => void;
}

type BindingSection = "assets" | "runs" | "contracts";

type FlowStage = 0 | 1 | 2 | 3;

export function WorkAreaPages(props: PageProps) {
  const path = props.location.pathname;
  if (path === "/workareas") return <WorkAreaIndex {...props} />;
  if (path.startsWith("/launches/") && path.endsWith("/review")) {
    const id = decodeURIComponent(path.slice("/launches/".length, -"/review".length));
    return <LaunchReviewPage {...props} candidateId={id} />;
  }
  if (path.startsWith("/launches/")) {
    const id = decodeURIComponent(path.slice("/launches/".length));
    return <LaunchDetailPage {...props} launchId={id} />;
  }
  if (path.startsWith("/workareas/") && path.endsWith("/launch/new")) {
    const id = decodeURIComponent(path.slice("/workareas/".length, -"/launch/new".length));
    return <NewLaunchPage {...props} workareaId={id} />;
  }
  if (path.startsWith("/workareas/")) {
    const id = decodeURIComponent(path.slice("/workareas/".length));
    return <WorkAreaDetailPage {...props} workareaId={id} />;
  }
  return null;
}

function WorkAreaIndex({ user, navigate }: PageProps) {
  const queryClient = useQueryClient();
  const areas = useQuery({
    queryKey: ["workareas", user],
    queryFn: ({ signal }) => workareaApi.list(user, signal),
  });
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");

  const create = useMutation({
    mutationFn: () => workareaApi.create(user, {
      title: title.trim(),
      description: description.trim(),
      request_key: `web-workarea-${crypto.randomUUID()}`,
    }),
    onSuccess: async (record) => {
      await queryClient.invalidateQueries({ queryKey: ["workareas", user] });
      navigate(`/workareas/${encodeURIComponent(record.workarea_id)}?user=${encodeURIComponent(user)}`);
    },
  });

  return <div className="workarea-v2">
    <PageHeader
      overline="研究上下文"
      title="研究区"
      detail="按科研任务组织代码、数据、配置和运行历史。进入研究区后，系统会保持同一上下文并引导下一步。"
      actions={<button className="button primary" type="button" onClick={() => setOpen(true)}>
        <Plus aria-hidden="true" size={15} /> 新建研究区
      </button>}
    />

    {open ? <section className="wa-focus-surface wa-editor" aria-label="创建研究区">
      <div className="wa-editor-copy">
        <h2>创建研究区</h2>
        <p>这里只定义研究上下文。文件、Contract 和 Run 仍保留各自的权威记录。</p>
      </div>
      <div className="wa-form-grid">
        <label className="form-field">
          <span>名称</span>
          <input autoFocus value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：Wan FFN 稀疏实验" />
        </label>
        <label className="form-field">
          <span>说明</span>
          <textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="一句话说明这组研究工作的目标" />
        </label>
      </div>
      {create.isError ? <p className="wa-error" role="alert">{create.error.message}</p> : null}
      <div className="wa-form-actions">
        <button className="button secondary" type="button" disabled={create.isPending} onClick={() => setOpen(false)}>取消</button>
        <button className="button primary" type="button" disabled={!title.trim() || create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? "创建中" : "创建并进入"}
        </button>
      </div>
    </section> : null}

    <QueryBoundary
      pending={areas.isPending}
      error={areas.error}
      empty={(areas.data?.items.length ?? 0) === 0}
      emptyTitle="还没有研究区"
      emptyDetail="先建立一个研究上下文，再从文件系统绑定资产并发起第一次运行。"
    >
      <section className="wa-index" aria-label="研究区列表">
        <div className="wa-index-head" aria-hidden="true">
          <span>研究区</span><span>最近更新</span><span>标识</span><span />
        </div>
        {(areas.data?.items ?? []).map((area) => <button
          className="wa-index-row"
          type="button"
          key={area.workarea_id}
          onClick={() => navigate(`/workareas/${encodeURIComponent(area.workarea_id)}?user=${encodeURIComponent(user)}`)}
        >
          <span className="wa-index-copy">
            <strong>{area.title}</strong>
            <span>{area.description || "未填写说明"}</span>
          </span>
          <time>{formatTimestamp(area.updated_at)}</time>
          <code>{area.workarea_id}</code>
          <ArrowRight aria-hidden="true" size={15} />
        </button>)}
      </section>
    </QueryBoundary>
  </div>;
}

function WorkAreaDetailPage({ user, navigate, workareaId }: PageProps & { workareaId: string }) {
  const queryClient = useQueryClient();
  const area = useQuery({
    queryKey: ["workarea", user, workareaId],
    queryFn: ({ signal }) => workareaApi.get(user, workareaId, signal),
  });
  const launches = useQuery({
    queryKey: ["workarea-launches", user, workareaId],
    queryFn: ({ signal }) => workareaApi.launches(user, workareaId, signal),
  });
  const [pickerOpen, setPickerOpen] = useState(false);
  const [assetRole, setAssetRole] = useState("code");
  const [runId, setRunId] = useState("");
  const [editOpen, setEditOpen] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editDescription, setEditDescription] = useState("");

  const bind = useMutation({
    mutationFn: (input: { kind: "asset" | "run"; target_ref: string; role?: string }) => workareaApi.addBinding(user, workareaId, input),
    onSuccess: (record) => {
      queryClient.setQueryData(["workarea", user, workareaId], record);
      setRunId("");
    },
  });

  const update = useMutation({
    mutationFn: () => workareaApi.update(user, workareaId, {
      title: editTitle.trim(),
      description: editDescription.trim(),
    }),
    onSuccess: async (record) => {
      queryClient.setQueryData(["workarea", user, workareaId], record);
      await queryClient.invalidateQueries({ queryKey: ["workareas", user] });
      setEditOpen(false);
    },
  });

  return <QueryBoundary pending={area.isPending} error={area.error}>
    {area.data ? <div className="workarea-v2">
      <PageHeader
        overline="研究区"
        identity={area.data.workarea_id}
        title={area.data.title}
        detail={area.data.description || "这个研究区还没有说明。"}
        actions={<>
          <button className="button secondary" type="button" onClick={() => {
            setEditTitle(area.data!.title);
            setEditDescription(area.data!.description);
            setEditOpen(true);
          }}>编辑</button>
          <button className="button primary" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(workareaId)}/launch/new?user=${encodeURIComponent(user)}`)}>
            <Rocket aria-hidden="true" size={15} /> 新建运行
          </button>
        </>}
      />

      <div className="wa-context-strip" aria-label="研究区概览">
        <ContextFact label="资产" value={String(area.data.bindings.assets.length)} />
        <ContextFact label="配置" value={String(area.data.bindings.contracts.length)} />
        <ContextFact label="运行" value={String(area.data.bindings.runs.length)} />
        <ContextFact label="Launch" value={launches.data ? String(launches.data.items.length) : "—"} />
      </div>

      {editOpen ? <section className="wa-focus-surface wa-editor" aria-label="编辑研究区">
        <div className="wa-editor-copy">
          <h2>编辑研究区</h2>
          <p>只修改名称和说明；资产、Launch、Run、Evidence 与 provenance 保持不变。</p>
        </div>
        <div className="wa-form-grid">
          <label className="form-field">
            <span>名称</span>
            <input autoFocus value={editTitle} onChange={(event) => setEditTitle(event.target.value)} />
          </label>
          <label className="form-field">
            <span>说明</span>
            <textarea value={editDescription} onChange={(event) => setEditDescription(event.target.value)} />
          </label>
        </div>
        {update.isError ? <p className="wa-error" role="alert">{update.error.message}</p> : null}
        <div className="wa-form-actions">
          <button className="button secondary" type="button" disabled={update.isPending} onClick={() => setEditOpen(false)}>取消</button>
          <button className="button primary" type="button" disabled={!editTitle.trim() || update.isPending} onClick={() => update.mutate()}>{update.isPending ? "保存中" : "保存研究区"}</button>
        </div>
      </section> : null}

      <div className="wa-workspace-grid">
        <section className="wa-focus-surface wa-assets-surface">
          <div className="wa-surface-header">
            <div>
              <h2>研究资产</h2>
              <p>这里只保存引用，不复制集群文件。代码、数据和模型继续由文件系统管理。</p>
            </div>
            <FolderOpen className="wa-surface-icon" aria-hidden="true" size={18} />
          </div>
          <div className="wa-toolbar">
            <label className="form-field">
              <span>资产角色</span>
              <select value={assetRole} onChange={(event) => setAssetRole(event.target.value)}>
                <option value="code">代码</option>
                <option value="dataset">数据集</option>
                <option value="model">模型</option>
                <option value="directory">目录</option>
                <option value="file">文件</option>
                <option value="external">外部引用</option>
              </select>
            </label>
            <button className="button secondary" type="button" onClick={() => setPickerOpen(true)}>
              <FilePlus2 aria-hidden="true" size={15} /> 从文件系统添加
            </button>
          </div>
          <BindingList area={area.data} section="assets" empty="尚未绑定文件资产。" />
          {pickerOpen ? <FilePickerDialog
            user={user}
            homePath={`/public/home/${user}`}
            title="选择要绑定到研究区的资产"
            selectionMode="path"
            onClose={() => setPickerOpen(false)}
            onSelect={(path) => {
              setPickerOpen(false);
              bind.mutate({ kind: "asset", target_ref: path, role: assetRole });
            }}
          /> : null}
          {bind.isError ? <p className="wa-error" role="alert">{bind.error.message}</p> : null}
        </section>

        <aside className="wa-inspector" aria-label="研究区上下文">
          <section className="wa-inspector-section">
            <div>
              <h2>历史运行</h2>
              <p>把已有 Run 明确纳入这个研究上下文。</p>
            </div>
            <div className="wa-inline-control">
              <label className="form-field">
                <span>绑定已有 Run ID</span>
                <input value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="run_..." />
              </label>
              <button className="button secondary" type="button" disabled={!runId.trim() || bind.isPending} onClick={() => bind.mutate({ kind: "run", target_ref: runId.trim() })}>绑定</button>
            </div>
            <BindingList
              area={area.data}
              section="runs"
              empty="尚无历史或 Launch 产生的 Run。"
              onOpen={(id) => navigate(`/runs/${encodeURIComponent(id)}?user=${encodeURIComponent(user)}&tab=overview`)}
            />
          </section>

          <section className="wa-inspector-section">
            <div>
              <h2>配置引用</h2>
              <p>选择 Contract 发起运行时自动建立来源关系。</p>
            </div>
            <BindingList area={area.data} section="contracts" empty="尚未关联 Contract。" />
          </section>
        </aside>
      </div>

      <section className="wa-flat-section">
        <div className="wa-flat-header">
          <div>
            <h2>运行提交</h2>
            <p>这里仅列出已经显式 Commit 的 Launch；候选与预检不会混入运行历史。</p>
          </div>
          {launches.isFetching ? <StatusBadge label="同步中" tone="info" /> : null}
        </div>
        <QueryBoundary
          pending={launches.isPending}
          error={launches.error}
          empty={(launches.data?.items.length ?? 0) === 0}
          emptyTitle="还没有 Launch"
          emptyDetail="新建运行会先生成 Candidate 与 Preflight；Review 后显式 Commit 才进入这里。"
        >
          <div className="wa-launch-list">
            {(launches.data?.items ?? []).map((launch) => <LaunchRow
              key={launch.launch_id}
              launch={launch}
              onOpen={() => navigate(`/launches/${encodeURIComponent(launch.launch_id)}?user=${encodeURIComponent(user)}`)}
            />)}
          </div>
        </QueryBoundary>
      </section>
    </div> : null}
  </QueryBoundary>;
}

function NewLaunchPage({ user, navigate, workareaId }: PageProps & { workareaId: string }) {
  const area = useQuery({
    queryKey: ["workarea", user, workareaId],
    queryFn: ({ signal }) => workareaApi.get(user, workareaId, signal),
  });
  const contracts = useQuery({
    queryKey: ["workarea-contract-options", user],
    queryFn: ({ signal }) => workareaApi.contracts(user, signal),
  });
  const [contractId, setContractId] = useState("");
  const [title, setTitle] = useState("");
  const [note, setNote] = useState("");

  const prepare = useMutation({
    mutationFn: async () => {
      const candidate = await workareaApi.createCandidate(user, workareaId, {
        contract_id: contractId,
        title: title.trim(),
        note: note.trim(),
        request_key: `web-launch-candidate-${crypto.randomUUID()}`,
      });
      await workareaApi.preflight(user, candidate.candidate_id);
      return candidate;
    },
    onSuccess: (candidate) => navigate(`/launches/${encodeURIComponent(candidate.candidate_id)}/review?user=${encodeURIComponent(user)}`),
  });

  return <div className="workarea-v2 wa-flow-page">
    <button className="wa-back-link" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(workareaId)}?user=${encodeURIComponent(user)}`)}>
      <ArrowLeft aria-hidden="true" size={14} /> 返回研究区
    </button>
    <PageHeader
      overline="Launch · 配置"
      title="新建运行"
      detail={`在 ${area.data?.title ?? workareaId} 中选择已有 Contract。此步骤只准备 Candidate 和预检，不会提交 Slurm。`}
    />
    <FlowRail current={0} />

    <div className="wa-flow-layout">
      <section className="wa-focus-surface wa-flow-main">
        <div className="wa-surface-header">
          <div>
            <h2>运行意图</h2>
            <p>先说明“运行什么”，资源和脚本的最终值在下一步由服务端重新物化并 Review。</p>
          </div>
        </div>
        <div className="wa-form-grid">
          <label className="form-field">
            <span>Contract</span>
            <select value={contractId} onChange={(event) => setContractId(event.target.value)}>
              <option value="">选择 Contract</option>
              {(contracts.data?.items ?? []).map((contract) => <option key={contract.contract_id} value={contract.contract_id}>
                {contract.contract_id} · {contract.recipe_version_id}
              </option>)}
            </select>
          </label>
          <label className="form-field">
            <span>Launch 标题</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：基线运行" />
          </label>
          <label className="form-field">
            <span>备注</span>
            <textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="这次运行要验证什么？" />
          </label>
        </div>
        {contracts.isError ? <p className="wa-error" role="alert">{contracts.error.message}</p> : null}
        {prepare.isError ? <p className="wa-error" role="alert">{prepare.error.message}</p> : null}
        <div className="wa-form-actions">
          <button className="button secondary" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(workareaId)}?user=${encodeURIComponent(user)}`)}>取消</button>
          <button className="button primary" type="button" disabled={!contractId || prepare.isPending} onClick={() => prepare.mutate()}>
            <ShieldCheck aria-hidden="true" size={15} /> {prepare.isPending ? "正在预检" : "生成预检并进入 Review"}
          </button>
        </div>
      </section>

      <aside className="wa-flow-aside">
        <h2>当前上下文</h2>
        <p>Review 前没有任何调度器副作用。选定的 Contract 会被明确绑定到当前研究区。</p>
        <dl className="wa-aside-facts">
          <AsideFact label="研究区" value={area.data?.title ?? workareaId} />
          <AsideFact label="WorkArea ID" value={workareaId} mono />
          <AsideFact label="下一步" value="检查实际工作目录、脚本与资源请求" />
        </dl>
      </aside>
    </div>
  </div>;
}

function LaunchReviewPage({ user, navigate, candidateId }: PageProps & { candidateId: string }) {
  const queryClient = useQueryClient();
  const candidate = useQuery({
    queryKey: ["launch-candidate", user, candidateId],
    queryFn: ({ signal }) => workareaApi.candidate(user, candidateId, signal),
  });
  const [confirmed, setConfirmed] = useState(false);

  const refresh = useMutation({
    mutationFn: () => workareaApi.preflight(user, candidateId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["launch-candidate", user, candidateId] });
      setConfirmed(false);
    },
  });

  const commit = useMutation({
    mutationFn: () => workareaApi.commit(user, candidateId, {
      preflight_digest: candidate.data?.preflight?.assessment_digest ?? "",
      request_key: `web-launch-commit-${crypto.randomUUID()}`,
    }),
    onSuccess: (result) => navigate(`/launches/${encodeURIComponent(result.launch.launch_id)}?user=${encodeURIComponent(user)}`),
  });

  const preflight = candidate.data?.preflight ?? null;

  return <QueryBoundary pending={candidate.isPending} error={candidate.error}>
    {candidate.data ? <div className="workarea-v2 wa-flow-page">
      <button className="wa-back-link" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(candidate.data!.workarea_id)}/launch/new?user=${encodeURIComponent(user)}`)}>
        <ArrowLeft aria-hidden="true" size={14} /> 返回运行配置
      </button>
      <PageHeader
        overline="Launch · Review"
        identity={candidate.data.candidate_id}
        title={candidate.data.title || "运行提交 Review"}
        detail="检查服务端重新物化后的实际 Slurm 请求。Commit 前会再次预检；任何变化都会要求重新 Review。"
        actions={<button className="button secondary" type="button" disabled={refresh.isPending} onClick={() => refresh.mutate()}>
          <RefreshCw aria-hidden="true" size={15} /> 重新预检
        </button>}
      />
      <FlowRail current={1} />

      <div className="wa-review-layout">
        <section className="wa-focus-surface wa-review-main">
          <PreflightView preflight={preflight} />
        </section>

        <aside className="wa-flow-aside wa-commit-aside">
          <div className={`wa-commit-box${confirmed ? " is-ready" : ""}`}>
            <div>
              <h2>确认提交</h2>
              <p>Candidate 和 Preflight 已持久化，但 Run 还没有提交。</p>
            </div>
            <label className="wa-confirm-check">
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
              <span>我已检查工作目录、脚本和资源请求，并确认按当前 Preflight Commit。</span>
            </label>
            {commit.isError ? <p className="wa-error" role="alert">{commit.error.message}</p> : null}
            <button className="button primary" type="button" disabled={!confirmed || preflight?.status !== "OK" || commit.isPending} onClick={() => commit.mutate()}>
              <Rocket aria-hidden="true" size={15} /> {commit.isPending ? "正在 Commit" : "Commit 并提交 Run"}
            </button>
          </div>
        </aside>
      </div>
    </div> : null}
  </QueryBoundary>;
}

function PreflightView({ preflight }: { preflight: LaunchPreflight | null }) {
  if (!preflight) return <div className="query-state">
    <strong>尚无 Preflight</strong><span>重新预检后才能进入 Commit。</span>
  </div>;

  const submit = asRecord(preflight.effective_request.run_submit_request);
  const resource = asRecord(submit?.resource_plan);
  const statusLabel = preflight.status === "OK" ? "可提交" : "已阻断";

  return <>
    <div className="wa-review-head">
      <div>
        <h2>Effective Slurm Request</h2>
        <p>以下字段是本次 Review 的执行事实，不是浏览器端推测。</p>
      </div>
      <StatusBadge label={statusLabel} tone={preflight.status === "OK" ? "success" : "danger"} />
    </div>

    <dl className="wa-request-facts">
      <RequestFact label="Workdir" value={String(submit?.workdir ?? preflight.effective_request.workdir ?? "—")} mono wide />
      <RequestFact label="Partition / QoS" value={`${String(resource?.partition ?? "—")} / ${String(resource?.qos ?? "—")}`} />
      <RequestFact label="CPU / task" value={String(resource?.cpus_per_task ?? "—")} />
      <RequestFact label="GPU" value={String(resource?.gpus_per_node ?? resource?.gpus_total ?? 0)} />
      <RequestFact label="Time limit" value={String(resource?.time_limit ?? "—")} />
      <RequestFact label="Assessment digest" value={preflight.assessment_digest} mono wide />
    </dl>

    <label className="wa-code-block">
      <span>将提交的脚本</span>
      <textarea className="wa-code-surface" readOnly rows={12} value={String(submit?.script ?? preflight.effective_request.script ?? "")} />
    </label>

    <div className="wa-findings">
      <span className="wa-findings-title">预检结论</span>
      {preflight.findings.length ? preflight.findings.map((finding) => <div className="wa-finding-row" key={`${finding.code}:${finding.message}`}>
        <StatusBadge label={finding.severity} tone={finding.severity === "BLOCK" ? "danger" : finding.severity === "WARN" ? "warning" : "neutral"} />
        <strong>{finding.code}</strong>
        <span>{finding.message}</span>
        <small>{finding.source_authority ?? "—"}</small>
      </div>) : <div className="wa-pass-line">
        <CheckCircle2 aria-hidden="true" size={15} />
        <span>没有阻塞项。Commit 时仍会重新计算关键检查。</span>
      </div>}
    </div>
  </>;
}

function LaunchDetailPage({ user, navigate, launchId }: PageProps & { launchId: string }) {
  const launch = useQuery({
    queryKey: ["launch", user, launchId],
    queryFn: ({ signal }) => workareaApi.launch(user, launchId, signal),
  });
  const firstRun = launch.data?.run_ids[0] ?? null;
  const run = useQuery({
    queryKey: ["run", user, firstRun],
    queryFn: ({ signal }) => api.run(user, firstRun!, signal),
    enabled: Boolean(firstRun),
  });

  return <QueryBoundary pending={launch.isPending} error={launch.error}>
    {launch.data ? <div className="workarea-v2 wa-flow-page">
      <button className="wa-back-link" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(launch.data!.workarea_id)}?user=${encodeURIComponent(user)}`)}>
        <ArrowLeft aria-hidden="true" size={14} /> 返回研究区
      </button>
      <PageHeader
        overline="Launch · 已 Commit"
        title={launch.data.launch_id}
        detail="Launch 记录已审核的提交意图；运行状态、Job ID、日志和 Evidence 继续由 Run authority 提供。"
        actions={<StatusBadge
          label={launch.data.submit_error ? "提交失败" : launch.data.submitted_at ? "已提交" : "已 Commit"}
          tone={launch.data.submit_error ? "danger" : "success"}
        />}
      />
      <FlowRail current={3} />

      <div className="wa-launch-layout">
        <section className="wa-focus-surface wa-run-focus">
          <div className="wa-surface-header">
            <div>
              <h2>运行状态</h2>
              <p>从这里继续进入实时日志、Evidence、诊断和发布，不需要重新寻找页面。</p>
            </div>
          </div>
          {firstRun ? <>
            <div className="wa-run-identity">
              <code>{firstRun}</code>
              <StatusBadge
                label={run.data?.state ?? "读取中"}
                tone={run.data?.state === "SUCCEEDED" ? "success" : run.data?.state?.includes("FAIL") ? "danger" : "info"}
              />
            </div>
            <dl className="wa-run-facts">
              <dt>Job ID</dt><dd>{run.data?.job_id ?? "—"}</dd>
              <dt>工作目录</dt><dd className="mono">{run.data?.workdir ?? "—"}</dd>
            </dl>
            <div className="wa-form-actions">
              <button className="button primary" type="button" onClick={() => navigate(`/runs/${encodeURIComponent(firstRun)}?user=${encodeURIComponent(user)}&tab=overview`)}>
                打开 Run、日志与 Evidence <ArrowRight aria-hidden="true" size={15} />
              </button>
            </div>
          </> : <p className="wa-empty-inline">Launch 尚未关联 Run。</p>}
        </section>

        <aside className="wa-flow-aside">
          <h2>提交来源</h2>
          <p>这些值用于审计“提交了什么”，与实时运行状态分开。</p>
          <dl className="wa-provenance">
            <ProvenanceFact label="Contract" value={launch.data.contract_id} mono />
            <ProvenanceFact label="Candidate" value={launch.data.candidate_id} mono />
            <ProvenanceFact label="Preflight digest" value={launch.data.preflight_digest} mono />
            <ProvenanceFact label="Committed" value={formatTimestamp(launch.data.committed_at)} />
          </dl>
          {launch.data.submit_error ? <pre className="mono wa-error">{JSON.stringify(launch.data.submit_error, null, 2)}</pre> : null}
        </aside>
      </div>
    </div> : null}
  </QueryBoundary>;
}

function PageHeader({
  overline,
  identity,
  title,
  detail,
  actions,
}: {
  overline: string;
  identity?: string;
  title: string;
  detail: string;
  actions?: React.ReactNode;
}) {
  return <header className="wa-page-header">
    <div className="wa-page-heading">
      {identity ? <div className="wa-id-line"><p className="wa-overline">{overline}</p><code>{identity}</code></div> : <p className="wa-overline">{overline}</p>}
      <h1>{title}</h1>
      <p className="wa-page-detail">{detail}</p>
    </div>
    {actions ? <div className="wa-header-actions">{actions}</div> : null}
  </header>;
}

function ContextFact({ label, value }: { label: string; value: string }) {
  return <div className="wa-context-fact"><span>{label}</span><strong>{value}</strong></div>;
}

function BindingList({
  area,
  section,
  empty,
  onOpen,
}: {
  area: WorkAreaDetail;
  section: BindingSection;
  empty: string;
  onOpen?: (id: string) => void;
}) {
  const items = area.bindings[section];
  if (!items.length) return <p className="wa-empty-inline">{empty}</p>;
  return <ul className="wa-binding-list">
    {items.map((item) => <li className="wa-binding-row" key={`${item.kind}:${item.target_ref}`}>
      <StatusBadge label={bindingLabel(item.role ?? item.kind)} tone={item.source === "inherited" ? "info" : "neutral"} />
      <span className="wa-binding-main">
        <code title={item.target_ref}>{item.target_ref}</code>
        <small>{item.source === "inherited" ? "由 Launch 继承" : "用户明确绑定"}</small>
      </span>
      {onOpen ? <button className="wa-binding-open" type="button" onClick={() => onOpen(item.target_ref)}>打开</button> : null}
    </li>)}
  </ul>;
}

function LaunchRow({ launch, onOpen }: { launch: LaunchRecord; onOpen: () => void }) {
  const status = launch.submit_error ? "提交失败" : launch.submitted_at ? "已提交" : "已 Commit";
  const tone = launch.submit_error ? "danger" : "success";
  return <button className="wa-launch-row" type="button" onClick={onOpen}>
    <code>{launch.run_ids[0] ?? launch.launch_id}</code>
    <span>{launch.contract_id}</span>
    <time>{formatTimestamp(launch.committed_at)}</time>
    <StatusBadge label={status} tone={tone} />
    <ArrowRight aria-hidden="true" size={14} />
  </button>;
}

function FlowRail({ current }: { current: FlowStage }) {
  const steps = ["配置", "Review", "提交", "运行"];
  return <div className="wa-flow-rail" aria-label="运行流程">
    {steps.map((label, index) => {
      const done = index < current;
      const active = index === current;
      return <div className={`wa-flow-step${done ? " is-done" : ""}${active ? " is-current" : ""}`} key={label}>
        <span className="wa-flow-step-mark">{done ? <Check aria-hidden="true" size={12} /> : index + 1}</span>
        <span>{label}</span>
      </div>;
    })}
  </div>;
}

function AsideFact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div className="wa-aside-fact"><dt>{label}</dt><dd className={mono ? "mono" : undefined}>{value}</dd></div>;
}

function RequestFact({
  label,
  value,
  mono = false,
  wide = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
  wide?: boolean;
}) {
  return <div className={`wa-request-fact${wide ? " is-wide" : ""}`}>
    <dt>{label}</dt><dd className={mono ? "mono" : undefined}>{value}</dd>
  </div>;
}

function ProvenanceFact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div className="wa-provenance-row"><dt>{label}</dt><dd className={mono ? "mono" : undefined}>{value}</dd></div>;
}

function bindingLabel(value: string): string {
  const labels: Record<string, string> = {
    asset: "资产",
    code: "代码",
    dataset: "数据",
    model: "模型",
    directory: "目录",
    file: "文件",
    external: "外部",
    run: "Run",
    historical_run: "历史 Run",
    launch_run: "Launch Run",
    contract: "Contract",
  };
  return labels[value] ?? value;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}