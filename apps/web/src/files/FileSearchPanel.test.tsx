import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { FileSearchEntry } from "../types";
import {
  FileSearchPanel,
  buildFileSearchRequest,
  fileSearchOpenTarget,
  mergeFileSearchItems,
  type FileSearchFilters,
} from "./FileSearchPanel";

const filters: FileSearchFilters = {
  kind: "all",
  sizeMin: "",
  sizeMax: "",
  modifiedFrom: "",
  modifiedTo: "",
};

const entry = (path: string): FileSearchEntry => ({
  path,
  relative_path: path.split("/").pop() ?? path,
  type: "file",
  size: 10,
  mtime: 1_700_000_000,
});

describe("FileSearchPanel", () => {
  it("does not create a request until the query has non-whitespace text", () => {
    expect(
      buildFileSearchRequest("/public/home/alice", "   ", filters, null),
    ).toBeNull();
  });

  it("continues an incomplete result page with its opaque cursor", () => {
    expect(
      buildFileSearchRequest(
        "/public/home/alice",
        "model",
        filters,
        "cursor-1/opaque==",
      ),
    ).toEqual({
      root: "/public/home/alice",
      q: "model",
      kind: "all",
      limit: 100,
      cursor: "cursor-1/opaque==",
    });
  });

  it("appends only continuation pages", () => {
    const first = entry("/public/home/alice/first.txt");
    const second = entry("/public/home/alice/second.txt");

    expect(mergeFileSearchItems([first], [second], null)).toEqual([second]);
    expect(mergeFileSearchItems([first], [second], "cursor-1")).toEqual([
      first,
      second,
    ]);
  });

  it("opens directories directly and selects files after opening their parent", () => {
    expect(fileSearchOpenTarget({
      ...entry("/public/home/alice/demo-search"),
      relative_path: "demo-search",
      type: "directory",
    })).toEqual({ path: "/public/home/alice/demo-search", selectedPath: undefined });
    expect(fileSearchOpenTarget(entry("/public/home/alice/demo-search/result.txt"))).toEqual({
      path: "/public/home/alice/demo-search",
      selectedPath: "/public/home/alice/demo-search/result.txt",
    });
  });

  it("renders query and filter controls without issuing an SSR request", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <FileSearchPanel
          user="alice"
          root="/public/home/alice"
          onOpen={vi.fn()}
        />
      </QueryClientProvider>,
    );

    expect(markup).toContain('aria-label="搜索文件名或路径"');
    expect(markup).toContain('aria-label="文件类型"');
  });
});
