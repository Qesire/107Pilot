from pathlib import Path

path = Path(__file__).resolve().parents[1] / "apps/web/src/files/FileInspector.tsx"
text = path.read_text(encoding="utf-8")
old = "  const entry = selected[0];\n"
new = "  const entry = selected[0]!;\n"
if text.count(old) != 1:
    raise RuntimeError("FileInspector single-selection shape changed")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("A5 inspector narrowing fixed")
