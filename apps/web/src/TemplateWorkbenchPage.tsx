import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ClipboardCheck, FilePlus2, Send, Upload } from "lucide-react";
import { api } from "./api";
import { createDefaultContract, parseJsonObject } from "./contract-state";
import { QueryBoundary, SectionHeading, StatusBadge, formatTimestamp } from "./components";
import { useTemplateDraft, useTemplateDrafts, useTemplateReviews } from "./query";
import type { MarketVisibility, TemplateDraft, TemplateGateValidation, TemplateReviewQueueItem } from "./types";
import type { LocationState } from "./url";

interface WorkbenchProps {
  user: string;
  location: LocationState;
  navigate: (path: string, options?: { replace?: boolean }) => void;
}

const draftStateTone: Record<string, "neutral" | "info" | "success" | "warning" | "danger"> = {
  editable: "neutral",
  submitted: "info",
  approved: "success",
  rejected: "danger",
  published: "success",
};

/** localStorage key so an owner can publish after an approval lands in a later session. */
function reviewStorageKey(draftId: string): string {
  return `pilot107:template-review:${draftId}`;
}

function rememberReviewId(draftId: string, reviewId: string): void {
  try {
    window.localStorage.setItem(reviewStorageKey(draftId), reviewId);
  } catch {
    // Storage unavailable (e.g. privacy mode) — the user can still paste the id.
  }
}

function rememberedReviewId(draftId: string): string {
  try {
    return window.localStorage.getItem(reviewStorageKey(draftId)) ?? "";
  } catch {
    return "";
  }
}

export function TemplateWorkbenchPage({ user, location, navigate }: WorkbenchProps) {
  const path = location.pathname;
  if (path === "/templates/new") {
    return <DraftEditor user={user} location={location} navigate={navigate} draftId={null} />;
  }
  if (path.startsWith("/templates/draft/")) {
    const draftId = decodeURIComponent(path.slice("/templates/draft/".length));
    return <DraftEditor user={user} location={location} navigate={navigate} draftId={draftId} />;
  }
  if (path === "/templates/reviews") {
    return <ReviewQueueView user={user} location={location} navigate={navigate} />;
  }
  return <MyDraftsView user={user} location={location} navigate={navigate} />;
}

// ---------------------------------------------------------------------------
// View 1: my drafts
// ---------------------------------------------------------------------------

function MyDraftsView({ user, navigate }: WorkbenchProps) {
  const drafts = useTemplateDrafts(user);
  return (
    <>
      <SectionHeading
        eyebrow="Template authoring / drafts"
        title="我的模板草稿"
        detail="草稿经服务端发布门禁校验、审核通过后才能发布为不可变 release。"
      />
      <div className="agent-action-row">
        <button className="button primary" type="button" onClick={() => navigate(`/templates/new?user=${encodeURIComponent(user)}`)}>
          <FilePlus2 aria-hidden="true" size={15} /> 新建模板草稿
        </button>
        <button className="button secondary" type="button" onClick={() => navigate(`/templates/reviews?user=${encodeURIComponent(user)}`)}>
          审核队列
        </button>
      </div>
      <QueryBoundary
        pending={drafts.isPending}
        error={drafts.error}
        empty={(drafts.data?.items.length ?? 0) === 0}
        emptyTitle="还没有模板草稿"
        emptyDetail="从“新建模板草稿”开始；发布后会出现在统一市场的 curated template 分类。"
      >
        <section className="market-grid" aria-label="我的模板草稿">
          {(drafts.data?.items ?? []).map((draft) => (
            <DraftCard key={draft.draft_id} user={user} draft={draft} navigate={navigate} />
          ))}
        </section>
      </QueryBoundary>
    </>
  );
}

