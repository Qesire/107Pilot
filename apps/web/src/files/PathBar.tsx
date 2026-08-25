import { useEffect, useId, useState } from "react";
import { ChevronRight, PencilLine } from "lucide-react";
import { clampToHome, pathSegments, resolvePanePath } from "./selection";

export interface PathBarProps {
  cwd: string;
  home: string;
  isPending: boolean;
  isError: boolean;
  onNavigate: (path: string) => void;
}

export function PathBar({
  cwd,
  home,
  isPending,
  isError,
  onNavigate,
}: PathBarProps) {
  const inputId = useId();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(cwd);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [submittedPath, setSubmittedPath] = useState<string | null>(null);

  useEffect(() => {
    if (!editing) {
      setDraft(cwd);
      return;
    }
    if (submittedPath !== cwd || isPending || isError) return;
    setEditing(false);
    setSubmittedPath(null);
  }, [cwd, editing, isError, isPending, submittedPath]);

  const beginEditing = () => {
    setDraft(cwd);
    setValidationError(null);
    setSubmittedPath(null);
    setEditing(true);
  };

  const cancelEditing = () => {
    setDraft(cwd);
    setValidationError(null);
    setSubmittedPath(null);
    setEditing(false);
  };

  if (editing) {
    const listingError = submittedPath !== null && isError
      ? "无法打开该路径，请检查后重试"
      : null;
    return (
      <form
        className="path-bar-form"
        onSubmit={(event) => {
          event.preventDefault();
          try {
            const target = resolvePanePath(draft, cwd, home);
            setValidationError(null);
            setSubmittedPath(target);
            onNavigate(target);
          } catch (error) {
            setSubmittedPath(null);
            setValidationError(error instanceof Error ? error.message : "路径无效");
          }
        }}
      >
        <label className="sr-only" htmlFor={inputId}>路径</label>
        <input
          id={inputId}
          className="path-bar-input"
          value={draft}
          autoFocus
          onChange={(event) => {
            setDraft(event.target.value);
            setValidationError(null);
            setSubmittedPath(null);
          }}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            event.preventDefault();
            event.stopPropagation();
            cancelEditing();
          }}
        />
        {isPending && submittedPath !== null ? (
          <span className="path-bar-feedback" role="status">正在打开…</span>
        ) : null}
        {validationError || listingError ? (
          <span className="path-bar-feedback error" role="alert">
            {validationError ?? listingError}
          </span>
        ) : null}
      </form>
    );
  }

  const segments = pathSegments(cwd).filter(
    (segment) => clampToHome(segment.path, home) === segment.path,
  );
  return (
    <nav className="filepane-breadcrumb" aria-label="路径" onDoubleClick={beginEditing}>
      {segments.map((segment, index) => (
        <span key={segment.path} className="crumb">
          {index > 0 ? <ChevronRight size={11} aria-hidden="true" /> : null}
          <button type="button" onClick={() => onNavigate(segment.path)}>
            {segment.label}
          </button>
        </span>
      ))}
      <button
        type="button"
        className="path-bar-edit-trigger"
        aria-label="手动输入路径"
        title="手动输入路径"
        onClick={beginEditing}
      >
        <PencilLine size={12} aria-hidden="true" />
      </button>
    </nav>
  );
}
