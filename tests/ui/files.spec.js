const { expect, test } = require("@playwright/test");

// In-memory filesystem template. Each test gets a fresh deep copy so rename
// (move) mutations stay isolated.
const HOME = "/public/home/alice";
function buildFs() {
  return {
    [HOME]: [
      { name: "docs", type: "dir", size: 0, mtime: 1700000000 },
      { name: "data", type: "dir", size: 0, mtime: 1700000000 },
      { name: "large", type: "dir", size: 0, mtime: 1700000000 },
      { name: "readme.md", type: "file", size: 2048, mtime: 1700000100 },
      { name: "run.py", type: "file", size: 512, mtime: 1700000200 },
    ],
    [`${HOME}/docs`]: [
      { name: "note.txt", type: "file", size: 128, mtime: 1700000300 },
    ],
    [`${HOME}/data`]: [
      { name: "sample.csv", type: "file", size: 4096, mtime: 1700000400 },
    ],
    [`${HOME}/demo-search`]: [
      { name: "nested", type: "dir", size: 0, mtime: 1700000500 },
    ],
    [`${HOME}/demo-search/nested`]: [
      { name: "result-model.txt", type: "file", size: 8192, mtime: 1700000600 },
    ],
    [`${HOME}/large`]: Array.from({ length: 2000 }, (_, index) => ({
      name: `sample-${String(index).padStart(4, "0")}.dat`,
      type: "file",
      size: 1024 + index,
      mtime: 1700010000 + index,
    })),
  };
}

function dirOf(path) {
  const idx = path.lastIndexOf("/");
  return idx <= 0 ? "/" : path.slice(0, idx);
}

async function installFilesMock(page) {
  const fs = buildFs();
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

    if (url.pathname === "/healthz") return json({ status: "ok" });
    if (url.pathname === "/api/v1/health/ready") {
      return json({ status: "ready", checks: { database: { status: "ok" } } });
    }
    if (url.pathname === "/api/v1/web/session") {
      const user = request.headers()["x-pilot107-user"] || "alice";
      return json({ identity_mode: "demo", user, switchable: true });
    }
    if (url.pathname === "/api/v1/files" && request.method() === "GET") {
      const path = url.searchParams.get("path") || HOME;
      const limit = Number(url.searchParams.get("limit") || 500);
      const offset = Number(url.searchParams.get("cursor") || 0);
      const allEntries = fs[path] ?? [];
      const entries = allEntries.slice(offset, offset + limit);
      const nextOffset = offset + entries.length;
      const hasMore = nextOffset < allEntries.length;
      return json({
        path,
        entries,
        page: { limit, has_more: hasMore, next_cursor: hasMore ? String(nextOffset) : null },
        directory_revision: "ui-fixture-v1",
      });
    }
    if (url.pathname === "/api/v1/files/search" && request.method() === "GET") {
      const root = url.searchParams.get("root") || HOME;
      const query = (url.searchParams.get("q") || "").toLowerCase();
      const kind = url.searchParams.get("kind") || "all";
      const items = Object.entries(fs).flatMap(([dir, entries]) => {
        if (dir !== root && !dir.startsWith(`${root}/`)) return [];
        return entries.flatMap((item) => {
          const path = `${dir}/${item.name}`;
          const type = item.type === "dir" ? "directory" : "file";
          const relativePath = path.slice(root.length).replace(/^\/+/, "");
          if (!relativePath.toLowerCase().includes(query)) return [];
          if (kind !== "all" && kind !== type) return [];
          return [{
            path,
            relative_path: relativePath,
            type,
            size: item.size,
            mtime: item.mtime,
          }];
        });
      });
      return json({ root, items, incomplete: false, next_cursor: null, warnings: [] });
    }
    if (url.pathname === "/api/v1/files/usage") {
      return json({
        home: HOME,
        used_bytes: 3 * 1024 * 1024 * 1024,
        total_bytes: 100 * 1024 * 1024 * 1024,
        observed_at: "2026-07-16T02:08:00Z",
      });
    }
    if (url.pathname === "/api/v1/files/rename" && request.method() === "POST") {
      const body = request.postDataJSON();
      const fromDir = dirOf(body.path);
      const toDir = dirOf(body.new_path);
      const name = body.new_path.split("/").pop();
      const sourceList = fs[fromDir];
      if (sourceList) {
        const idx = sourceList.findIndex((e) => e.name === body.path.split("/").pop());
        if (idx >= 0) {
          const [moved] = sourceList.splice(idx, 1);
          moved.name = name;
          (fs[toDir] ??= []).push(moved);
        }
      }
      return json({ status: "ok", path: body.path, new_path: body.new_path });
    }
    if (url.pathname === "/api/v1/files/mkdir" && request.method() === "POST") {
      return json({ status: "ok", path: request.postDataJSON().path });
    }
    if (url.pathname === "/api/v1/files/delete" && request.method() === "POST") {
      return json({ status: "ok", path: request.postDataJSON().path });
    }
    if (url.pathname === "/api/v1/files/archive" && request.method() === "POST") {
      return json({ status: "ok", archive_path: `${HOME}/archive.tar.gz`, member_count: 1 });
    }
    // Anything else (HTML page, static assets, unused API) passes through to
    // the web server so the app shell loads normally.
    return route.continue();
  });
}