function DraftCard({ user, draft, navigate }: {
  user: string;
  draft: TemplateDraft;
  navigate: WorkbenchProps["navigate"];
}) {
  const queryClient = useQueryClient();
  const [validation, setValidation] = useState<TemplateGateValidation | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [releaseVersion, setReleaseVersion] = useState("1.0.0");
  const [reviewId, setReviewId] = useState("");
  const invalidateDrafts = () => {
    void queryClient.invalidateQueries({ queryKey: ["template-drafts", user] });
    void queryClient.invalidateQueries({ queryKey: ["template-draft", user, draft.draft_id] });
  };
  const validate = useMutation({
    mutationFn: () => api.validateTemplateDraft(user, draft.draft_id),
    onSuccess: (result) => setValidation(result),
  });
  const submitReview = useMutation({
    mutationFn: () => api.submitTemplateDraftForReview(user, draft.draft_id, draft.version),
    onSuccess: (review) => {
      rememberReviewId(draft.draft_id, review.review_id);
      invalidateDrafts();
    },
  });
  const publish = useMutation({
    mutationFn: () => api.publishTemplateDraft(user, draft.draft_id, {
      reviewId: reviewId.trim(),
      releaseVersion: releaseVersion.trim(),
      requestKey: `web-publish-template-${crypto.randomUUID()}`,
    }),
    onSuccess: () => {
      invalidateDrafts();
      void queryClient.invalidateQueries({ queryKey: ["market-items"] });
      void queryClient.invalidateQueries({ queryKey: ["templates"] });
      setPublishOpen(false);
    },
  });
  const error = validate.error ?? submitReview.error ?? publish.error;
  const editable = draft.state === "editable" || draft.state === "rejected";
  return (
    <article className="template-card">
      <header>
        <div><p className="panel-kicker">{draft.draft_id}</p><h2>{draft.title}</h2></div>
        <StatusBadge label={draft.state} tone={draftStateTone[draft.state] ?? "neutral"} />
      </header>
      <p className="template-description">{draft.description || "没有填写说明。"}</p>
      <div className="template-meta">
        <span>{draft.visibility}{draft.scope_key ? ` · ${draft.scope_key}` : ""}</span>
        <span>v{draft.version}</span>
        <span>{formatTimestamp(draft.updated_at)}</span>
      </div>
      <div className="agent-action-row">
        {editable ? <button className="button secondary" type="button" onClick={() => navigate(`/templates/draft/${encodeURIComponent(draft.draft_id)}?user=${encodeURIComponent(user)}`)}>编辑</button> : null}
        {editable ? <button className="button secondary" type="button" disabled={validate.isPending} onClick={() => validate.mutate()}><ClipboardCheck aria-hidden="true" size={15} />{validate.isPending ? "校验中" : "服务端校验"}</button> : null}
        {editable ? <button className="button primary" type="button" disabled={submitReview.isPending} onClick={() => submitReview.mutate()}><Send aria-hidden="true" size={15} />{submitReview.isPending ? "提交中" : "提交审核"}</button> : null}
        {draft.state === "submitted" ? <p className="side-detail">等待审核；审核通过后可发布。</p> : null}
        {draft.state === "approved" && !publishOpen ? (
          <button className="button primary" type="button" onClick={() => { setReviewId(rememberedReviewId(draft.draft_id)); setPublishOpen(true); }}>
            <Upload aria-hidden="true" size={15} /> 发布为 release
          </button>
        ) : null}
        {draft.state === "published" ? (
          <button className="button secondary" type="button" onClick={() => navigate(`/templates/${encodeURIComponent(draft.template_id)}?user=${encodeURIComponent(user)}`)}>
            查看已发布 release <ArrowRight aria-hidden="true" size={15} />
          </button>
        ) : null}
      </div>
      {publishOpen ? (
        <div className="form-grid two">
          <label className="form-field"><span className="form-field-label">Release 版本</span><input value={releaseVersion} onChange={(event) => setReleaseVersion(event.target.value)} /></label>
          <label className="form-field"><span className="form-field-label">审核记录 ID（提交审核时返回）</span><input value={reviewId} onChange={(event) => setReviewId(event.target.value)} /></label>
          <button className="button primary" type="button" disabled={!reviewId.trim() || !releaseVersion.trim() || publish.isPending} onClick={() => publish.mutate()}>{publish.isPending ? "发布中" : "确认发布"}</button>
        </div>
      ) : null}
      {validation ? (
        <div className="diagnosis-state">
          <StatusBadge label={`gate ${validation.status}`} tone={validation.status === "approved" ? "success" : "warning"} />
          <span>{validation.findings.length} findings</span>
        </div>
      ) : null}
      {validation?.findings.length ? <ul className="capsule-messages warning">{validation.findings.map((finding, index) => <li key={`${finding.code}-${index}`}>{finding.severity} · {finding.code}: {finding.message}</li>)}</ul> : null}
      {error ? <p className="limitation" role="alert">{error.message}</p> : null}
    </article>
  );
}

