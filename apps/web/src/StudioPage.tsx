import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import Ajv2020 from "ajv/dist/2020";
import type { ErrorObject, ValidateFunction } from "ajv";
import {
  AlertTriangle,
  Braces,
  CheckCircle2,
  ClipboardCheck,
  Code2,
  FileCode2,
  Save,
  ShieldAlert,
  TerminalSquare,
} from "lucide-react";
import { api } from "./api";
import {
  createDefaultContract,
  diffText,
  linesToStrings,
  parseContractSource,
  parseJsonObject,
  readContractValue,
  serializeContract,
  stringsToLines,
  updateContractPath,
  type SourceFormat,
} from "./contract-state";
import { QueryBoundary, SectionHeading, StatusBadge } from "./components";
import { useContract, useContractSchema, useRecipes } from "./query";
import type { JsonObject } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

interface StudioPageProps {
  user: string;
  location: LocationState;
  navigate: (path: string, options?: { replace?: boolean }) => void;
}

type StudioTab = "basic" | "advanced" | "source" | "script" | "terminal";

const studioTabs: Array<{ id: StudioTab; label: string; icon: typeof Braces }> = [
  { id: "basic", label: "基础", icon: Braces },
  { id: "advanced", label: "高级", icon: ShieldAlert },
  { id: "source", label: "源码", icon: Code2 },
  { id: "script", label: "脚本", icon: FileCode2 },
  { id: "terminal", label: "终端协同", icon: TerminalSquare },
];

const ContractSourceEditor = lazy(() => import("./ContractSourceEditor"));