test.beforeEach(async ({ page }) => {
  await installFilesMock(page);
});

test("defaults to one list pane rooted at home", async ({ page }) => {
  await page.goto("/files?user=alice");

  const pane = page.locator(".file-pane").first();
  await expect(page.locator(".file-pane")).toHaveCount(1);
  await expect(pane).toHaveAttribute("data-pane-cwd", HOME);
  await expect(pane.locator(".filepane-table")).toBeVisible();
  await expect(pane.locator(".file-row", { hasText: "readme.md" })).toBeVisible();
  await expect(pane.locator(".file-row", { hasText: "docs" })).toBeVisible();
});

test("manual path entry and search open a file in the active pane", async ({ page }) => {
  await page.goto("/files?user=alice");
  const pane = page.locator(".file-pane").first();
  await pane.getByTitle("手动输入路径").click();
  await pane.getByLabel("路径").fill(`${HOME}/demo-search`);
  await pane.getByLabel("路径").press("Enter");
  await expect(pane).toHaveAttribute("data-pane-cwd", `${HOME}/demo-search`);

  const search = page.getByLabel("搜索文件名或路径");
  await search.fill("model");
  await expect(page.locator(".file-search-root")).toHaveText(`${HOME}/demo-search`);
  await page.getByRole("button", { name: /nested\/result-model\.txt/ }).click();

  await expect(pane).toHaveAttribute("data-pane-cwd", `${HOME}/demo-search/nested`);
  await expect(pane.locator('[data-path$="/result-model.txt"]')).toHaveClass(/selected/);
});

test("panes navigate independently", async ({ page }) => {
  await page.goto("/files?user=alice");
  await page.locator(".file-pane").getByTitle("横向拆分").click();

  const panes = page.locator(".file-pane");
  await panes.nth(0).getByTitle("网格视图").click();
  await panes.nth(1).getByTitle("网格视图").click();
  // Open "docs" in the first pane only.
  await panes.nth(0).locator(".file-tile", { hasText: "docs" }).dblclick();

  // First pane now shows the docs contents; second pane is still at home.
  await expect(panes.nth(0).locator(".file-tile", { hasText: "note.txt" })).toBeVisible();
  await expect(panes.nth(1).locator(".file-tile", { hasText: "readme.md" })).toBeVisible();
  await expect(panes.nth(1).locator(".file-tile", { hasText: "note.txt" })).toHaveCount(0);
});

test("splitting adds a pane and the layout survives a reload", async ({ page }) => {
  await page.goto("/files?user=alice");
  await expect(page.locator(".file-pane")).toHaveCount(1);

  // Split the first pane horizontally.
  await page.locator(".file-pane").nth(0).getByTitle("横向拆分").click();
  await expect(page.locator(".file-pane")).toHaveCount(2);

  await page.reload();
  await expect(page.locator(".file-pane")).toHaveCount(2);
});

test("closing a pane removes it but keeps at least one", async ({ page }) => {
  await page.goto("/files?user=alice");
  await page.locator(".file-pane").getByTitle("横向拆分").click();
  await page.locator(".file-pane").nth(1).getByTitle("关闭窗格").click();
  await expect(page.locator(".file-pane")).toHaveCount(1);

  // The last pane cannot be closed.
  await page.locator(".file-pane").nth(0).getByTitle("关闭窗格").click();
  await expect(page.locator(".file-pane")).toHaveCount(1);
});

test("grid view toggles to a list view per pane", async ({ page }) => {
  await page.goto("/files?user=alice");
  const firstPane = page.locator(".file-pane").nth(0);

  await firstPane.getByTitle("网格视图").click();
  await expect(firstPane.locator(".filegrid")).toBeVisible();
  await firstPane.getByTitle("列表视图").click();
  await expect(firstPane.locator(".filepane-table")).toBeVisible();
  await expect(firstPane.locator(".filegrid")).toHaveCount(0);
});

test("ctrl-click builds a multi-selection with an action bar", async ({ page }) => {
  await page.goto("/files?user=alice");
  const firstPane = page.locator(".file-pane").nth(0);
  await firstPane.getByTitle("网格视图").click();

  await firstPane.locator(".file-tile", { hasText: "readme.md" }).click();
  await firstPane.locator(".file-tile", { hasText: "run.py" }).click({ modifiers: ["Control"] });

  await expect(firstPane.locator(".filepane-selectionbar")).toContainText("2 项已选");
});