// ---------------------------------------------------------------------------
// View 2: create / edit draft
// ---------------------------------------------------------------------------

const defaultCompatibility = JSON.stringify({ partitions: ["Students"], gpu: false }, null, 2);
const defaultPublication = JSON.stringify(
  { license: "MIT", attribution: "", dataset_access: "", risk_statement: "" },
  null,
  2,
);

function DraftEditor({ user, navigate, draftId }: WorkbenchProps & { draftId: string | null }) {
  const queryClient = useQueryClient();
  const existing = useTemplateDraft(user, draftId);
  const [hydratedId, setHydratedId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<MarketVisibility>("private");
  const [scopeKey, setScopeKey] = useState("");
  const [payloadText, setPayloadText] = useState(() => JSON.stringify(createDefaultContract(), null, 2));
  const [compatibilityText, setCompatibilityText] = useState(defaultCompatibility);
  const [publicationText, setPublicationText] = useState(defaultPublication);

  useEffect(() => {
    if (!draftId) {
      if (hydratedId !== null) setHydratedId(null);
      return;
    }
    if (existing.data && hydratedId !== draftId) {
      setTitle(existing.data.title);
      setDescription(existing.data.description);
      setVisibility(existing.data.visibility);
      setScopeKey(existing.data.scope_key ?? "");
      setPayloadText(JSON.stringify(existing.data.payload, null, 2));
      setCompatibilityText(JSON.stringify(existing.data.compatibility, null, 2));
      setPublicationText(JSON.stringify(existing.data.publication, null, 2));
      setHydratedId(draftId);
    }
  }, [draftId, existing.data, hydratedId]);

  const readOnly = Boolean(existing.data && (existing.data.state === "submitted" || existing.data.state === "published"));
  const parseError = useMemo(() => {
    try {
      parseJsonObject(payloadText, "payload");
      parseJsonObject(compatibilityText, "compatibility");
      parseJsonObject(publicationText, "publication");
      return null;
    } catch (caught) {
      return caught instanceof Error ? caught.message : "invalid JSON";
    }
  }, [payloadText, compatibilityText, publicationText]);

  const save = useMutation({
    mutationFn: () => {
      const body = {
        title: title.trim(),
        description: description.trim(),
        visibility,
        scope_key: visibility === "course" ? scopeKey.trim() || null : null,
        payload: parseJsonObject(payloadText, "payload"),
        compatibility: parseJsonObject(compatibilityText, "compatibility"),
        publication: parseJsonObject(publicationText, "publication"),
      };
      return draftId && existing.data
        ? api.updateTemplateDraft(user, draftId, { expected_version: existing.data.version, ...body })
        : api.createTemplateDraft(user, body);
    },
    onSuccess: (record) => {
      void queryClient.invalidateQueries({ queryKey: ["template-drafts", user] });
      if (!draftId) {
        navigate(`/templates/draft/${encodeURIComponent(record.draft_id)}?user=${encodeURIComponent(user)}`, { replace: true });
      } else {
        void queryClient.invalidateQueries({ queryKey: ["template-draft", user, draftId] });
        setHydratedId(null);
      }
    },
  });

  const hydrated = !draftId || hydratedId === draftId;
  return (
    <>
      <SectionHeading
        eyebrow="Template authoring / draft editor"
        title={draftId ? "编辑模板草稿" : "新建模板草稿"}
        detail="payload 是一份 canonical Contract；提交审核前请先服务端校验。submitted/published 状态只读。"
      />
      {draftId ? (
        <QueryBoundary pending={existing.isPending} error={existing.error}>
          {hydrated ? (
            <div className="studio-form-scroll template-draft-editor">
              {existing.data ? <div className="diagnosis-state"><StatusBadge label={existing.data.state} tone={draftStateTone[existing.data.state] ?? "neutral"} /><span>v{existing.data.version}</span>{readOnly ? <span>当前状态只读</span> : null}</div> : null}
              <div className="form-grid two">
                <label className="form-field"><span className="form-field-label">标题</span><input value={title} disabled={readOnly} onChange={(event) => setTitle(event.target.value)} /></label>
                <label className="form-field"><span className="form-field-label">可见性</span>
                  <select value={visibility} disabled={readOnly} onChange={(event) => setVisibility(event.target.value as MarketVisibility)}>
                    <option value="private">Private</option>
                    <option value="course">Course</option>
                    <option value="campus">Campus</option>
                    <option value="public">Public</option>
                  </select>
                </label>
                {visibility === "course" ? <label className="form-field"><span className="form-field-label">课程 scope</span><input value={scopeKey} disabled={readOnly} placeholder="course-107" onChange={(event) => setScopeKey(event.target.value)} /></label> : null}
              </div>
              <label className="form-field"><span className="form-field-label">说明</span><textarea value={description} disabled={readOnly} onChange={(event) => setDescription(event.target.value)} /></label>
              <label className="form-field"><span className="form-field-label">Contract payload（JSON）</span><textarea rows={12} className="mono" value={payloadText} disabled={readOnly} onChange={(event) => setPayloadText(event.target.value)} /></label>
              <div className="form-grid two">
                <label className="form-field"><span className="form-field-label">Compatibility（JSON）</span><textarea rows={6} className="mono" value={compatibilityText} disabled={readOnly} onChange={(event) => setCompatibilityText(event.target.value)} /></label>
                <label className="form-field"><span className="form-field-label">Publication（JSON）</span><textarea rows={6} className="mono" value={publicationText} disabled={readOnly} onChange={(event) => setPublicationText(event.target.value)} /></label>
              </div>
              {parseError ? <p className="limitation" role="alert">{parseError}</p> : null}
              {save.isError ? <p className="limitation" role="alert">{save.error.message}</p> : null}
              {save.isSuccess ? <p>已保存（v{save.data.version}）。</p> : null}
              {!readOnly ? (
                <div className="agent-action-row">
                  <button className="button primary" type="button" disabled={!title.trim() || Boolean(parseError) || save.isPending} onClick={() => save.mutate()}>
                    {save.isPending ? "保存中" : draftId ? "保存修改" : "创建草稿"}
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}
        </QueryBoundary>
      ) : (
        <div className="studio-form-scroll template-draft-editor">
          <div className="form-grid two">
            <label className="form-field"><span className="form-field-label">标题</span><input value={title} onChange={(event) => setTitle(event.target.value)} /></label>
            <label className="form-field"><span className="form-field-label">可见性</span>
              <select value={visibility} onChange={(event) => setVisibility(event.target.value as MarketVisibility)}>
                <option value="private">Private</option>
                <option value="course">Course</option>
                <option value="campus">Campus</option>
                <option value="public">Public</option>
              </select>
            </label>
            {visibility === "course" ? <label className="form-field"><span className="form-field-label">课程 scope</span><input value={scopeKey} placeholder="course-107" onChange={(event) => setScopeKey(event.target.value)} /></label> : null}
          </div>
          <label className="form-field"><span className="form-field-label">说明</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} /></label>
          <label className="form-field"><span className="form-field-label">Contract payload（JSON）</span><textarea rows={12} className="mono" value={payloadText} onChange={(event) => setPayloadText(event.target.value)} /></label>
          <div className="form-grid two">
            <label className="form-field"><span className="form-field-label">Compatibility（JSON）</span><textarea rows={6} className="mono" value={compatibilityText} onChange={(event) => setCompatibilityText(event.target.value)} /></label>
            <label className="form-field"><span className="form-field-label">Publication（JSON）</span><textarea rows={6} className="mono" value={publicationText} onChange={(event) => setPublicationText(event.target.value)} /></label>
          </div>
          {parseError ? <p className="limitation" role="alert">{parseError}</p> : null}
          {save.isError ? <p className="limitation" role="alert">{save.error.message}</p> : null}
          <div className="agent-action-row">
            <button className="button primary" type="button" disabled={!title.trim() || Boolean(parseError) || save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? "创建中" : "创建草稿"}
            </button>
          </div>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// View 3: review queue (reviewer role only)
// ---------------------------------------------------------------------------

function ReviewQueueView({ user }: WorkbenchProps) {
  const reviews = useTemplateReviews(user);
  return (
    <>
      <SectionHeading
        eyebrow="Template authoring / review queue"
        title="模板审核队列"
        detail="只有配置了 reviewer 角色的账号能看到待审条目；决定会记录 reviewer 与备注。"
      />
      <QueryBoundary
        pending={reviews.isPending}
        error={reviews.error}
        empty={(reviews.data?.items.length ?? 0) === 0}
        emptyTitle="没有待审模板"
        emptyDetail="当前账号可见的审核队列为空；可能没有待审草稿，或当前账号不是 reviewer。"
      >
        <section className="market-grid" aria-label="模板审核队列">
          {(reviews.data?.items ?? []).map((review) => (
            <ReviewCard key={review.review_id} user={user} review={review} />
          ))}
        </section>
      </QueryBoundary>
    </>
  );
}

function ReviewCard({ user, review }: { user: string; review: TemplateReviewQueueItem }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: (approve: boolean) => api.decideTemplateReview(user, review.review_id, {
      expectedVersion: review.version,
      approve,
      note: note.trim() || undefined,
    }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["template-reviews", user] });
      void queryClient.invalidateQueries({ queryKey: ["template-drafts"] });
    },
  });
  const decided = review.state !== "pending";
  return (
    <article className="template-card">
      <header>
        <div><p className="panel-kicker">{review.review_id}</p><h2>{review.draft_title}</h2></div>
        <StatusBadge label={review.state} tone={review.state === "approved" ? "success" : review.state === "rejected" ? "danger" : "info"} />
      </header>
      <div className="template-meta">
        <span>申请人 {review.requester}</span>
        <span>{review.visibility}{review.scope_key ? ` · ${review.scope_key}` : ""}</span>
        <span>{formatTimestamp(review.created_at)}</span>
      </div>
      {decided ? <p className="side-detail">{review.note ? `审核备注：${review.note}` : "已做出决定。"}</p> : (
        <>
          <label className="form-field"><span className="form-field-label">审核备注</span><textarea value={note} onChange={(event) => setNote(event.target.value)} /></label>
          <div className="agent-action-row">
            <button className="button primary" type="button" disabled={decide.isPending} onClick={() => decide.mutate(true)}>批准</button>
            <button className="button danger" type="button" disabled={decide.isPending} onClick={() => decide.mutate(false)}>拒绝</button>
          </div>
        </>
      )}
      {decide.isError ? <p className="limitation" role="alert">{decide.error.message}</p> : null}
    </article>
  );
}
