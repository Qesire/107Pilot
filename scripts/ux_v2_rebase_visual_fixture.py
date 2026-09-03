from pathlib import Path

spec = Path("tests/ui/visual.spec.js")
text = spec.read_text()

old_workspace = '''test("workspace renders live run and platform read models", async ({ page }) => {
  await page.goto("/projects?user=alice");

  await expect(page.getByRole("heading", { name: "把下一次提交建立在可验证事实之上" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看 run_alice_succeeded" })).toBeVisible();
  await expect(page.getByText("acct_alice", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("可见分区", { exact: true })).toBeVisible();
  await expect(page.locator(".signal-strip").getByText("2", { exact: true })).toBeVisible();
  await capture(page, "phase3d-workspace.png");
});
'''
new_workspace = '''test("workspace prioritizes current work and preparation facts", async ({ page }) => {
  await page.goto("/projects?user=alice");

  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "当前工作" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看 run_alice_succeeded" })).toBeVisible();
  await expect(page.getByText("acct_alice", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("可见分区", { exact: true })).toBeVisible();
  await expect(page.getByText("1 次运行需要处理", { exact: true })).toBeVisible();
  await capture(page, "phase3d-workspace.png");
});

'''
if old_workspace not in text:
    raise SystemExit("main workspace test marker not found")
text = text.replace(old_workspace, new_workspace, 1)

run_filter_marker = 'test("run filters are URL-controlled and narrow the server query", async ({ page }) => {'
file_test = '''test("file workspace consumes backend storage and upload session read models", async ({ page }) => {
  await page.goto("/files?user=alice");

  await expect(page.getByRole("heading", { name: "文件工作区" })).toBeVisible();
  await expect(page.getByText("个人存储", { exact: true })).toBeVisible();
  await expect(page.getByText("后台传输", { exact: true })).toBeVisible();
  await expect(page.getByText("dataset.tar.gz", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/完整性已验证，正在写入/)).toBeVisible();
  await expect(page.getByLabel("压缩包上传后处理")).toHaveValue("keep");
});

'''
if run_filter_marker not in text:
    raise SystemExit("run filter insertion marker not found")
text = text.replace(run_filter_marker, file_test + run_filter_marker, 1)

text = text.replace(
    'page.getByLabel("Workdir")',
    'page.getByRole("textbox", { name: "工作目录", exact: true })',
)

source_marker = 'test("dirty source is not silently overwritten by a basic form update", async ({ page }) => {'
picker_tests = '''test("studio workdir picker browses backend directories without leaving the contract", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览工作目录" }).click();
  await expect(page.getByRole("dialog", { name: "选择实验工作目录" })).toBeVisible();
  await page.getByRole("button", { name: /project-a/ }).click();
  await page.getByRole("button", { name: "选择此目录" }).click();
  await expect(page.getByRole("textbox", { name: "工作目录", exact: true })).toHaveValue("/public/home/alice/project-a");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
});

test("recipe shared_path browses existing backend files and writes canonical field", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览 runtime.environment.DATA_ROOT" }).click();
  await expect(page.getByRole("dialog", { name: /选择共享路径/ })).toBeVisible();
  await page.getByRole("button", { name: /dataset.tar.gz/ }).click();
  await page.getByRole("button", { name: "选择此文件" }).click();
  await expect(page.getByRole("textbox", { name: /^runtime\\.environment\\.DATA_ROOT/ })).toHaveValue("/public/home/alice/dataset.tar.gz");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
});

'''
if source_marker not in text:
    raise SystemExit("source test insertion marker not found")
text = text.replace(source_marker, picker_tests + source_marker, 1)

schema_marker = '    if (url.pathname === "/api/v1/contracts/schema") {'
backend_routes = '''    if (url.pathname === "/api/v1/files/usage") {
      return json(route, {
        home: "/public/home/alice",
        used_bytes: 1073741824,
        total_bytes: 2147483648,
        observed_at: "2026-09-03T04:00:00Z",
      });
    }
    if (url.pathname === "/api/v1/files/uploads") {
      return json(route, {
        items: [{
          upload_id: "upload_visual_001",
          owner: "alice",
          target_path: "/public/home/alice",
          filename: "dataset.tar.gz",
          total_size: 100,
          is_partial: false,
          received_bytes: 100,
          sha256_expected: null,
          sha256_actual: "a".repeat(64),
          state: "verified",
          auto_extract: false,
          created_at: "2026-09-03T04:00:00Z",
          written_path: null,
          extracted_members: null,
          error: null,
        }],
      });
    }
    if (url.pathname === "/api/v1/files") {
      const currentPath = url.searchParams.get("path") || "/public/home/alice";
      return json(route, {
        path: currentPath,
        entries: currentPath === "/public/home/alice"
          ? [
              { name: "project-a", type: "directory", size: 0, mtime: 1788408000 },
              { name: "dataset.tar.gz", type: "file", size: 100, mtime: 1788408000 },
            ]
          : [],
      });
    }
    if (url.pathname === "/api/v1/recipes/recipe_python_cpu/versions/1.0.0") {
      return json(route, {
        recipe_id: "recipe_python_cpu",
        version: "1.0.0",
        parameter_schema: {
          "runtime.environment.DATA_ROOT": {
            type: "shared_path",
            prefix: "/public/home/alice",
            contract: "选择已存在的共享输入文件或目录。",
          },
        },
      });
    }
'''
if schema_marker not in text:
    raise SystemExit("contract schema route marker not found")
text = text.replace(schema_marker, backend_routes + schema_marker, 1)

spec.write_text(text)
