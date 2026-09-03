from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, got {count}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "apps/web/src/api.ts",
    '''  fileList: async (\n    user: string,\n    path: string,\n    input: { limit?: number; cursor?: string | null } = {},\n    signal?: AbortSignal,\n  ) => {\n    const raw = await getJson<{\n''',
    '''  fileList: async (\n    user: string,\n    path: string,\n    inputOrSignal: { limit?: number; cursor?: string | null } | AbortSignal = {},\n    signal?: AbortSignal,\n  ) => {\n    const input = inputOrSignal instanceof AbortSignal ? {} : inputOrSignal;\n    const requestSignal = inputOrSignal instanceof AbortSignal ? inputOrSignal : signal;\n    const raw = await getJson<{\n''',
)
replace_once(
    "apps/web/src/api.ts",
    '''    }), user, signal);\n    const base = raw.path.replace(/\\/+$/, "");\n''',
    '''    }), user, requestSignal);\n    const base = raw.path.replace(/\\/+$/, "");\n''',
)

for path in (
    "apps/web/src/files/useFilePane.ts",
    "apps/web/src/files/FilePickerDialog.tsx",
    "apps/web/src/files/MillerColumns.tsx",
    "apps/web/src/files/MoveDialog.tsx",
):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    text = text.replace("cursor: pageParam ?? undefined", "cursor: pageParam")
    p.write_text(text, encoding="utf-8")

print("paged files type fixes applied")