test("marquee drag over empty grid area selects the tiles it covers", async ({ page }) => {
  await page.goto("/files?user=alice");
  const firstPane = page.locator(".file-pane").nth(0);
  await firstPane.getByTitle("网格视图").click();

  // `.filegrid` deliberately keeps an empty bottom strip for marquee starts.
  // Scroll that strip into the pane viewport before deriving pointer geometry;
  // this avoids coupling the gesture to the surrounding page height.
  await firstPane.locator(".filepane-body").evaluate((el) => { el.scrollTop = el.scrollHeight; });
  // Keep the file viewport itself inside the browser viewport before using
  // page-level mouse coordinates. Virtualization changes document geometry,
  // so inner scrolling alone is not sufficient for a reliable gesture.
  await firstPane.locator(".filepane-body").scrollIntoViewIfNeeded();
  await firstPane.locator(".filepane-body").evaluate((el) => { el.scrollTop = el.scrollHeight; });

  const box = await firstPane.evaluate((el) => {
    const rects = [...el.querySelectorAll(".file-tile")].map((t) => t.getBoundingClientRect());
    const grid = el.querySelector(".filegrid").getBoundingClientRect();
    return {
      sx: Math.round(rects[0].left + 4),
      sy: Math.round(grid.bottom - 5),
      tx: Math.round(rects[1].left + rects[1].width / 2),
      ty: Math.round(rects[0].top + 4),
    };
  });

  await page.mouse.move(box.sx, box.sy);
  await page.mouse.down();
  await page.mouse.move((box.sx + box.tx) / 2, (box.sy + box.ty) / 2, { steps: 6 });
  await page.mouse.move(box.tx, box.ty, { steps: 6 });
  await page.mouse.up();

  await expect(firstPane.locator(".filepane-selectionbar")).toContainText("2 项已选");
});

test("dragging a file onto another pane's directory moves it", async ({ page }) => {
  await page.goto("/files?user=alice");
  await page.locator(".file-pane").getByTitle("横向拆分").click();
  await page.locator(".file-pane").nth(0).getByTitle("网格视图").click();
  await page.locator(".file-pane").nth(1).getByTitle("网格视图").click();
  // Wait for the actual drag source and target in their respective panes.
  // The fixture may gain unrelated home entries as file-workspace coverage grows.
  const panes = page.locator(".file-pane");
  await expect(panes.nth(0).locator(".file-tile", { hasText: "readme.md" })).toBeVisible();
  await expect(panes.nth(1).locator(".file-tile", { hasText: "data" })).toBeVisible();

  const source = panes.nth(0).locator(".file-tile", { hasText: "readme.md" });
  const target = panes.nth(1).locator(".file-tile", { hasText: "data" });

  // Drive the native HTML5 drag-and-drop sequence directly (Playwright's
  // dragTo does not synthesise dragover/drop reliably in headless Chromium).
  await source.evaluate((el) => {
    const dt = new DataTransfer();
    el.dispatchEvent(new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: dt }));
    window.__dt = dt;
  });
  await target.evaluate((el) => {
    const dt = window.__dt;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const opts = { bubbles: true, cancelable: true, dataTransfer: dt, clientX: cx, clientY: cy };
    el.dispatchEvent(new DragEvent("dragover", opts));
    el.dispatchEvent(new DragEvent("drop", opts));
  });
  // No dragend dispatch: the drop already cleared the drag payload, and the
  // source tile is removed from the DOM once the move refetch lands.

  // readme.md moved into data/, so neither home pane lists it any more.
  await expect(page.locator(".file-tile", { hasText: "readme.md" })).toHaveCount(0);
  // Navigating a pane into data/ reveals the moved file.
  await panes.nth(0).locator(".file-tile", { hasText: "data" }).dblclick();
  await expect(panes.nth(0).locator(".file-tile", { hasText: "readme.md" })).toBeVisible();
});


test("large directories keep rendered DOM bounded across file views", async ({ page }) => {
  await page.goto("/files?user=alice");
  const pane = page.locator(".file-pane").first();
  await pane.getByTitle("手动输入路径").click();
  await pane.getByLabel("路径").fill(`${HOME}/large`);
  await pane.getByLabel("路径").press("Enter");
  await expect(pane).toHaveAttribute("data-pane-cwd", `${HOME}/large`);
  await expect(pane.locator(".filepane-status")).toContainText("500 项已加载");

  for (const loaded of [1000, 1500, 2000]) {
    await pane.locator(".filepane-status").getByRole("button", { name: "加载更多" }).click();
    await expect(pane.locator(".filepane-status")).toContainText(`${loaded} 项已加载`);
  }
  expect(await pane.locator(".file-row[data-path]").count()).toBeLessThan(100);

  await pane.getByTitle("网格视图").click();
  expect(await pane.locator(".file-tile[data-path]").count()).toBeLessThan(150);

  await pane.getByTitle("分栏视图").click();
  expect(await pane.locator('.miller-column[data-last-column="true"] .miller-row[data-path]').count()).toBeLessThan(100);
});
