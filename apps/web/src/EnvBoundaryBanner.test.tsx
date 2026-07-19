import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { EnvBoundaryBanner } from "./EnvBoundaryBanner";

describe("EnvBoundaryBanner", () => {
  it("renders the Docker Slurm boundary notice text", () => {
    const markup = renderToStaticMarkup(<EnvBoundaryBanner />);

    expect(markup).toContain("当前为 Docker Slurm 模拟环境，非真实 107 平台。");
  });

  it("announces the boundary via role=status and polite live region", () => {
    const markup = renderToStaticMarkup(<EnvBoundaryBanner />);

    expect(markup).toContain('role="status"');
    expect(markup).toContain('aria-live="polite"');
  });
});