export function StudioPage({ user, location, navigate }: StudioPageProps) {
  const contractId = location.pathname === "/studio/new"
    ? null
    : decodeURIComponent(location.pathname.slice("/studio/".length));
  const requestedTab = location.search.get("tab");
  const tab = studioTabs.some((item) => item.id === requestedTab)
    ? requestedTab as StudioTab
    : "basic";
  const format: SourceFormat = location.search.get("format") === "json" ? "json" : "yaml";
  const schemaQuery = useContractSchema(user);
  const recipes = useRecipes(user);
  const existing = useContract(user, contractId);
  const [canonical, setCanonical] = useState<JsonObject>(() => createDefaultContract());
  const [source, setSource] = useState(() => serializeContract(createDefaultContract(), format));
  const [sourceDirty, setSourceDirty] = useState(false);
  const [sourceConflict, setSourceConflict] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [canonicalDirty, setCanonicalDirty] = useState(false);
  const hydratedId = useRef<string | null>(null);

  const validation = useMutation({
    mutationFn: () => api.validateContract(user, canonical),
    onSuccess: (result) => {
      setCanonical(result.effective_request.contract);
      if (!sourceDirty && tab === "source") setSource(serializeContract(result.effective_request.contract, format));
    },
  });
  const creation = useMutation({
    mutationFn: () => api.createContract(user, canonical),
    onSuccess: (result) => {
      navigate(`/studio/${result.contract_id}?user=${encodeURIComponent(user)}&tab=script`, { replace: true });
    },
  });

  useEffect(() => {
    if (!contractId) {
      hydratedId.current = null;
      return;
    }
    if (existing.data && hydratedId.current !== contractId) {
      const loaded = structuredClone(existing.data.contract);
      hydratedId.current = contractId;
      setCanonical(loaded);
      setSource(serializeContract(loaded, format));
      setSourceDirty(false);
      setSourceConflict(false);
      setSourceError(null);
      setCanonicalDirty(false);
      if (validation.data?.effective_request.contract_digest !== existing.data.digest) {
        validation.reset();
      }
      creation.reset();
    }
  }, [contractId, creation, existing.data, format, validation]);

  useEffect(() => {
    if (!sourceDirty) setSource(serializeContract(canonical, format));
  }, [format]); // canonical changes are synchronized through commitCanonical.

  const validator = useMemo<ValidateFunction | null>(() => {
    if (!schemaQuery.data) return null;
    try {
      return new Ajv2020({ allErrors: true, strict: false }).compile(schemaQuery.data);
    } catch {
      return null;
    }
  }, [schemaQuery.data]);
  const clientErrors = useMemo<ErrorObject[]>(() => {
    if (!validator) return [];
    return validator(canonical) ? [] : [...(validator.errors ?? [])];
  }, [canonical, validator]);

  const commitCanonical = (next: JsonObject) => {
    setCanonical(next);
    setCanonicalDirty(true);
    validation.reset();
    creation.reset();
    if (sourceDirty) {
      setSourceConflict(true);
    } else if (tab === "source") {
      setSource(serializeContract(next, format));
    }
  };
  const update = (path: readonly string[], value: unknown) =>
    commitCanonical(updateContractPath(canonical, path, value));
  const switchTab = (next: StudioTab) => {
    if (next === "source" && !sourceDirty) setSource(serializeContract(canonical, format));
    navigate(withSearch(location.pathname, location.search, { tab: next }));
  };
  const switchFormat = (next: SourceFormat) => {
    if (sourceDirty) {
      setSourceError("源码有未应用修改；请先应用或放弃，再切换格式。");
      return;
    }
    navigate(withSearch(location.pathname, location.search, { format: next, tab: "source" }));
  };
  const applySource = () => {
    try {
      const parsed = parseContractSource(source, format);
      setCanonical(parsed);
      setCanonicalDirty(true);
      setSource(serializeContract(parsed, format));
      setSourceDirty(false);
      setSourceConflict(false);
      setSourceError(null);
      validation.reset();
      creation.reset();
    } catch (error) {
      setSourceError(error instanceof Error ? error.message : "源码解析失败");
    }
  };
  const discardSource = () => {
    setSource(serializeContract(canonical, format));
    setSourceDirty(false);
    setSourceConflict(false);
    setSourceError(null);
  };
  const busy = validation.isPending || creation.isPending;
  const blocked = sourceDirty || clientErrors.length > 0 || busy;

  return (
    <>
      <SectionHeading
        eyebrow="Contract Studio / canonical state"
        title={contractId ? "检查与派生 Contract" : "新建 Contract"}
        detail="五个投影共享同一 canonical object；客户端提示只用于即时反馈，服务端 validation 始终是最终权威。"
      />

      <QueryBoundary pending={schemaQuery.isPending || recipes.isPending || (Boolean(contractId) && existing.isPending)} error={schemaQuery.error ?? recipes.error ?? existing.error}>
        <div className="studio-shell">
          <header className="studio-toolbar">
            <nav aria-label="Contract Studio 投影" className="studio-tabs">
              {studioTabs.map((item) => {
                const Icon = item.icon;
                return (
                  <button key={item.id} type="button" className={tab === item.id ? "active" : undefined} aria-current={tab === item.id ? "page" : undefined} onClick={() => switchTab(item.id)}>
                    <Icon aria-hidden="true" size={15} /> {item.label}
                  </button>
                );
              })}
            </nav>
            <div className="studio-actions">
              <StatusBadge
                label={sourceDirty ? "源码未应用" : clientErrors.length ? `${clientErrors.length} 个客户端问题` : "客户端结构通过"}
                tone={sourceDirty || clientErrors.length ? "warning" : "success"}
              />
              <button className="button secondary" type="button" disabled={blocked} onClick={() => validation.mutate()}>
                <ClipboardCheck aria-hidden="true" size={16} /> {validation.isPending ? "校验中" : "服务端校验"}
              </button>
              <button className="button primary" type="button" disabled={blocked || validation.data?.status !== "OK"} onClick={() => creation.mutate()}>
                <Save aria-hidden="true" size={16} /> {creation.isPending ? "保存中" : contractId ? "另存为新 Contract" : "创建 Contract"}
              </button>
            </div>
          </header>

          <div className="studio-context" aria-label="Contract 固定上下文">
            <span><strong>Recipe</strong>{readContractValue(canonical, ["recipe_version_id"], "—")}</span>
            <span><strong>Contract</strong>{contractId ?? "尚未持久化"}</span>
            <span><strong>Digest</strong>{validation.data?.effective_request.contract_digest ?? existing.data?.digest ?? "校验后生成"}</span>
          </div>
          {existing.data?.derivation_reason ? <div className="studio-lineage"><strong>Lineage</strong><span>{existing.data.derivation_reason}</span>{existing.data.parent_contract_id ? <span className="mono">parent {existing.data.parent_contract_id}</span> : null}{location.search.get("adoption") ? <span className="mono">adoption {location.search.get("adoption")}</span> : null}</div> : null}

          {sourceConflict ? (
            <div className="studio-notice warning" role="alert">
              <AlertTriangle aria-hidden="true" />
              <div><strong>表单与未应用源码发生冲突</strong><p>表单更新没有覆盖源码草稿。请选择“应用源码”或“放弃源码修改”。</p></div>
            </div>
          ) : null}
          {sourceError ? <div className="studio-notice error" role="alert"><AlertTriangle aria-hidden="true" /><div><strong>源码未应用</strong><p>{sourceError}</p></div></div> : null}
          {validation.isError || creation.isError ? (
            <div className="studio-notice error" role="alert"><AlertTriangle aria-hidden="true" /><div><strong>服务器拒绝请求</strong><p>{(validation.error ?? creation.error)?.message}</p></div></div>
          ) : null}

          <div className="studio-body">
            <section className="studio-main" aria-label={`${tab} 投影`}>
              {tab === "basic" ? <BasicProjection contract={canonical} recipes={recipes.data?.items ?? []} update={update} /> : null}
              {tab === "advanced" ? <AdvancedProjection contract={canonical} update={update} /> : null}
              {tab === "source" ? (
                <SourceProjection
                  format={format}
                  source={source}
                  schema={schemaQuery.data ?? {}}
                  dirty={sourceDirty}
                  conflict={sourceConflict}
                  onFormat={switchFormat}
                  onChange={(next) => { setSource(next); setSourceDirty(true); setSourceError(null); }}
                  onApply={applySource}
                  onDiscard={discardSource}
                />
              ) : null}
              {tab === "script" ? <ScriptProjection validation={validation.data} contract={canonical} /> : null}
              {tab === "terminal" ? <TerminalProjection user={user} contractId={contractId} /> : null}
            </section>
            <aside className="studio-side" aria-label="校验结果">
              <h2>校验与风险</h2>
              <p className="side-detail">Ajv 即时检查 schema；提交动作仍调用服务器 materializer 和 preflight。</p>
              {clientErrors.length ? (
                <ul className="finding-list client">
                  {clientErrors.slice(0, 8).map((error, index) => (
                    <li key={`${error.instancePath}-${error.keyword}-${index}`}><strong>{error.instancePath || "/"}</strong><span>{error.message ?? error.keyword}</span></li>
                  ))}
                </ul>
              ) : <div className="validation-ok"><CheckCircle2 aria-hidden="true" /><span><strong>客户端结构通过</strong><small>等待或已完成服务端校验。</small></span></div>}
              {validation.data ? (
                <>
                  <div className="server-validation-heading"><StatusBadge label={`服务器 ${validation.data.status}`} tone={validation.data.status === "BLOCK" ? "danger" : "success"} /></div>
                  {validation.data.findings.length ? (
                    <ul className="finding-list server">
                      {validation.data.findings.map((finding, index) => <li key={`${finding.code}-${index}`}><strong>{finding.code}</strong><span>{finding.message}</span></li>)}
                    </ul>
                  ) : <p className="no-findings">无服务端 findings。</p>}
                </>
              ) : null}
            </aside>
          </div>
          {contractId ? <RunLaunchPanel user={user} contractId={contractId} localDirty={canonicalDirty} navigate={navigate} /> : null}
        </div>
      </QueryBoundary>
    </>
  );
}

