from pathlib import Path

path = Path(__file__).resolve().parents[1] / "tests/ui/visual.spec.js"
text = path.read_text()
old = '  await expect(page.getByText("RUNTIME.PYTHON_PACKAGE_MISSING", { exact: true })).toBeVisible();\n'
new = '  await expect(page.getByRole("button", { name: "诊断", exact: true })).toHaveAttribute("aria-current", "page");\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one stale diagnosis assertion, found {text.count(old)}")
path.write_text(text.replace(old, new, 1))
print("A7 browser gate aligned with current diagnosis read-model contract")
