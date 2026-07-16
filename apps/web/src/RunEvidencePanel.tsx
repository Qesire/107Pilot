import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  Boxes,
  Bug,
  CheckCircle2,
  FileText,
  FolderTree,
  ScrollText,
  ShieldCheck,
} from "lucide-react";
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
import { useEvidenceObject, useRunCapsule, useRunDiagnoses, useRunEvidence } from "./query";
import type {
  DiagnosisRecordPayload,
  EvidenceObject,
  EvidenceObjectPreview,
  JsonObject,
  RunSummary,
} from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

type EvidenceTab = "overview" | "logs" | "results" | "diagnosis" | "capsule" | "objects";

const tabs: Array<{ id: EvidenceTab; label: string; icon: typeof FileText }> = [
  { id: "overview", label: "摘要", icon: FileText },
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
  const objects = evidence.data?.objects ?? [];
  const logs = objects.filter((item) => item.category === "logs");
  const outputs = objects.filter((item) => item.category === "outputs");
  const activeObject = selectActiveEvidenceObject(objects, tab, requestedObjectId);
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
  const setView = (nextTab: EvidenceTab, objectId: string | null = null) =>
    navigate(withSearch(location.pathname, location.search, { tab: nextTab, object: objectId }));

  return (
    <div className="evidence-workbench">
      <nav className="evidence-tabs" aria-label="Run Evidence 视图">
        {tabs.map((item) => {
          const Icon = item.icon;
          return <button key={item.id} type="button" className={tab === item.id ? "active" : undefined} aria-current={tab === item.id ? "page" : undefined} onClick={() => setView(item.id)}><Icon aria-hidden="true" />{item.label}</button>;
        })}
      </nav>

      <QueryBoundary pending={evidence.isPending} error={evidence.error}>
        {tab === "overview" ? <Overview run={run} objects={objects} tasks={evidence.data?.tasks ?? []} /> : null}
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

function Overview({ run, objects, tasks }: { run: RunSummary; objects: EvidenceObject[]; tasks: Array<{ task_id: number; task_type: string; state: string; attempts: number; updated_at: string }> }) {
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
        <div><dt>Terminal</dt><dd>{run.terminal_state ?? run.state}</dd></div>
        <div><dt>Exit</dt><dd className="mono">{run.exit_code ?? "—"}</dd></div>
        <div><dt>Result</dt><dd>{run.result_status}</dd></div>
        <div><dt>Updated</dt><dd>{formatTimestamp(run.updated_at)}</dd></div>
      </dl>
      <section className="collection-tasks"><h3>Collection tasks</h3><ul>{tasks.map((task) => <li key={task.task_id}><FactState status={task.state} /><span>{task.task_type}</span><small>attempt {task.attempts}</small></li>)}</ul></section>
    </div>
  );
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
