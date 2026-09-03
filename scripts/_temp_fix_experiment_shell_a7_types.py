from pathlib import Path

path = Path(__file__).resolve().parents[1] / "apps/web/src/ExperimentShell.tsx"
text = path.read_text()
replacements = {
    '  onClick?: () => void;\n': '  onClick?: (() => void) | undefined;\n',
    'label={context.kind === "contract" && context.dirty ? "配置有未持久化修改" : context.contractId ? "配置已持久化" : "配置草稿"}': 'label={context.kind === "contract" && context.dirty ? "配置有未持久化修改" : contractId ? "配置已持久化" : "配置草稿"}',
    'tone={context.kind === "contract" && context.dirty ? "warning" : context.contractId ? "success" : "neutral"}': 'tone={context.kind === "contract" && context.dirty ? "warning" : contractId ? "success" : "neutral"}',
}
for old, new in replacements.items():
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one occurrence: {old!r}; found {text.count(old)}")
    text = text.replace(old, new, 1)
path.write_text(text)
print("A7 strict TypeScript corrections applied")
