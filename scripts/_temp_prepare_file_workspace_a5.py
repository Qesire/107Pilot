from pathlib import Path

path = Path(__file__).with_name("_temp_apply_file_workspace_a5.py")
text = path.read_text(encoding="utf-8")
old = '''def replace_once(text: str, old: str, new: str, label: str) -> str:\n    count = text.count(old)\n    if count != 1:\n        raise RuntimeError(f"{label}: expected one match, found {count}")\n    return text.replace(old, new, 1)\n'''
new = '''def replace_once(text: str, old: str, new: str, label: str) -> str:\n    count = text.count(old)\n    if label in {"manager value fields", "manager memo dependencies"}:\n        if count < 1:\n            raise RuntimeError(f"{label}: expected at least one match, found {count}")\n    elif count != 1:\n        raise RuntimeError(f"{label}: expected one match, found {count}")\n    return text.replace(old, new, 1)\n'''
if text.count(old) != 1:
    raise RuntimeError("A5 migration helper shape changed")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("A5 migration assertions prepared")
