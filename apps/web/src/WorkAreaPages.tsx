import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  FilePlus2,
  FolderOpen,
  History,
  Play,
  Plus,
  RefreshCw,
  Rocket,
  ShieldCheck,
} from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "./api";
import { QueryBoundary, SectionHeading, StatusBadge, formatTimestamp } from "./components";
import { FilePickerDialog } from "./files/FilePickerDialog";
import "./files/file-workspace-shell.css";
import type { LocationState } from "./url";
import {
  workareaApi,
  type LaunchCandidate,
  type LaunchPreflight,
  type LaunchRecord,
  type WorkAreaDetail,
} from "./workarea-api";

interface PageProps {
  user: string;
  location: LocationState;
  navigate: (path: string, options?: { replace?: boolean }) => void;
}

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
  return (
    <>
      <SectionHeading
        eyebrow="WorkArea / research context"
        title="研究区"
        detail="把代码、数据、Contract、历史 Run 和后续 Launch 放在同一个持久研究上下文中；文件和 Run 本身仍由各自权威系统管理。"
        action={<button className="button primary" type="button" onClick={() => setOpen(true)}><Plus aria-hidden="true" size={15} /> 新建研究区</button>}
      />
      {open ? <section className="panel template-release-main">
        <div className="panel-heading"><div><p className="panel-kicker">New WorkArea</p><h2>创建研究区</h2></div></div>
        <div className="form-grid two">
          <label className="form-field"><span>名称</span><input autoFocus value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：Wan FFN 稀疏实验" /></label>
          <label className="form-field"><span>说明</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="记录这组研究工作的目标与边界" /></label>
        </div>
        {create.isError ? <p className="limitation" role="alert">{create.error.message}</p> : null}
        <div className="agent-action-row"><button className="button secondary" type="button" onClick={() => setOpen(false)}>取消</button><button className="button primary" type="button" disabled={!title.trim() || create.isPending} onClick={() => create.mutate()}>{create.isPending ? "创建中" : "创建并进入"}</button></div>
      </section> : null}
      <QueryBoundary
        pending={areas.isPending}
        error={areas.error}
        empty={(areas.data?.items.length ?? 0) === 0}
        emptyTitle="还没有研究区"
        emptyDetail="创建一个 WorkArea 后，再从文件系统绑定研究资产并发起第一次 Launch。"
      >
        <section className="market-grid" aria-label="研究区列表">
          {(areas.data?.items ?? []).map((area) => <article className="template-card" key={area.workarea_id}>
            <header><div><p className="panel-kicker mono">{area.workarea_id}</p><h2>{area.title}</h2></div><StatusBadge label="WorkArea" tone="info" /></header>
            <p className="template-description">{area.description || "未填写说明。"}</p>
            <div className="template-meta"><span>更新 {formatTimestamp(area.updated_at)}</span></div>
            <button className="button secondary wide" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(area.workarea_id)}?user=${encodeURIComponent(user)}`)}>打开研究区 <ArrowRight aria-hidden="true" size={15} /></button>
          </article>)}
        </section>
      </QueryBoundary>
    </>
  );
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
  return (
    <QueryBoundary pending={area.isPending} error={area.error}>
      {area.data ? <>
        <SectionHeading
          eyebrow={`WorkArea / ${area.data.workarea_id}`}
          title={area.data.title}
          detail={area.data.description || "这个研究区还没有说明。"}
          action={<div className="agent-action-row">
            <button className="button secondary" type="button" onClick={() => {
              setEditTitle(area.data!.title);
              setEditDescription(area.data!.description);
              setEditOpen(true);
            }}>编辑研究区</button>
            <button className="button primary" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(workareaId)}/launch/new?user=${encodeURIComponent(user)}`)}><Rocket aria-hidden="true" size={15} /> 新建运行</button>
          </div>}
        />
        {editOpen ? <section className="panel template-release-main">
          <div className="panel-heading"><div><p className="panel-kicker">WorkArea metadata</p><h2>编辑研究区</h2></div></div>
          <p className="side-detail">只修改研究区名称与说明；已有资产、Launch、Run、Evidence 和 provenance 不受影响。</p>
          <div className="form-grid two">
            <label className="form-field"><span>名称</span><input autoFocus value={editTitle} onChange={(event) => setEditTitle(event.target.value)} /></label>
            <label className="form-field"><span>说明</span><textarea value={editDescription} onChange={(event) => setEditDescription(event.target.value)} /></label>
          </div>
          {update.isError ? <p className="limitation" role="alert">{update.error.message}</p> : null}
          <div className="agent-action-row">
            <button className="button secondary" type="button" disabled={update.isPending} onClick={() => setEditOpen(false)}>取消</button>
            <button className="button primary" type="button" disabled={!editTitle.trim() || update.isPending} onClick={() => update.mutate()}>{update.isPending ? "保存中" : "保存研究区"}</button>
          </div>
        </section> : null}
        <div className="template-detail-grid">
          <section className="panel template-release-main">
            <div className="panel-heading"><div><p className="panel-kicker">Assets / Files authority</p><h2>研究资产</h2></div><FolderOpen aria-hidden="true" size={19} /></div>
            <p className="side-detail">这里只保存引用，不复制集群文件。使用现有文件选择器选择代码目录、数据、模型或其它资产。</p>
            <div className="agent-action-row">
              <label className="form-field"><span>资产角色</span><select value={assetRole} onChange={(event) => setAssetRole(event.target.value)}><option value="code">代码</option><option value="dataset">数据集</option><option value="model">模型</option><option value="directory">目录</option><option value="file">文件</option><option value="external">外部引用</option></select></label>
              <button className="button secondary" type="button" onClick={() => setPickerOpen(true)}><FilePlus2 aria-hidden="true" size={15} /> 从文件系统添加</button>
            </div>
            <BindingList area={area.data} section="assets" empty="尚未绑定文件资产。" />
            {pickerOpen ? <FilePickerDialog user={user} homePath={`/public/home/${user}`} title="选择要绑定到研究区的资产" selectionMode="path" onClose={() => setPickerOpen(false)} onSelect={(path) => { setPickerOpen(false); bind.mutate({ kind: "asset", target_ref: path, role: assetRole }); }} /> : null}
            {bind.isError ? <p className="limitation" role="alert">{bind.error.message}</p> : null}
          </section>
          <aside className="template-detail-side">
            <section className="panel"><div className="panel-heading"><div><p className="panel-kicker">Historical Runs</p><h2>历史运行</h2></div><History aria-hidden="true" size={19} /></div>
              <label className="form-field"><span>绑定已有 Run ID</span><input value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="run_..." /></label>
              <button className="button secondary wide" type="button" disabled={!runId.trim() || bind.isPending} onClick={() => bind.mutate({ kind: "run", target_ref: runId.trim() })}>绑定历史 Run</button>
              <BindingList area={area.data} section="runs" empty="尚无历史或 Launch 产生的 Run。" onOpen={(id) => navigate(`/runs/${encodeURIComponent(id)}?user=${encodeURIComponent(user)}&tab=overview`)} />
            </section>
            <section className="panel"><div className="panel-heading"><div><p className="panel-kicker">Contracts</p><h2>配置引用</h2></div></div><BindingList area={area.data} section="contracts" empty="发起 Launch 时会自动绑定所选 Contract。" /></section>
          </aside>
        </div>
        <section className="panel runs-panel">
          <div className="panel-heading"><div><p className="panel-kicker">Launch history</p><h2>已提交 Launch</h2></div>{launches.isFetching ? <StatusBadge label="同步中" tone="info" /> : <StatusBadge label="持久化" tone="success" />}</div>
          <QueryBoundary pending={launches.isPending} error={launches.error} empty={(launches.data?.items.length ?? 0) === 0} emptyTitle="还没有 Launch" emptyDetail="“新建运行”会先生成 Candidate 与 Preflight；只有 Review 后显式 Commit 才产生这里的 Launch。">
            <div className="market-grid">{(launches.data?.items ?? []).map((launch) => <LaunchCard key={launch.launch_id} launch={launch} onOpen={() => navigate(`/launches/${encodeURIComponent(launch.launch_id)}?user=${encodeURIComponent(user)}`)} />)}</div>
          </QueryBoundary>
        </section>
      </> : null}
    </QueryBoundary>
  );
}

