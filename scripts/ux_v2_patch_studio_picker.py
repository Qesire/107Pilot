from pathlib import Path

studio = Path("apps/web/src/StudioPage.tsx")
text = studio.read_text()

marker = 'import { QueryBoundary, SectionHeading, StatusBadge } from "./components";\n'
addition = marker + 'import { FilePickerDialog } from "./files/FilePickerDialog";\n'
if 'FilePickerDialog' not in text:
    if marker not in text:
        raise SystemExit("Studio import marker not found")
    text = text.replace(marker, addition, 1)

old = '<BasicProjection contract={canonical} recipes={recipes.data?.items ?? []} update={update} parameterSchema={parameterSchema} />'
new = '<BasicProjection user={user} contract={canonical} recipes={recipes.data?.items ?? []} update={update} parameterSchema={parameterSchema} />'
if old in text:
    text = text.replace(old, new, 1)
elif '<BasicProjection user={user}' not in text:
    raise SystemExit("BasicProjection call marker not found")

old = 'function BasicProjection({ contract, recipes, update, parameterSchema }: ProjectionProps & { recipes: Array<{ recipe_id: string; latest_version: string; title: string }>; parameterSchema?: unknown }) {'
new = 'function BasicProjection({ user, contract, recipes, update, parameterSchema }: ProjectionProps & { user: string; recipes: Array<{ recipe_id: string; latest_version: string; title: string }>; parameterSchema?: unknown }) {'
if old in text:
    text = text.replace(old, new, 1)
elif 'function BasicProjection({ user, contract' not in text:
    raise SystemExit("BasicProjection signature marker not found")

marker = '  const workdirValue = readContractValue(contract, ["project", "workdir"], "");\n'
addition = marker + '  const [workdirPickerOpen, setWorkdirPickerOpen] = useState(false);\n'
if 'workdirPickerOpen' not in text:
    if marker not in text:
        raise SystemExit("workdir state marker not found")
    text = text.replace(marker, addition, 1)

old = '        <TextField className="span-2" label={requiredLabel("project.workdir", "Workdir")} value={workdirValue} onChange={(value) => update(["project", "workdir"], value)} customizable={isPlaceholderValue(workdirValue)} placeholder={fieldOf("project.workdir")?.prefix ?? undefined} />'
new = '''        <div className="path-field-browser span-2">
          <TextField label={requiredLabel("project.workdir", "工作目录")} value={workdirValue} onChange={(value) => update(["project", "workdir"], value)} customizable={isPlaceholderValue(workdirValue)} placeholder={fieldOf("project.workdir")?.prefix ?? undefined} />
          <button type="button" className="button secondary" aria-label="浏览工作目录" onClick={() => setWorkdirPickerOpen(true)}>浏览…</button>
        </div>
        {workdirPickerOpen ? <FilePickerDialog user={user} homePath={`/public/home/${user}`} initialPath={workdirValue} title="选择实验工作目录" onSelect={(path) => { update(["project", "workdir"], path); setWorkdirPickerOpen(false); }} onClose={() => setWorkdirPickerOpen(false)} /> : null}'''
if old in text:
    text = text.replace(old, new, 1)
elif 'aria-label="浏览工作目录"' not in text:
    raise SystemExit("Workdir field marker not found")

text = text.replace('label={requiredLabel("entry.command", "Command")}', 'label={requiredLabel("entry.command", "运行命令")}')

old = '<TemplateExtraParameters contract={contract} schema={parameterSchema} update={update} />'
new = '<TemplateExtraParameters user={user} contract={contract} schema={parameterSchema} update={update} />'
if old in text:
    text = text.replace(old, new, 1)
elif '<TemplateExtraParameters user={user}' not in text:
    raise SystemExit("TemplateExtraParameters call marker not found")
studio.write_text(text)

main = Path("apps/web/src/main.tsx")
text = main.read_text()
marker = 'import "./styles/task-indicator-v2.css";\n'
addition = marker + 'import "./styles/file-picker-v2.css";\n'
if 'file-picker-v2.css' not in text:
    if marker not in text:
        raise SystemExit("main picker style marker not found")
    text = text.replace(marker, addition, 1)
main.write_text(text)

spec = Path("tests/ui/visual.spec.js")
text = spec.read_text().replace('getByLabel("Workdir")', 'getByLabel("工作目录")')
marker = 'test("dirty source is not silently overwritten by a basic form update", async ({ page }) => {'
workdir_test = '''test("studio workdir picker browses backend directories without leaving the contract", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览工作目录" }).click();
  await expect(page.getByRole("dialog", { name: "选择实验工作目录" })).toBeVisible();
  await page.getByRole("button", { name: /project-a/ }).click();
  await page.getByRole("button", { name: "选择此目录" }).click();
  await expect(page.getByLabel("工作目录")).toHaveValue("/public/home/alice/project-a");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
});

'''
shared_path_test = '''test("recipe shared_path browses existing backend files and writes canonical field", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览 runtime.environment.DATA_ROOT" }).click();
  await expect(page.getByRole("dialog", { name: /选择共享路径/ })).toBeVisible();
  await page.getByRole("button", { name: /dataset.tar.gz/ }).click();
  await page.getByRole("button", { name: "选择此文件" }).click();
  await expect(page.getByLabel(/runtime.environment.DATA_ROOT/)).toHaveValue("/public/home/alice/dataset.tar.gz");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
});

'''
if workdir_test.strip() not in text:
    if marker not in text:
        raise SystemExit("Studio test insertion marker not found")
    text = text.replace(marker, workdir_test + marker, 1)
if shared_path_test.strip() not in text:
    if marker not in text:
        raise SystemExit("shared_path test insertion marker not found")
    text = text.replace(marker, shared_path_test + marker, 1)

route_start = 'if (url.pathname === "/api/v1/files") {'
route_end = '    if (url.pathname === "/api/v1/contracts/schema") {'
if 'name: "project-a"' not in text:
    start = text.find(route_start)
    end = text.find(route_end, start)
    if start < 0 or end < 0:
        raise SystemExit("file listing route markers not found")
    replacement = '''if (url.pathname === "/api/v1/files") {
  const path = url.searchParams.get("path") || "/public/home/alice";
  return json(route, {
    path,
    entries: path === "/public/home/alice"
      ? [
          { name: "project-a", type: "directory", size: 0, mtime: 1788408000 },
          { name: "dataset.tar.gz", type: "file", size: 100, mtime: 1788408000 },
        ]
      : [],
  });
}
'''
    text = text[:start] + replacement + text[end:]

recipe_route_marker = '    if (url.pathname === "/api/v1/contracts/schema") {'
recipe_route = '''    if (url.pathname === "/api/v1/recipes/recipe_python_cpu/versions/1.0.0") {
      return json(route, {
        recipe_id: "recipe_python_cpu",
        version: "1.0.0",
        parameter_schema: {
          required: ["runtime.environment.DATA_ROOT"],
          "runtime.environment.DATA_ROOT": {
            type: "shared_path",
            prefix: "/public/home/alice",
            contract: "选择已存在的共享输入文件或目录。",
          },
        },
      });
    }
'''
if '/api/v1/recipes/recipe_python_cpu/versions/1.0.0' not in text:
    if recipe_route_marker not in text:
        raise SystemExit("recipe version route marker not found")
    text = text.replace(recipe_route_marker, recipe_route + recipe_route_marker, 1)
spec.write_text(text)
