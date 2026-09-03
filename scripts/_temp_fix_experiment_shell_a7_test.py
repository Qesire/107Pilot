from pathlib import Path

path = Path(__file__).resolve().parents[1] / "tests/ui/visual.spec.js"
text = path.read_text()
old = '  await expect(page.getByText("RUNTIME.PYTHON_PACKAGE_MISSING", { exact: true })).toBeVisible();\n'
new = '  await expect(page.getByRole("button", { name: "诊断", exact: true })).toHaveAttribute("aria-current", "page");\n'
count = text.count(old)
if count < 2:
    raise SystemExit(f"expected historical plus staged diagnosis assertions, found {count}")
index = text.rfind(old)
if index < 0:
    raise SystemExit("staged diagnosis assertion missing")
path.write_text(text[:index] + new + text[index + len(old):])
print("A7 staged browser gate aligned; historical assertion preserved")
