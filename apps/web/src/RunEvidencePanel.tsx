import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  Boxes,
  Bot,
  Bug,
  CheckCircle2,
  Copy,
  FileText,
  FolderTree,
  GitCompare,
  ListTree,
  RotateCcw,
  ScrollText,
  ShieldCheck,
  Upload,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { api } from "./api";
import { FactState, QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import {
  asObject,
  formatBytes,
  numberValue,
  parseJsonObject,
  previewContent,
  selectActiveEvidenceObject,
} from "./evidence-state";
import {
  useEvidenceObject,
  useRun,
  useRunCapsule,
  useRunDiagnoses,
  useRunEvidence,
  useRunEvents,
  useRunLineage,
} from "./query";
import type {
  DiagnosisRecordPayload,
  EvidenceObject,
  EvidenceObjectPreview,
  JsonObject,
  RunSummary,
} from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

type EvidenceTab = "overview" | "timeline" | "compare" | "logs" | "results" | "diagnosis" | "capsule" | "objects";

const tabs: Array<{ id: EvidenceTab; label: string; icon: typeof FileText }> = [
  { id: "overview", label: "摘要", icon: FileText },
  { id: "timeline", label: "时间线", icon: ListTree },
  { id: "compare", label: "对比", icon: GitCompare },
  { id: "logs", label: "日志", icon: ScrollText },
  { id: "results", label: "结果", icon: Boxes },
  { id: "diagnosis", label: "诊断", icon: Bug },
  { id: "capsule", label: "Capsule", icon: Archive },
  { id: "objects", label: "对象", icon: FolderTree },
];

interface RunEvidencePanelProps {
  user: string;
  run: RunSummary;
  location: LocationState;
  navigate: (path: string) => void;
}

export function RunEvidencePanel({ user, run, location, navigate }: RunEvidencePanelProps) {
  const requestedTab = location.search.get("tab");
  const tab = tabs.some((item) => item.id === requestedTab)
    ? requestedTab as EvidenceTab
    : "overview";
  const requestedObjectId = location.search.get("object");
  const evidence = useRunEvidence(user, run.run_id);
  const diagnoses = useRunDiagnoses(user, run.run_id);
  const capsule = useRunCapsule(user, run.run_id);
  const events = useRunEvents(user, tab === "timeline" ? run.run_id : null);
  const lineage = useRunLineage(user, tab === "timeline" || tab === "compare" ? run.run_id : null);
  const compareRunId = location.search.get("compare") ?? run.parent_run_id ?? null;
  const comparisonRun = useRun(user, tab === "compare" ? compareRunId : null);
  const comparisonEvidence = useRunEvidence(user, tab === "compare" ? compareRunId : null);
  const objects = evidence.data?.objects ?? [];
  const logs = objects.filter((item) => item.category === "logs");
  const outputs = objects.filter((item) => item.category === "outputs");
  const objectTab = tab === "timeline" || tab === "compare" ? "overview" : tab;
  const activeObject = selectActiveEvidenceObject(objects, objectTab, requestedObjectId);
  const preview = useEvidenceObject(user, run.run_id, activeObject?.object_id ?? null);
  const resultSummary = objects.find(
    (item) => item.logical_path === "derived/result_summary.v1.json",
  ) ?? null;
  const resultPreview = useEvidenceObject(
    user,
    run.run_id,
    tab === "results" ? resultSummary?.object_id ?? null : null,
  );
  const queryClient = useQueryClient();
  const diagnose = useMutation({
    mutationFn: () => api.diagnoseRun(user, run.run_id),
    onSuccess: (result) => {
      queryClient.setQueryData(["run-diagnoses", user, run.run_id], result);
      void queryClient.invalidateQueries({ queryKey: ["run", user, run.run_id] });
    },
  });
  const buildCapsule = useMutation({
    mutationFn: () => api.buildRunCapsule(user, run.run_id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["run-capsule", user, run.run_id] });
      void queryClient.invalidateQueries({ queryKey: ["run", user, run.run_id] });
    },
  });
  const startRemediation = useMutation({
    mutationFn: () => api.createRemediationSession(user, run.run_id),
    onSuccess: (session) => navigate(
      `/agent?user=${encodeURIComponent(user)}&session=${encodeURIComponent(session.session_id)}`,
    ),
  });
  const cancelRun = useMutation({
    mutationFn: () => api.cancelRun(user, run.run_id),
    onSuccess: (updated) => {
      queryClient.setQueryData(["run", user, run.run_id], updated);
      void queryClient.invalidateQueries({ queryKey: ["runs", user] });
    },
  });
  const retryRun = useMutation({
    mutationFn: () => api.prepareRetry(user, run),
    onSuccess: (prepared) => navigate(
      `/runs/${encodeURIComponent(prepared.run_id)}?user=${encodeURIComponent(user)}&tab=overview`,
    ),
  });
  const submitPrepared = useMutation({
    mutationFn: () => api.submitRun(user, run.run_id),
    onSuccess: (updated) => {
      queryClient.setQueryData(["run", user, run.run_id], updated);
      void queryClient.invalidateQueries({ queryKey: ["runs", user] });
    },
  });
  const setView = (nextTab: EvidenceTab, objectId: string | null = null) =>
    navigate(withSearch(location.pathname, location.search, { tab: nextTab, object: objectId }));

  return (
    <div className="evidence-workbench">
      <RunControls
        run={run}
        cancel={cancelRun}
        retry={retryRun}
        submit={submitPrepared}
      />
      <nav className="evidence-tabs" aria-label="Run Evidence 视图">
        {tabs.map((item) => {
          const Icon = item.icon;
          return <button key={item.id} type="button" className={tab === item.id ? "active" : undefined} aria-current={tab === item.id ? "page" : undefined} onClick={() => setView(item.id)}><Icon aria-hidden="true" />{item.label}</button>;
        })}
      </nav>

      <QueryBoundary pending={evidence.isPending} error={evidence.error}>
        {tab === "overview" ? <Overview user={user} run={run} objects={objects} tasks={evidence.data?.tasks ?? []} remediation={startRemediation} /> : null}
        {tab === "timeline" ? (
          <TimelineView
            events={events}
            lineage={lineage}
            currentRunId={run.run_id}
            onCompare={(runId) => navigate(withSearch(location.pathname, location.search, {
              tab: "compare",
              compare: runId,
              object: null,
            }))}
          />
        ) : null}
        {tab === "compare" ? (
          <CompareView
            current={run}
            currentObjects={objects}
            comparison={comparisonRun}
            comparisonEvidence={comparisonEvidence}
            candidates={lineage.data?.nodes ?? []}
            compareRunId={compareRunId}
            onSelect={(runId) => navigate(withSearch(location.pathname, location.search, {
              compare: runId,
              tab: "compare",
              object: null,
            }))}
          />
        ) : null}
        {tab === "logs" ? (
          <ObjectPreviewView
            title="标准输出与标准错误"
            detail="展示采集器保存的 bounded tail；来源、digest 和截断状态保持可核验。"
            objects={logs}
            selected={activeObject}
            preview={preview}
            onSelect={(item) => setView("logs", item.object_id)}
            contentMode="log"
          />
        ) : null}
        {tab === "results" ? (
          <ResultsView
            summary={resultPreview}
            outputs={outputs}
            selected={activeObject}
            preview={preview}
            onSelect={(item) => setView("results", item.object_id)}
          />
        ) : null}
        {tab === "diagnosis" ? (
          <DiagnosisView
            pending={diagnoses.isPending}
            error={diagnoses.error ?? diagnose.error}
            state={diagnoses.data?.diagnosis_state ?? run.diagnosis_state}
            items={diagnoses.data?.items ?? []}
            diagnosing={diagnose.isPending}
            onDiagnose={() => diagnose.mutate()}
            onEvidence={(ref) => {
              const object = objects.find((item) => item.source_uri === ref);
              if (object) setView("objects", object.object_id);
            }}
          />
        ) : null}
        {tab === "capsule" ? (
          <CapsuleView
            pending={capsule.isPending}
            error={capsule.error ?? buildCapsule.error}
            state={capsule.data?.capsule_state ?? run.capsule_state}
            detail={capsule.data?.capsule ?? null}
            collectionReady={evidence.data?.collection_state === "succeeded"}
            building={buildCapsule.isPending}
            onBuild={() => buildCapsule.mutate()}
          />
        ) : null}
        {tab === "objects" ? (
          <ObjectPreviewView
            title="Evidence objects"
            detail="按 category 浏览 owner-scoped 对象；公共 payload 不包含服务器 store path。"
            objects={objects}
            selected={activeObject}
            preview={preview}
            onSelect={(item) => setView("objects", item.object_id)}
            contentMode="raw"
          />
        ) : null}
      </QueryBoundary>
    </div>
  );
}