function RunLaunchPanel({ user, contractId, localDirty, navigate }: { user: string; contractId: string; localDirty: boolean; navigate: (path: string) => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const preflight = useMutation({
    mutationFn: () => api.preflightContract(user, contractId),
    onMutate: () => { prepare.reset(); setConfirmed(false); },
  });
  const prepare = useMutation({
    mutationFn: () => api.prepareRun(user, contractId),
    onMutate: () => setConfirmed(false),
  });
  const submit = useMutation({
    mutationFn: () => api.submitRun(user, prepare.data?.run_id ?? ""),
    onSuccess: (run) => navigate(`/runs/${encodeURIComponent(run.run_id)}?user=${encodeURIComponent(user)}`),
  });
  useEffect(() => {
    if (localDirty) setConfirmed(false);
  }, [localDirty]);
  const preflightOk = preflight.data?.status === "OK";
  return (
    <section className="run-launch" aria-labelledby="run-launch-heading">
      <div className="run-launch-heading"><div><p className="panel-kicker">Preflight → prepare → submit</p><h2 id="run-launch-heading">从此 immutable Contract 启动 Run</h2><p>prepare 只固化 Run 与预览；真正提交 Slurm 需要对具体 Run ID 再次确认。</p></div><div className="run-launch-actions"><button className="button secondary" type="button" disabled={localDirty || preflight.isPending} onClick={() => preflight.mutate()}>{preflight.isPending ? "预检中" : "运行动态预检"}</button><button className="button secondary" type="button" disabled={localDirty || !preflightOk || prepare.isPending} onClick={() => prepare.mutate()}>{prepare.isPending ? "准备中" : "准备 Run"}</button></div></div>
      {localDirty ? <div className="studio-notice warning" role="alert"><AlertTriangle aria-hidden="true" /><div><strong>本地修改尚未持久化</strong><p>请先“另存为新 Contract”，再对新 Contract 运行 preflight/prepare。</p></div></div> : null}
      {preflight.isError || prepare.isError || submit.isError ? <div className="studio-notice error" role="alert"><AlertTriangle aria-hidden="true" /><div><strong>Run 流程被服务器拒绝</strong><p>{(preflight.error ?? prepare.error ?? submit.error)?.message}</p></div></div> : null}
      {preflight.data ? <div className="run-preflight-result"><StatusBadge label={`Preflight ${preflight.data.status}`} tone={preflightOk ? "success" : "danger"} /><span className="mono">digest {preflight.data.effective_request.contract_digest}</span><span>{preflight.data.findings.length} findings</span></div> : null}
      {prepare.data ? <div className="run-confirm"><div><strong>Prepared Run</strong><p className="mono wrap-anywhere">{prepare.data.run_id}</p><p>Job 尚未提交；确认脚本、Contract 和风险后再执行。</p></div><pre className="script-preview"><code>{prepare.data.preview?.submitted_script ?? "服务器未返回 submitted script preview"}</code></pre><label className="check-field"><input type="checkbox" checked={confirmed} disabled={localDirty} onChange={(event) => setConfirmed(event.target.checked)} /><span><strong>我确认提交 Run {prepare.data.run_id}</strong><small>{localDirty ? "当前表单已有未持久化修改；旧 Prepared Run 已锁定，不能继续提交。" : "该动作会触发配置的 Slurm backend，不是界面预览。"}</small></span></label><button className="button primary wide" type="button" disabled={localDirty || !confirmed || submit.isPending} onClick={() => submit.mutate()}>{submit.isPending ? "提交中" : `确认提交 ${prepare.data.run_id}`}</button></div> : null}
    </section>
  );
}

function BasicProjection({ contract, recipes, update }: ProjectionProps & { recipes: Array<{ recipe_id: string; latest_version: string; title: string }> }) {
  const currentRecipe = readContractValue(contract, ["recipe_version_id"], "");
  const recipeOptions = recipes.map((recipe) => ({ value: `${recipe.recipe_id}@${recipe.latest_version}`, label: `${recipe.title} · ${recipe.latest_version}` }));
  if (currentRecipe && !recipeOptions.some((option) => option.value === currentRecipe)) {
    recipeOptions.unshift({ value: currentRecipe, label: `${currentRecipe} · 当前固定版本` });
  }
  const expected = readContractValue<unknown[]>(contract, ["outputs", "expected"], []);
  const stringOutputs = expected.filter((item): item is string => typeof item === "string");
  const typedOutputs = expected.filter((item) => typeof item !== "string");
  return (
    <div className="projection-stack">
      <ProjectionHeading title="基础模式" detail="任务、路径、资源和常用输出；高级字段保留在 canonical object 中。" />
      <fieldset className="field-group"><legend>任务</legend><div className="form-grid two">
        <SelectField label="Recipe version" value={currentRecipe} onChange={(value) => update(["recipe_version_id"], value)} options={recipeOptions} />
        <TextField label="项目名（可选）" value={readContractValue(contract, ["project", "name"], "")} onChange={(value) => update(["project", "name"], value)} />
        <TextField className="span-2" label="Workdir" value={readContractValue(contract, ["project", "workdir"], "")} onChange={(value) => update(["project", "workdir"], value)} />
        <TextField className="span-2" label="Command" multiline value={readContractValue(contract, ["entry", "command"], "")} onChange={(value) => update(["entry", "command"], value)} />
      </div></fieldset>
      <fieldset className="field-group"><legend>资源</legend><div className="form-grid three">
        <TextField label="Partition" value={readContractValue(contract, ["resources", "partition"], "")} onChange={(value) => update(["resources", "partition"], value)} />
        <TextField label="QoS" value={readContractValue(contract, ["resources", "qos"], "")} onChange={(value) => update(["resources", "qos"], value || null)} />
        <TextField label="Time limit" value={readContractValue(contract, ["resources", "time_limit"], "")} onChange={(value) => update(["resources", "time_limit"], value)} />
        <NumberField label="Nodes" value={readContractValue(contract, ["resources", "nodes"], 1)} min={1} onChange={(value) => update(["resources", "nodes"], value)} />
        <NumberField label="Tasks" value={readContractValue(contract, ["resources", "ntasks"], 1)} min={1} onChange={(value) => update(["resources", "ntasks"], value)} />
        <NumberField label="CPU / task" value={readContractValue(contract, ["resources", "cpus_per_task"], 1)} min={1} onChange={(value) => update(["resources", "cpus_per_task"], value)} />
        <TextField label="Memory" value={String(readContractValue(contract, ["resources", "memory"], ""))} onChange={(value) => update(["resources", "memory"], value || null)} />
        <NumberField label="GPU / node" value={readContractValue(contract, ["resources", "gpus_per_node"], 0) ?? 0} min={0} onChange={(value) => update(["resources", "gpus_per_node"], value)} />
      </div></fieldset>
      <fieldset className="field-group"><legend>输出</legend><div className="form-grid">
        <TextField label="预期输出（每行一个）" multiline value={stringOutputs.join("\n")} detail={typedOutputs.length ? `${typedOutputs.length} 个 typed output 会被原样保留。` : undefined} onChange={(value) => update(["outputs", "expected"], [...linesToStrings(value), ...typedOutputs])} />
      </div></fieldset>
    </div>
  );
}

interface ProjectionProps {
  contract: JsonObject;
  update: (path: readonly string[], value: unknown) => void;
}

function AdvancedProjection({ contract, update }: ProjectionProps) {
  return (
    <div className="projection-stack">
      <ProjectionHeading title="高级模式" detail="runtime、array、workflow、policy 与 extensions；未展示字段仍保留。" />
      <fieldset className="field-group"><legend>Runtime</legend><div className="form-grid two">
        <TextField label="Conda env" value={readContractValue(contract, ["runtime", "conda_env"], "") ?? ""} onChange={(value) => update(["runtime", "conda_env"], value || null)} />
        <TextField label="Container image" value={readContractValue(contract, ["runtime", "container_image"], "") ?? ""} onChange={(value) => update(["runtime", "container_image"], value || null)} />
        <TextField label="Modules（每行一个）" multiline value={stringsToLines(readContractValue(contract, ["runtime", "modules"], []))} onChange={(value) => update(["runtime", "modules"], linesToStrings(value))} />
        <JsonObjectField label="Environment JSON" value={readContractValue<JsonObject>(contract, ["runtime", "environment"], {})} onCommit={(value) => update(["runtime", "environment"], value)} />
      </div></fieldset>
      <fieldset className="field-group"><legend>Array 与 workflow</legend><div className="form-grid three">
        <TextField label="Array expression" value={readContractValue(contract, ["resources", "array", "expression"], "")} onChange={(value) => update(["resources", "array"], value ? { ...readContractValue<JsonObject>(contract, ["resources", "array"], {}), expression: value } : null)} />
        <NumberField label="Max concurrency" value={readContractValue(contract, ["resources", "array", "max_concurrency"], 1)} min={1} onChange={(value) => update(["resources", "array", "max_concurrency"], value)} disabled={!readContractValue(contract, ["resources", "array", "expression"], "")} />
        <TextField label="Dependencies（每行 Run ID）" multiline value={stringsToLines(readContractValue(contract, ["workflow", "dependencies"], []))} onChange={(value) => update(["workflow", "dependencies"], linesToStrings(value))} />
        <NumberField label="Retry attempts" value={readContractValue(contract, ["workflow", "retry", "max_attempts"], 1)} min={1} max={10} onChange={(value) => update(["workflow", "retry", "max_attempts"], value)} />
        <NumberField label="Backoff seconds" value={readContractValue(contract, ["workflow", "retry", "backoff_seconds"], 0)} min={0} max={86400} onChange={(value) => update(["workflow", "retry", "backoff_seconds"], value)} />
      </div></fieldset>
      <fieldset className="field-group"><legend>Policy 与 extensions</legend><div className="form-grid two">
        <SelectField label="Automation level" value={readContractValue(contract, ["policy", "automation_level"], "explain")} onChange={(value) => update(["policy", "automation_level"], value)} options={["explain", "suggest", "approved_execute", "bounded_auto"].map((value) => ({ value, label: value }))} />
        <NumberField label="Max remediation" value={readContractValue(contract, ["policy", "max_remediation_attempts"], 0)} min={0} max={10} onChange={(value) => update(["policy", "max_remediation_attempts"], value)} />
        <label className="check-field"><input type="checkbox" checked={readContractValue(contract, ["policy", "require_approval"], true)} onChange={(event) => update(["policy", "require_approval"], event.target.checked)} /><span><strong>执行前需要批准</strong><small>关闭前先确认 automation policy 与权限边界。</small></span></label>
        <JsonObjectField label="Extensions JSON（含 raw sbatch）" value={readContractValue<JsonObject>(contract, ["extensions"], {})} onCommit={(value) => update(["extensions"], value)} />
      </div></fieldset>
    </div>
  );
}

function SourceProjection({ format, source, schema, dirty, conflict, onFormat, onChange, onApply, onDiscard }: { format: SourceFormat; source: string; schema: JsonObject; dirty: boolean; conflict: boolean; onFormat: (format: SourceFormat) => void; onChange: (value: string) => void; onApply: () => void; onDiscard: () => void }) {
  return (
    <div className="projection-stack">
      <ProjectionHeading title="源码模式" detail="直接编辑 canonical Contract；只有“应用源码”才会替换共享状态。" />
      <div className="source-toolbar">
        <div className="segmented" aria-label="源码格式"><button type="button" className={format === "yaml" ? "active" : undefined} onClick={() => onFormat("yaml")}>YAML</button><button type="button" className={format === "json" ? "active" : undefined} onClick={() => onFormat("json")}>JSON</button></div>
        <div><button className="button secondary" type="button" disabled={!dirty} onClick={onDiscard}>放弃源码修改</button><button className="button primary" type="button" disabled={!dirty} onClick={onApply}>{conflict ? "应用源码并覆盖表单" : "应用源码"}</button></div>
      </div>
      <div className="source-editor" data-testid="contract-source-editor">
        <Suspense fallback={<div className="query-state" role="status">正在加载源码编辑器…</div>}>
          <ContractSourceEditor format={format} schema={schema} source={source} onChange={onChange} />
        </Suspense>
      </div>
    </div>
  );
}

function ScriptProjection({ validation, contract }: { validation: ReturnType<typeof api.validateContract> extends Promise<infer T> ? T | undefined : never; contract: JsonObject }) {
  if (!validation) return <ProjectionEmpty title="尚无 materialized script" detail="先应用所有源码修改并执行服务端校验。wrapper 与 original script 会在 Run prepare 后进入 Evidence。" />;
  const script = validation.effective_request.script;
  const command = readContractValue(contract, ["entry", "command"], "");
  const diff = script ? diffText(command, script) : [];
  return (
    <div className="projection-stack">
      <ProjectionHeading title="脚本模式" detail={`materializer: ${validation.effective_request.materializer} · digest: ${validation.effective_request.contract_digest}`} />
      {script ? <>
        <div className="script-section-heading"><h3>Entry command → materialized submitted script</h3><p>绿色为 materializer 增加的执行上下文；wrapper 在 Run prepare 后进入 Evidence。</p></div>
        <pre className="script-diff"><code>{diff.map((item, index) => <span className={`diff-${item.kind}`} key={`${item.kind}-${index}`}>{item.kind === "added" ? "+ " : item.kind === "removed" ? "- " : "  "}{item.line}{"\n"}</span>)}</code></pre>
        <div className="script-section-heading"><h3>Resolved script</h3></div>
        <pre className="script-preview"><code>{script}</code></pre>
      </> : <ProjectionEmpty title="脚本被阻断" detail="查看右侧服务端 findings；当前 Contract 无法 materialize。" />}
    </div>
  );
}

function TerminalProjection({ user, contractId }: { user: string; contractId: string | null }) {
  const validateCommand = `curl --fail-with-body \\\n  -H 'Content-Type: application/json' \\\n  -H 'X-Pilot107-User: ${user}' \\\n  --data-binary @contract.json \\\n  http://127.0.0.1:3000/api/v1/contracts/validate`;
  return (
    <div className="projection-stack">
      <ProjectionHeading title="终端协同" detail="下载/保存当前 JSON 为 contract.json 后，可用同一服务端契约继续；trusted header 命令仅限本地开发。" />
      <pre className="script-preview"><code>{validateCommand}</code></pre>
      <dl className="terminal-facts"><div><dt>当前 Contract</dt><dd className="mono wrap-anywhere">{contractId ?? "尚未持久化"}</dd></div><div><dt>关键对象</dt><dd>Contract ID 与 digest，而不是浏览器表单 session</dd></div></dl>
    </div>
  );
}

function ProjectionHeading({ title, detail }: { title: string; detail: string }) {
  return <header className="projection-heading"><h2>{title}</h2><p>{detail}</p></header>;
}

function ProjectionEmpty({ title, detail }: { title: string; detail: string }) {
  return <div className="projection-empty"><FileCode2 aria-hidden="true" /><div><strong>{title}</strong><p>{detail}</p></div></div>;
}

function TextField({ label, value, onChange, multiline, detail, className }: { label: string; value: string; onChange: (value: string) => void; multiline?: boolean; detail?: string | undefined; className?: string | undefined }) {
  return <label className={`form-field ${className ?? ""}`}><span>{label}</span>{multiline ? <textarea value={value} rows={4} onChange={(event) => onChange(event.target.value)} /> : <input value={value} onChange={(event) => onChange(event.target.value)} />}{detail ? <small>{detail}</small> : null}</label>;
}

function NumberField({ label, value, onChange, min, max, disabled }: { label: string; value: number; onChange: (value: number) => void; min: number; max?: number | undefined; disabled?: boolean | undefined }) {
  return <label className="form-field"><span>{label}</span><input type="number" value={value} min={min} max={max} disabled={disabled} onChange={(event) => onChange(Number(event.target.value))} /></label>;
}

function SelectField({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: Array<{ value: string; label: string }> }) {
  return <label className="form-field"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}>{options.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select></label>;
}

function JsonObjectField({ label, value, onCommit }: { label: string; value: JsonObject; onCommit: (value: JsonObject) => void }) {
  const [draft, setDraft] = useState(() => JSON.stringify(value, null, 2));
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (!dirty) setDraft(JSON.stringify(value, null, 2)); }, [dirty, value]);
  const apply = () => {
    try {
      onCommit(parseJsonObject(draft, label));
      setDirty(false);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "invalid JSON");
    }
  };
  return <label className="form-field json-object-field"><span>{label}</span><textarea rows={7} value={draft} onChange={(event) => { setDraft(event.target.value); setDirty(true); }} /><span className="json-field-actions"><small className={error ? "field-error" : undefined}>{error ?? (dirty ? "有未应用修改" : "已同步 canonical state")}</small><button type="button" className="text-link" disabled={!dirty} onClick={apply}>应用 JSON</button></span></label>;
}
