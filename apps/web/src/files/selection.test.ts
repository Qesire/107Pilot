import { describe, expect, it } from "vitest";
import type { FileEntry } from "../types";
import {
  clampToHome,
  columnDirsFor,
  computeMoveTargets,
  invertSelection,
  isArchiveName,
  joinPath,
  normalizeDir,
  parentPath,
  pathSegments,
  resolvePanePath,
  selectAllPaths,
  sortEntries,
  toggleSelection,
} from "./selection";

function entry(name: string, path: string, kind: FileEntry["kind"] = "file"): FileEntry {
  return { name, path, kind, size: 10, modified: "2024-01-01T00:00:00Z" };
}

describe("normalizeDir / joinPath / parentPath", () => {
  it("normalizes trailing slashes", () => {
    expect(normalizeDir("/a/b/")).toBe("/a/b");
    expect(normalizeDir("")).toBe("/");
    expect(normalizeDir("/")).toBe("/");
  });

  it("joins paths against root and nested dirs", () => {
    expect(joinPath("/", "x")).toBe("/x");
    expect(joinPath("/a/b/", "y")).toBe("/a/b/y");
  });

  it("computes parents", () => {
    expect(parentPath("/a/b/c")).toBe("/a/b");
    expect(parentPath("/a")).toBe("/");
  });
});

describe("pathSegments", () => {
  it("builds breadcrumb segments from root", () => {
    expect(pathSegments("/public/home/u")).toEqual([
      { label: "/", path: "/" },
      { label: "public", path: "/public" },
      { label: "home", path: "/public/home" },
      { label: "u", path: "/public/home/u" },
    ]);
  });
});

describe("columnDirsFor", () => {
  it("starts the chain at home when cwd is underneath it", () => {
    expect(columnDirsFor("/public/home/alice/data/runs", "/public/home/alice")).toEqual([
      "/public/home/alice",
      "/public/home/alice/data",
      "/public/home/alice/data/runs",
    ]);
  });

  it("returns just home when cwd equals home", () => {
    expect(columnDirsFor("/public/home/alice", "/public/home/alice")).toEqual([
      "/public/home/alice",
    ]);
  });

  it("falls back to just the cwd when cwd escapes home", () => {
    expect(columnDirsFor("/tmp/scratch", "/public/home/alice")).toEqual([
      "/tmp/scratch",
    ]);
    expect(columnDirsFor("/public/home", "/public/home/alice")).toEqual([
      "/public/home",
    ]);
  });

  it("tolerates trailing slashes", () => {
    expect(columnDirsFor("/public/home/alice/data/", "/public/home/alice/")).toEqual([
      "/public/home/alice",
      "/public/home/alice/data",
    ]);
  });
});

describe("clampToHome", () => {
  it("keeps paths at or below home", () => {
    expect(clampToHome("/public/home/alice", "/public/home/alice")).toBe("/public/home/alice");
    expect(clampToHome("/public/home/alice/data/", "/public/home/alice")).toBe(
      "/public/home/alice/data",
    );
  });

  it("snaps paths above home back to home", () => {
    expect(clampToHome("/public/home", "/public/home/alice")).toBe("/public/home/alice");
    expect(clampToHome("/public", "/public/home/alice")).toBe("/public/home/alice");
    expect(clampToHome("/", "/public/home/alice")).toBe("/public/home/alice");
  });

  it("does not treat prefix siblings as inside home", () => {
    expect(clampToHome("/public/home/alice2", "/public/home/alice")).toBe("/public/home/alice");
  });
});

describe("resolvePanePath", () => {
  it("resolves absolute and relative paths inside home", () => {
    expect(
      resolvePanePath(
        "/public/home/alice/project",
        "/public/home/alice",
        "/public/home/alice",
      ),
    ).toBe("/public/home/alice/project");
    expect(
      resolvePanePath(
        "../data",
        "/public/home/alice/project",
        "/public/home/alice",
      ),
    ).toBe("/public/home/alice/data");
  });

  it("normalizes repeated separators and dot segments lexically", () => {
    expect(
      resolvePanePath(
        "./results//today/../final/",
        "/public/home/alice/project",
        "/public/home/alice",
      ),
    ).toBe("/public/home/alice/project/results/final");
  });

  it("rejects paths escaping the owner root", () => {
    expect(() =>
      resolvePanePath(
        "../../bob",
        "/public/home/alice/project",
        "/public/home/alice",
      ),
    ).toThrow("路径超出授权目录");
  });

  it("rejects empty and NUL-containing input", () => {
    expect(() =>
      resolvePanePath("   ", "/public/home/alice", "/public/home/alice"),
    ).toThrow("请输入路径");
    expect(() =>
      resolvePanePath("data\0secret", "/public/home/alice", "/public/home/alice"),
    ).toThrow("路径包含无效字符");
  });
});

describe("toggleSelection", () => {
  it("adds then removes a path, preserving order", () => {
    const once = toggleSelection([], "/a");
    expect(once).toEqual(["/a"]);
    const twice = toggleSelection(["/a", "/b"], "/a");
    expect(twice).toEqual(["/b"]);
  });
});

describe("selectAllPaths", () => {
  it("returns every entry path", () => {
    expect(selectAllPaths([entry("a", "/a"), entry("b", "/b", "directory")])).toEqual([
      "/a",
      "/b",
    ]);
  });
});

describe("invertSelection", () => {
  it("returns the unselected entry paths in listing order", () => {
    const entries = [
      entry("a", "/a"),
      entry("b", "/b", "directory"),
      entry("c", "/c"),
    ];
    expect(invertSelection(entries, ["/b"])).toEqual(["/a", "/c"]);
  });

  it("selects everything when nothing is selected and nothing when all is", () => {
    const entries = [entry("a", "/a"), entry("b", "/b")];
    expect(invertSelection(entries, [])).toEqual(["/a", "/b"]);
    expect(invertSelection(entries, ["/a", "/b"])).toEqual([]);
  });
});

describe("isArchiveName", () => {
  it("matches tar-family, zip and rar extensions case-insensitively", () => {
    for (const name of ["a.tar", "a.tar.gz", "a.tgz", "a.tar.bz2", "a.tar.xz", "a.zip", "a.rar", "B.ZIP", "C.Rar"]) {
      expect(isArchiveName(name)).toBe(true);
    }
  });

  it("rejects non-archive names", () => {
    for (const name of ["a.txt", "a.zipx", "a.rars", "zip", "archive"]) {
      expect(isArchiveName(name)).toBe(false);
    }
  });
});

describe("computeMoveTargets", () => {
  it("maps entries into the destination directory", () => {
    const targets = computeMoveTargets(
      [entry("a.txt", "/src/a.txt"), entry("d", "/src/d", "directory")],
      "/dest",
    );
    expect(targets).toEqual([
      { from: "/src/a.txt", to: "/dest/a.txt", name: "a.txt" },
      { from: "/src/d", to: "/dest/d", name: "d" },
    ]);
  });

  it("skips entries already in the destination", () => {
    const targets = computeMoveTargets([entry("a.txt", "/dest/a.txt")], "/dest/");
    expect(targets).toEqual([]);
  });
});

describe("sortEntries", () => {
  it("puts directories first, then alphabetical", () => {
    const sorted = sortEntries([
      entry("z.txt", "/z.txt"),
      entry("m", "/m", "directory"),
      entry("a.txt", "/a.txt"),
      entry("b", "/b", "directory"),
    ]);
    expect(sorted.map((e) => e.name)).toEqual(["b", "m", "a.txt", "z.txt"]);
  });
});
