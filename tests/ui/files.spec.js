const { expect, test } = require("@playwright/test");

// In-memory filesystem template. Each test gets a fresh deep copy so rename
// (move) mutations stay isolated.
const HOME = "/public/home/alice";
function buildFs() {
  return {
    [HOME]: [
      { name: "docs", type: "dir", size: 0, mtime: 1700000000 },
      { name: "data", type: "dir", size: 0, mtime: 1700000000 },
      { name: "readme.md", type: "file", size: 2048, mtime: 1700000100 },
      { name: "run.py", type: "file", size: 512, mtime: 1700000200 },
    ],
    [`${HOME}/docs`]: [
      { name: "note.txt", type: "file", size: 128, mtime: 1700000300 },
    ],
    [`${HOME}/data`]: [
      { name: "sample.csv", type: "file", size: 4096, mtime: 1700000400 },
    ],
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
      return json({ path, entries: fs[path] ?? [] });
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
  // Clear persisted layout once (not via addInitScript, which would also wipe
  // it on reload and break the persistence test).
  await page.goto("/files?user=alice");
  await page.evaluate(() => window.localStorage.clear());
});

test("defaults to two independent panes rooted at home", async ({ page }) => {
  await page.goto("/files?user=alice");

  await expect(page.locator(".file-pane")).toHaveCount(2);
  // Both panes list the home directory contents.
  await expect(page.locator(".file-tile", { hasText: "readme.md" })).toHaveCount(2);
  await expect(page.locator(".file-tile", { hasText: "docs" }).first()).toBeVisible();
});

test("panes navigate independently", async ({ page }) => {
  await page.goto("/files?user=alice");

  const panes = page.locator(".file-pane");
  // Open "docs" in the first pane only.
  await panes.nth(0).locator(".file-tile", { hasText: "docs" }).dblclick();

  // First pane now shows the docs contents; second pane is still at home.
  await expect(panes.nth(0).locator(".file-tile", { hasText: "note.txt" })).toBeVisible();
  await expect(panes.nth(1).locator(".file-tile", { hasText: "readme.md" })).toBeVisible();
  await expect(panes.nth(1).locator(".file-tile", { hasText: "note.txt" })).toHaveCount(0);
});

test("splitting adds a pane and the layout survives a reload", async ({ page }) => {
  await page.goto("/files?user=alice");
  await expect(page.locator(".file-pane")).toHaveCount(2);

  // Split the first pane horizontally.
  await page.locator(".file-pane").nth(0).getByTitle("横向拆分").click();
  await expect(page.locator(".file-pane")).toHaveCount(3);

  await page.reload();
  await expect(page.locator(".file-pane")).toHaveCount(3);
});

test("closing a pane removes it but keeps at least one", async ({ page }) => {
  await page.goto("/files?user=alice");
  await page.locator(".file-pane").nth(1).getByTitle("关闭窗格").click();
  await expect(page.locator(".file-pane")).toHaveCount(1);

  // The last pane cannot be closed.
  await page.locator(".file-pane").nth(0).getByTitle("关闭窗格").click();
  await expect(page.locator(".file-pane")).toHaveCount(1);
});

test("grid view toggles to a list view per pane", async ({ page }) => {
  await page.goto("/files?user=alice");
  const firstPane = page.locator(".file-pane").nth(0);

  await expect(firstPane.locator(".filegrid")).toBeVisible();
  await firstPane.getByTitle("列表视图").click();
  await expect(firstPane.locator(".filepane-table")).toBeVisible();
  await expect(firstPane.locator(".filegrid")).toHaveCount(0);
});

test("ctrl-click builds a multi-selection with an action bar", async ({ page }) => {
  await page.goto("/files?user=alice");
  const firstPane = page.locator(".file-pane").nth(0);

  await firstPane.locator(".file-tile", { hasText: "readme.md" }).click();
  await firstPane.locator(".file-tile", { hasText: "run.py" }).click({ modifiers: ["Control"] });

  await expect(firstPane.locator(".filepane-selectionbar")).toContainText("2 项已选");
});

test("marquee drag over empty grid area selects the tiles it covers", async ({ page }) => {
  await page.goto("/files?user=alice");
  const firstPane = page.locator(".file-pane").nth(0);

  // Geometry-driven marquee: start in the grid's empty bottom strip and sweep
  // up-right across the first two tiles (data, docs) but not the rest.
  // Beginning in empty grid space lets Selecto (not native tile drag) own it.
  const box = await firstPane.evaluate((el) => {
    const rects = [...el.querySelectorAll(".file-tile")].map((t) => t.getBoundingClientRect());
    const grid = el.querySelector(".filegrid").getBoundingClientRect();
    return {
      sx: Math.round(rects[0].left + 4),
      sy: Math.round(grid.bottom - 4),
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
  // Wait for both panes to be fully rendered (4 tiles each) before dragging.
  await expect(page.locator(".file-tile")).toHaveCount(8);
  const panes = page.locator(".file-pane");

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
