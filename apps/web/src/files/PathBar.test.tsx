import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { PathBar } from "./PathBar";

describe("PathBar", () => {
  it("renders only authorized breadcrumb segments and an edit control", () => {
    const markup = renderToStaticMarkup(
      <PathBar
        cwd="/public/home/alice/project"
        home="/public/home/alice"
        isPending={false}
        isError={false}
        onNavigate={vi.fn()}
      />,
    );

    expect(markup).toContain('aria-label="路径"');
    expect(markup).toContain('aria-label="手动输入路径"');
    expect(markup).toContain("alice");
    expect(markup).toContain("project");
    expect(markup).not.toContain(">public<");
  });
});