function BindingList({ area, section, empty, onOpen }: { area: WorkAreaDetail; section: "assets" | "runs" | "contracts"; empty: string; onOpen?: (id: string) => void }) {
  const items = area.bindings[section];
  if (!items.length) return <p className="side-detail">{empty}</p>;
  return <ul className="lineage-edges">{items.map((item) => <li key={`${item.kind}:${item.target_ref}`}><StatusBadge label={item.role ?? item.kind} tone={item.source === "inherited" ? "info" : "neutral"} /><span className="mono wrap-anywhere">{item.target_ref}</span><small>{item.source === "inherited" ? "由 Launch 继承" : "用户绑定"}</small>{onOpen ? <button type="button" onClick={() => onOpen(item.target_ref)}>打开</button> : null}</li>)}</ul>;
}

function NewLaunchPage({ user, navigate, workareaId }: PageProps & { workareaId: string }) {
  const area = useQuery({ queryKey: ["workarea", user, workareaId], queryFn: ({ signal }) => workareaApi.get(user, workareaId, signal) });
  const contracts = useQuery({ queryKey: ["workarea-contract-options", user], queryFn: ({ signal }) => workareaApi.contracts(user, signal) });
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
  return <>
    <SectionHeading eyebrow="LaunchCandidate / prepare" title="新建运行" detail={`在 ${area.data?.title ?? workareaId} 中选择一个不可变 Contract。下一步只生成预检和 Review，不会提交 Slurm。`} />
    <section className="panel template-release-main">
      <div className="form-grid two">
        <label className="form-field"><span>Contract</span><select value={contractId} onChange={(event) => setContractId(event.target.value)}><option value="">选择 Contract</option>{(contracts.data?.items ?? []).map((contract) => <option key={contract.contract_id} value={contract.contract_id}>{contract.contract_id} · {contract.recipe_version_id}</option>)}</select></label>
        <label className="form-field"><span>Launch 标题</span><input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：baseline GPU run" /></label>
        <label className="form-field"><span>备注</span><textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="这次运行要验证什么？" /></label>
      </div>
      {contracts.isError ? <p className="limitation" role="alert">{contracts.error.message}</p> : null}
      {prepare.isError ? <p className="limitation" role="alert">{prepare.error.message}</p> : null}
      <div className="agent-action-row"><button className="button secondary" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(workareaId)}?user=${encodeURIComponent(user)}`)}>返回研究区</button><button className="button primary" type="button" disabled={!contractId || prepare.isPending} onClick={() => prepare.mutate()}><ShieldCheck aria-hidden="true" size={15} /> {prepare.isPending ? "正在预检" : "生成预检并进入 Review"}</button></div>
    </section>
  </>;
}

function LaunchReviewPage({ user, navigate, candidateId }: PageProps & { candidateId: string }) {
  const queryClient = useQueryClient();
  const candidate = useQuery({ queryKey: ["launch-candidate", user, candidateId], queryFn: ({ signal }) => workareaApi.candidate(user, candidateId, signal) });
  const [confirmed, setConfirmed] = useState(false);
  const refresh = useMutation({ mutationFn: () => workareaApi.preflight(user, candidateId), onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["launch-candidate", user, candidateId] }); setConfirmed(false); } });
  const commit = useMutation({
    mutationFn: () => workareaApi.commit(user, candidateId, { preflight_digest: candidate.data?.preflight?.assessment_digest ?? "", request_key: `web-launch-commit-${crypto.randomUUID()}` }),
    onSuccess: (result) => navigate(`/launches/${encodeURIComponent(result.launch.launch_id)}?user=${encodeURIComponent(user)}`),
  });
  const preflight = candidate.data?.preflight ?? null;
  return <QueryBoundary pending={candidate.isPending} error={candidate.error}>{candidate.data ? <>
    <SectionHeading eyebrow="Launch / Review Effective Slurm Request" title={candidate.data.title || "运行提交 Review"} detail="这里展示服务端重新物化后的有效请求。Commit 前会再次预检；若平台事实或授权发生变化，提交会因 stale preflight 被拒绝。" action={<button className="button secondary" type="button" disabled={refresh.isPending} onClick={() => refresh.mutate()}><RefreshCw aria-hidden="true" size={15} /> 重新预检</button>} />
    <div className="template-detail-grid"><section className="panel template-release-main"><PreflightView preflight={preflight} /></section><aside className="template-detail-side"><section className="panel"><div className="panel-heading"><div><p className="panel-kicker">Explicit Commit</p><h2>确认提交</h2></div><Play aria-hidden="true" size={19} /></div><p className="side-detail">Candidate 与 Preflight 都是持久对象，但此刻尚未创建/提交 Run。</p><label className="checkbox-field"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>我已检查工作目录、脚本和资源请求，并确认按当前 Preflight Commit。</span></label>{commit.isError ? <p className="limitation" role="alert">{commit.error.message}</p> : null}<button className="button primary wide" type="button" disabled={!confirmed || preflight?.status !== "OK" || commit.isPending} onClick={() => commit.mutate()}><Rocket aria-hidden="true" size={15} /> {commit.isPending ? "正在 Commit" : "Commit 并提交 Run"}</button></section></aside></div>
  </> : null}</QueryBoundary>;
}

function PreflightView({ preflight }: { preflight: LaunchPreflight | null }) {
  if (!preflight) return <div className="query-state"><strong>尚无 Preflight</strong><span>请重新预检。</span></div>;
  const submit = asRecord(preflight.effective_request.run_submit_request);
  const resource = asRecord(submit?.resource_plan);
  return <>
    <div className="panel-heading"><div><p className="panel-kicker">Deterministic preflight</p><h2>Effective Slurm Request</h2></div><StatusBadge label={preflight.status} tone={preflight.status === "OK" ? "success" : "danger"} /></div>
    <dl className="fact-list"><div><dt>Workdir</dt><dd className="mono wrap-anywhere">{String(submit?.workdir ?? preflight.effective_request.workdir ?? "—")}</dd></div><div><dt>Partition / QoS</dt><dd>{String(resource?.partition ?? "—")} / {String(resource?.qos ?? "—")}</dd></div><div><dt>CPU</dt><dd>{String(resource?.cpus_per_task ?? "—")} / task</dd></div><div><dt>GPU</dt><dd>{String(resource?.gpus_per_node ?? resource?.gpus_total ?? 0)}</dd></div><div><dt>Time</dt><dd>{String(resource?.time_limit ?? "—")}</dd></div><div><dt>Assessment digest</dt><dd className="mono wrap-anywhere">{preflight.assessment_digest}</dd></div></dl>
    <label className="form-field"><span>将提交的脚本</span><textarea className="mono" readOnly rows={12} value={String(submit?.script ?? preflight.effective_request.script ?? "")} /></label>
    <div><p className="panel-kicker">Findings</p>{preflight.findings.length ? <ul className="lineage-edges">{preflight.findings.map((finding) => <li key={`${finding.code}:${finding.message}`}><StatusBadge label={finding.severity} tone={finding.severity === "BLOCK" ? "danger" : finding.severity === "WARN" ? "warning" : "neutral"} /><strong>{finding.code}</strong><span>{finding.message}</span><small>{finding.source_authority ?? "—"}</small></li>)}</ul> : <div className="studio-notice success"><CheckCircle2 aria-hidden="true" /><div><strong>没有阻塞项</strong><p>Commit 时仍会重新计算一次。</p></div></div>}</div>
  </>;
}

function LaunchDetailPage({ user, navigate, launchId }: PageProps & { launchId: string }) {
  const launch = useQuery({ queryKey: ["launch", user, launchId], queryFn: ({ signal }) => workareaApi.launch(user, launchId, signal) });
  const firstRun = launch.data?.run_ids[0] ?? null;
  const run = useQuery({ queryKey: ["run", user, firstRun], queryFn: ({ signal }) => api.run(user, firstRun!, signal), enabled: Boolean(firstRun) });
  return <QueryBoundary pending={launch.isPending} error={launch.error}>{launch.data ? <>
    <SectionHeading eyebrow="Launch / durable commit" title={launch.data.launch_id} detail="Launch 记录用户已经审核并 Commit 的请求；运行状态、Job ID、日志和 Evidence 继续由 Run authority 提供。" action={<button className="button secondary" type="button" onClick={() => navigate(`/workareas/${encodeURIComponent(launch.data!.workarea_id)}?user=${encodeURIComponent(user)}`)}>返回研究区</button>} />
    <div className="template-detail-grid"><section className="panel template-release-main"><div className="panel-heading"><div><p className="panel-kicker">Commit provenance</p><h2>Launch</h2></div><StatusBadge label={launch.data.submit_error ? "submit failed" : launch.data.submitted_at ? "submitted" : "committed"} tone={launch.data.submit_error ? "danger" : "success"} /></div><dl className="fact-list"><div><dt>Contract</dt><dd className="mono">{launch.data.contract_id}</dd></div><div><dt>Candidate</dt><dd className="mono">{launch.data.candidate_id}</dd></div><div><dt>Preflight digest</dt><dd className="mono wrap-anywhere">{launch.data.preflight_digest}</dd></div><div><dt>Committed</dt><dd>{formatTimestamp(launch.data.committed_at)}</dd></div></dl>{launch.data.submit_error ? <pre className="mono limitation">{JSON.stringify(launch.data.submit_error, null, 2)}</pre> : null}</section><aside className="template-detail-side"><section className="panel"><div className="panel-heading"><div><p className="panel-kicker">Run authority</p><h2>运行状态</h2></div></div>{firstRun ? <><p className="request-key mono">{firstRun}</p><StatusBadge label={run.data?.state ?? "loading"} tone={run.data?.state === "SUCCEEDED" ? "success" : run.data?.state?.includes("FAIL") ? "danger" : "info"} /><dl className="fact-list"><div><dt>Job ID</dt><dd>{run.data?.job_id ?? "—"}</dd></div><div><dt>Workdir</dt><dd className="mono wrap-anywhere">{run.data?.workdir ?? "—"}</dd></div></dl><button className="button primary wide" type="button" onClick={() => navigate(`/runs/${encodeURIComponent(firstRun)}?user=${encodeURIComponent(user)}&tab=overview`)}>打开 Run、日志与 Evidence <ArrowRight aria-hidden="true" size={15} /></button></> : <p className="side-detail">Launch 尚未关联 Run。</p>}</section></aside></div>
  </> : null}</QueryBoundary>;
}

function LaunchCard({ launch, onOpen }: { launch: LaunchRecord; onOpen: () => void }) {
  return <article className="template-card"><header><div><p className="panel-kicker mono">{launch.launch_id}</p><h2>{launch.run_ids[0] ?? "Committed Launch"}</h2></div><StatusBadge label={launch.submit_error ? "failed" : launch.submitted_at ? "submitted" : "committed"} tone={launch.submit_error ? "danger" : "success"} /></header><p className="template-description mono">Contract {launch.contract_id}</p><div className="template-meta"><span>{formatTimestamp(launch.committed_at)}</span></div><button className="button secondary wide" type="button" onClick={onOpen}>查看 Launch <ArrowRight aria-hidden="true" size={15} /></button></article>;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