interface RunMutationView {
  isPending: boolean;
  isError: boolean;
  error: Error | null;
  mutate: () => void;
}

function RunControls({ run, cancel, retry, submit }: {
  run: RunSummary;
  cancel: RunMutationView;
  retry: RunMutationView;
  submit: RunMutationView;
}) {
  const [armed, setArmed] = useState<"cancel" | "retry" | "submit" | null>(null);
  const active = ["SUBMITTED", "PENDING", "RUNNING", "COMPLETING"].includes(run.state);
  const retryable = ["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "CANCELLED"].includes(
    run.state,
  );
  const cloneable = ["SUCCEEDED", "FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "CANCELLED"]
    .includes(run.state) && Boolean(run.contract_id);
  const prepared = run.state === "VALIDATED";
  const perform = (action: "cancel" | "retry" | "submit", mutation: RunMutationView) => {
    if (armed !== action) {
      setArmed(action);
      return;
    }
    mutation.mutate();
    setArmed(null);
  };
  if (!active && !cloneable && !prepared) return null;
  const error = cancel.error ?? retry.error ?? submit.error;
  return (
    <section className="run-controls" aria-label="Run 写操作">
      <div>
        <strong>对象级确认</strong>
        <span>{armed ? `再次点击确认 ${armed}` : "第一次点击只会进入确认状态。"}</span>
      </div>
      <div className="agent-action-row">
        {active ? <button className="button danger" type="button" disabled={cancel.isPending} onClick={() => perform("cancel", cancel)}><XCircle aria-hidden="true" size={15} />{armed === "cancel" ? `确认取消 ${run.job_id ?? run.run_id}` : "取消 Run"}</button> : null}
        {cloneable ? <button className="button secondary" type="button" disabled={retry.isPending} onClick={() => perform("retry", retry)}><RotateCcw aria-hidden="true" size={15} />{armed === "retry" ? "确认创建派生 Run" : retryable ? "准备重试" : "克隆 Run"}</button> : null}
        {prepared ? <button className="button primary" type="button" disabled={submit.isPending} onClick={() => perform("submit", submit)}><Upload aria-hidden="true" size={15} />{armed === "submit" ? "确认提交此 Run" : "提交 Run"}</button> : null}
      </div>
      {error ? <p role="alert">{error.message}</p> : null}
    </section>
  );
}

function TimelineView({ events, lineage, currentRunId, onCompare }: {
  events: ReturnType<typeof useRunEvents>;
  lineage: ReturnType<typeof useRunLineage>;
  currentRunId: string;
  onCompare: (runId: string) => void;
}) {
  return (
    <div className="evidence-section timeline-view">
      <header><h3>状态时间线与 lineage</h3><p>事件按服务器 event ID 排序；依赖边和重试边来自持久 Run graph。</p></header>
      <QueryBoundary pending={events.isPending || lineage.isPending} error={events.error ?? lineage.error}>
        <div className="lineage-nodes">
          {(lineage.data?.nodes ?? []).map((node) => (
            <article key={node.run_id} className={node.run_id === currentRunId ? "current" : undefined}>
              <span><StatusBadge label={node.state} tone={node.state === "SUCCEEDED" ? "success" : node.state === "FAILED" ? "danger" : "info"} /><small>attempt {node.attempt ?? 1}</small></span>
              <strong className="mono wrap-anywhere">{node.run_id}</strong>
              <small>{node.lineage_reason ?? "root"}</small>
              {node.run_id !== currentRunId ? <button type="button" onClick={() => onCompare(node.run_id)}>与当前 Run 对比</button> : null}
            </article>
          ))}
        </div>
        {(lineage.data?.edges.length ?? 0) > 0 ? <ul className="lineage-edges">{lineage.data?.edges.map((edge) => <li key={`${edge.source_run_id}:${edge.target_run_id}:${edge.type}`}><span className="mono">{edge.source_run_id}</span><strong>{edge.type}</strong><span className="mono">{edge.target_run_id}</span><small>{edge.reason ?? "—"}</small></li>)}</ul> : null}
        <ol className="run-event-list">
          {(events.data?.items ?? []).map((event) => (
            <li key={event.event_id}>
              <span>#{event.event_id}</span>
              <div><strong>{event.event_type}</strong><small>{formatTimestamp(event.created_at)}</small><pre><code>{JSON.stringify(event.payload, null, 2)}</code></pre></div>
            </li>
          ))}
        </ol>
      </QueryBoundary>
    </div>
  );
}

function CompareView({ current, currentObjects, comparison, comparisonEvidence, candidates, compareRunId, onSelect }: {
  current: RunSummary;
  currentObjects: EvidenceObject[];
  comparison: ReturnType<typeof useRun>;
  comparisonEvidence: ReturnType<typeof useRunEvidence>;
  candidates: RunSummary[];
  compareRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  const alternatives = candidates.filter((item) => item.run_id !== current.run_id);
  const rows = comparison.data
    ? runComparisonRows(current, currentObjects, comparison.data, comparisonEvidence.data?.objects ?? [])
    : [];
  return (
    <div className="evidence-section compare-view">
      <header><h3>Run / Evidence 对比</h3><p>只比较服务器事实；差异本身不等于修复成功。</p></header>
      <label className="select-field compare-selector"><GitCompare aria-hidden="true" size={16} /><span className="sr-only">选择对比 Run</span><select value={compareRunId ?? ""} onChange={(event) => event.target.value && onSelect(event.target.value)}><option value="">选择 lineage 中的 Run</option>{alternatives.map((item) => <option key={item.run_id} value={item.run_id}>{item.run_id} · {item.state}</option>)}</select></label>
      <QueryBoundary pending={Boolean(compareRunId) && (comparison.isPending || comparisonEvidence.isPending)} error={comparison.error ?? comparisonEvidence.error} empty={!compareRunId} emptyTitle="没有可对比 Run" emptyDetail="从 timeline 选择同一 lineage 的 source 或 derived Run。">
        {comparison.data ? <div className="comparison-table"><div className="comparison-head"><span>字段</span><strong>当前</strong><strong>对比</strong></div>{rows.map((row) => <div key={row.label} className={row.changed ? "changed" : undefined}><span>{row.label}</span><code>{row.current}</code><code>{row.other}</code></div>)}</div> : null}
      </QueryBoundary>
    </div>
  );
}

export function runComparisonRows(
  current: RunSummary,
  currentObjects: EvidenceObject[],
  other: RunSummary,
  otherObjects: EvidenceObject[],
) {
  const values = [
    ["State", current.state, other.state],
    ["Terminal", current.terminal_state ?? "—", other.terminal_state ?? "—"],
    ["Exit", current.exit_code ?? "—", other.exit_code ?? "—"],
    ["Result", current.result_status, other.result_status],
    ["Collection", current.collection_state, other.collection_state],
    ["Diagnosis", current.diagnosis_state, other.diagnosis_state],
    ["Contract", current.contract_id ?? "—", other.contract_id ?? "—"],
    ["Evidence objects", String(currentObjects.length), String(otherObjects.length)],
    ["Finalized evidence", String(currentObjects.filter((item) => item.finalized_at).length), String(otherObjects.filter((item) => item.finalized_at).length)],
  ] as const;
  return values.map(([label, currentValue, otherValue]) => ({
    label,
    current: currentValue,
    other: otherValue,
    changed: currentValue !== otherValue,
  }));
}

function Overview({ user, run, objects, tasks, remediation }: { user: string; run: RunSummary; objects: EvidenceObject[]; tasks: Array<{ task_id: number; task_type: string; state: string; attempts: number; updated_at: string }>; remediation: { isPending: boolean; isError: boolean; error: Error | null; mutate: () => void } }) {
  const categories = [...new Set(objects.map((item) => item.category))];
  return (
    <div className="evidence-section">
      <div className="evidence-summary-strip">
        <div><strong>{objects.length}</strong><span>Evidence objects</span></div>
        <div><strong>{tasks.filter((item) => item.state === "succeeded").length}/{tasks.length}</strong><span>Collection tasks</span></div>
        <div><strong>{categories.length}</strong><span>Categories</span></div>
      </div>
      <dl className="fact-list evidence-facts">
        <div><dt>Job ID</dt><dd className="mono">{run.job_id ?? "—"}</dd></div>
        <div><dt>Contract</dt><dd className="mono wrap-anywhere">{run.contract_id ?? "—"}</dd></div>
        <div><dt>Workdir</dt><dd className="mono wrap-anywhere">{run.workdir ?? "服务器 read model 未公开"}</dd></div>
        <div><dt>Terminal</dt><dd>{run.terminal_state ?? run.state}</dd></div>
        <div><dt>Exit</dt><dd className="mono">{run.exit_code ?? "—"}</dd></div>
        <div><dt>Result</dt><dd>{run.result_status}</dd></div>
        <div><dt>Updated</dt><dd>{formatTimestamp(run.updated_at)}</dd></div>
      </dl>
      {run.job_id ? <NativeCommands user={user} run={run} /> : null}
      <section className="collection-tasks"><h3>Collection tasks</h3><ul>{tasks.map((task) => <li key={task.task_id}><FactState status={task.state} /><span>{task.task_type}</span><small>attempt {task.attempts}</small></li>)}</ul></section>
      {["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED"].includes(run.state) ? (
        <section className="run-agent-entry">
          <div><Bot aria-hidden="true" /><span><strong>启动受控修复</strong><small>基于当前 Diagnosis 和 Evidence 创建 owner-scoped 会话。</small></span></div>
          <button className="button secondary" type="button" disabled={remediation.isPending} onClick={() => remediation.mutate()}>{remediation.isPending ? "创建中" : "进入 Agent"}</button>
          {remediation.isError ? <p role="alert">{remediation.error?.message ?? "创建修复会话失败"}</p> : null}
        </section>
      ) : null}
    </div>
  );
}

function NativeCommands({ user, run }: { user: string; run: RunSummary }) {
  const [copied, setCopied] = useState<string | null>(null);
  const commands = nativeRunCommands(run.job_id ?? "", run.workdir ?? null);
  if (!commands.length) return null;
  const copy = async (label: string, command: string) => {
    await navigator.clipboard.writeText(command);
    setCopied(label);
  };
  return (
    <section className="native-commands">
      <header><div><h3>原生命令</h3><p>仅复制到剪贴板，不在浏览器或登录节点自动执行。</p></div><a href={`/terminal?user=${encodeURIComponent(user)}&run=${encodeURIComponent(run.run_id)}`}>终端协同</a></header>
      {commands.map((item) => <div key={item.label}><span><strong>{item.label}</strong>{item.dangerous ? <small>会修改作业状态</small> : null}</span><code>{item.command}</code><button type="button" aria-label={`复制 ${item.label}`} onClick={() => void copy(item.label, item.command)}><Copy aria-hidden="true" size={14} />{copied === item.label ? "已复制" : "复制"}</button></div>)}
    </section>
  );
}

export function nativeRunCommands(jobId: string, workdir: string | null = null) {
  if (!/^[A-Za-z0-9_.\[\]-]{1,128}$/.test(jobId)) return [];
  const value = shellQuote(jobId);
  const commands = [
    { label: "Queue", command: `squeue --jobs ${value} --format='%i %T %R %M %l'`, dangerous: false },
    { label: "Detail", command: `scontrol show job ${value}`, dangerous: false },
    { label: "Accounting", command: `sacct --jobs ${value} --format=JobID,State,ExitCode,Elapsed,AllocTRES`, dangerous: false },
    { label: "Cancel", command: `scancel -- ${value}`, dangerous: true },
  ];
  if (workdir?.startsWith("/") && workdir.length <= 4096 && !/[\r\n\0]/.test(workdir)) {
    commands.splice(3, 0, {
      label: "Output tail",
      command: `tail -n 200 -- ${shellQuote(`${workdir.replace(/\/$/, "")}/slurm-${jobId}.out`)}`,
      dangerous: false,
    });
  }
  return commands;
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

function ObjectPreviewView({ title, detail, objects, selected, preview, onSelect, contentMode }: { title: string; detail: string; objects: EvidenceObject[]; selected: EvidenceObject | null; preview: ReturnType<typeof useEvidenceObject>; onSelect: (item: EvidenceObject) => void; contentMode: "log" | "raw" }) {
  const grouped = [...new Set(objects.map((item) => item.category))];
  return (
    <div className="evidence-section object-view">
      <header><h3>{title}</h3><p>{detail}</p></header>
      {objects.length ? <div className="evidence-object-groups">{grouped.map((category) => <section key={category}><h4>{category}</h4><div>{objects.filter((item) => item.category === category).map((item) => <button key={item.object_id} type="button" className={selected?.object_id === item.object_id ? "active" : undefined} onClick={() => onSelect(item)}><span>{item.logical_path}</span><small>{formatBytes(item.size_bytes)} · {item.collection_status}</small></button>)}</div></section>)}</div> : <EmptyEvidence title="尚无可用对象" detail="Evidence collector 尚未登记此类对象。" />}
      <PreviewPane selected={selected} preview={preview} contentMode={contentMode} />
    </div>
  );
}

function PreviewPane({ selected, preview, contentMode }: { selected: EvidenceObject | null; preview: ReturnType<typeof useEvidenceObject>; contentMode: "log" | "raw" }) {
  if (!selected) return <EmptyEvidence title="选择一个 Evidence object" detail="对象 ID 和选择状态会写入 URL，便于分享同一证据视图。" />;
  return (
    <section className="evidence-preview" aria-label="Evidence 内容预览">
      <div className="preview-heading"><div><strong className="mono wrap-anywhere">{selected.logical_path}</strong><small>{selected.mime_type ?? "unknown"} · sha256 {selected.sha256?.slice(0, 16) ?? "—"}</small></div><StatusBadge label={selected.collection_status} tone={selected.collection_status === "collected" ? "success" : "warning"} /></div>
      <QueryBoundary pending={preview.isPending} error={preview.error}>
        {preview.data?.preview.available ? <><PreviewIntegrity preview={preview.data} /><pre><code>{previewContent(preview.data, contentMode)}</code></pre></> : <EmptyEvidence title="此对象不支持文本预览" detail={preview.data?.preview.reason ?? "只展示元数据；不会把二进制内容强行解码。"} />}
      </QueryBoundary>
    </section>
  );
}

function PreviewIntegrity({ preview }: { preview: EvidenceObjectPreview }) {
  const integrity = preview.preview.integrity ?? "not_checked";
  return <div className={`preview-integrity ${integrity === "mismatch" ? "danger" : ""}`}><ShieldCheck aria-hidden="true" /><span>{integrity === "verified" ? "完整内容 digest 已验证" : integrity === "mismatch" ? "内容与登记 digest 不一致" : "截断预览未执行完整 digest 校验"}{preview.preview.truncated ? ` · 已截断到 ${formatBytes(preview.preview.bytes_read ?? 0)}` : ""}</span></div>;
}

function ResultsView({ summary, outputs, selected, preview, onSelect }: { summary: ReturnType<typeof useEvidenceObject>; outputs: EvidenceObject[]; selected: EvidenceObject | null; preview: ReturnType<typeof useEvidenceObject>; onSelect: (item: EvidenceObject) => void }) {
  const payload = parseJsonObject(summary.data?.preview.content);
  const result = asObject(payload?.outputs);
  return (
    <div className="evidence-section">
      <header><h3>结果与输出</h3><p>结果摘要来自 `derived/result_summary.v1.json`，文件内容仍通过独立 Evidence object 读取。</p></header>
      <QueryBoundary pending={summary.isPending} error={summary.error}>
        {payload ? <div className="result-summary"><div><strong>{String(payload.result_status ?? "—")}</strong><span>Result status</span></div><div><strong>{String(result?.file_count ?? 0)}</strong><span>Output files</span></div><div><strong>{formatBytes(numberValue(result?.total_size_bytes))}</strong><span>Total size</span></div></div> : <EmptyEvidence title="尚无结果摘要" detail="collection 完成后才会生成 derived result summary。" />}
      </QueryBoundary>
      <div className="output-object-list">{outputs.map((item) => <button key={item.object_id} type="button" className={selected?.object_id === item.object_id ? "active" : undefined} onClick={() => onSelect(item)}><span><strong>{item.logical_path}</strong><small>{item.mime_type ?? "unknown"}</small></span><span>{formatBytes(item.size_bytes)}</span></button>)}</div>
      <PreviewPane selected={selected} preview={preview} contentMode="raw" />
    </div>
  );
}

function DiagnosisView({ pending, error, state, items, diagnosing, onDiagnose, onEvidence }: { pending: boolean; error: unknown; state: string; items: DiagnosisRecordPayload[]; diagnosing: boolean; onDiagnose: () => void; onEvidence: (ref: string) => void }) {
  return (
    <div className="evidence-section">
      <header className="section-action-heading"><div><h3>规则诊断</h3><p>结论必须引用已登记 Evidence；建议 patch 不会自动执行。</p></div><button className="button secondary" type="button" disabled={diagnosing} onClick={onDiagnose}>{diagnosing ? "诊断中" : "重新运行规则诊断"}</button></header>
      <QueryBoundary pending={pending} error={error}>
        <div className="diagnosis-state"><FactState status={state} /><span>{items.length} 条诊断</span></div>
        {items.length ? <div className="diagnosis-list">{items.map((item) => <article key={item.diagnosis_id}><header><StatusBadge label={item.severity} tone={item.severity === "error" ? "danger" : "warning"} /><span>{item.rule_id}</span><small>{item.confidence}</small></header><h4>{item.summary}</h4><p>{item.fix_guide.fix ?? "没有结构化修复说明。"}</p>{Object.keys(item.suggested_patch).length ? <pre><code>{JSON.stringify(item.suggested_patch, null, 2)}</code></pre> : null}<div className="diagnosis-evidence">{item.evidence_refs.map((ref) => <button key={ref} type="button" onClick={() => onEvidence(ref)}>{ref}</button>)}</div></article>)}</div> : <EmptyEvidence title="没有匹配的已知错误" detail="这不是“没有失败”的证明；只表示当前规则和已收集证据未形成诊断。" />}
      </QueryBoundary>
    </div>
  );
}

function CapsuleView({ pending, error, state, detail, collectionReady, building, onBuild }: { pending: boolean; error: unknown; state: string; detail: { capsule_id: string; manifest_sha256: string; files_copied: number; valid?: boolean; checked_files?: number; manifest?: JsonObject; warnings: string[]; errors?: string[] } | null; collectionReady: boolean; building: boolean; onBuild: () => void }) {
  return (
    <div className="evidence-section">
      <header className="section-action-heading"><div><h3>Raw Capsule</h3><p>只复制 Evidence manifest 已登记对象，并生成 provenance、policy 和 checksums。</p></div>{!detail ? <button className="button secondary" type="button" disabled={!collectionReady || building || state === "running"} onClick={onBuild}>{building || state === "running" ? "构建中" : "构建 Raw Capsule"}</button> : null}</header>
      <QueryBoundary pending={pending} error={error}>
        <div className="diagnosis-state"><FactState status={state} /><span>{collectionReady ? "Evidence collection complete" : "等待完整 Evidence"}</span></div>
        {detail ? <div className="capsule-detail"><div className="capsule-verification">{detail.valid ? <CheckCircle2 aria-hidden="true" /> : <Bug aria-hidden="true" />}<div><strong>{detail.valid ? "Capsule checksum 验证通过" : "Capsule 验证失败"}</strong><span>{detail.checked_files ?? 0} checked files · {detail.files_copied} copied</span></div></div><dl className="fact-list"><div><dt>Capsule ID</dt><dd className="mono wrap-anywhere">{detail.capsule_id}</dd></div><div><dt>Manifest sha256</dt><dd className="mono wrap-anywhere">{detail.manifest_sha256}</dd></div></dl>{detail.warnings.length ? <ul className="capsule-messages warning">{detail.warnings.map((item) => <li key={item}>{item}</li>)}</ul> : null}{detail.errors?.length ? <ul className="capsule-messages danger">{detail.errors.map((item) => <li key={item}>{item}</li>)}</ul> : null}<details><summary>Manifest</summary><pre><code>{JSON.stringify(detail.manifest ?? {}, null, 2)}</code></pre></details></div> : <EmptyEvidence title={collectionReady ? "尚未构建 Raw Capsule" : "Capsule 尚未就绪"} detail={collectionReady ? "构建只固化已收集 Evidence，不会重新提交作业。" : "等待 collection succeeded 后才能构建。"} />}
      </QueryBoundary>
    </div>
  );
}

function EmptyEvidence({ title, detail }: { title: string; detail: string }) {
  return <div className="evidence-empty"><strong>{title}</strong><p>{detail}</p></div>;
}
