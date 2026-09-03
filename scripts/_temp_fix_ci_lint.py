from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, got {count}: {old!r}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


# Existing zero-semantics lint cleanup so the Python gate can proceed.
replace_once(
    "src/pilot107/adapters/slurm.py",
    "import hmac\nimport heapq\n",
    "import heapq\nimport hmac\n",
)
replace_once(
    "src/pilot107/adapters/slurm.py",
    'raw = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}".encode("utf-8")',
    'raw = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}".encode()',
)
replace_once(
    "src/pilot107/adapters/slurm.py",
    '''                if isinstance(state.get("binding"), dict) and state["binding"].get("path") == str(target):\n''',
    '''                if (\n                    isinstance(state.get("binding"), dict)\n                    and state["binding"].get("path") == str(target)\n                ):\n''',
)
replace_once(
    "src/pilot107/api/agent_tool_routes.py",
    '''    elif code in {"AGENT.TOOL.INVALID", "AGENT.TOOL.INVALID_RESULT"}:\n        status = 400\n    elif code == "AGENT.BUILDER.VALIDATIONS_INVALID":\n        status = 400\n''',
    '''    elif code in {\n        "AGENT.TOOL.INVALID",\n        "AGENT.TOOL.INVALID_RESULT",\n        "AGENT.BUILDER.VALIDATIONS_INVALID",\n    }:\n        status = 400\n''',
)
replace_once(
    "src/pilot107/core/run_service.py",
    '''                failure_reason="SlurmTransportError: submission transport failed without reconciliation",\n''',
    '''                failure_reason=(\n                    "SlurmTransportError: submission transport failed without reconciliation"\n                ),\n''',
)

# Static typing for the local bounded directory generator.
replace_once(
    "src/pilot107/adapters/slurm.py",
    "import urllib.request\nfrom dataclasses import dataclass, field\n",
    "import urllib.request\nfrom collections.abc import Iterator\nfrom dataclasses import dataclass, field\n",
)
replace_once(
    "src/pilot107/adapters/slurm.py",
    "        def candidates():\n",
    "        def candidates() -> Iterator[FileEntry]:\n",
)

# Keep list and search page types distinct at the HTTP boundary.
file_routes = Path("src/pilot107/api/file_routes.py")
text = file_routes.read_text(encoding="utf-8")
old_list = '''                page = self.executor.list_dir(\n                    path=path,\n                    owner=owner,\n                    limit=limit,\n                    cursor=_first_param(params, "cursor"),\n                )\n'''
new_list = old_list.replace("page =", "list_page =")
if text.count(old_list) != 1:
    raise RuntimeError("expected one directory list page assignment")
text = text.replace(old_list, new_list, 1)
for old, new in (
    ("\"path\": page.path", "\"path\": list_page.path"),
    ("for entry in page.entries", "for entry in list_page.entries"),
    ("\"limit\": page.limit", "\"limit\": list_page.limit"),
    ("\"has_more\": page.has_more", "\"has_more\": list_page.has_more"),
    ("\"next_cursor\": page.next_cursor", "\"next_cursor\": list_page.next_cursor"),
    ("\"directory_revision\": page.directory_revision", "\"directory_revision\": list_page.directory_revision"),
):
    if text.count(old) != 1:
        raise RuntimeError(f"expected one list response use: {old}")
    text = text.replace(old, new, 1)
old_search = '''                page = self.executor.search_files(\n'''
if text.count(old_search) != 1:
    raise RuntimeError("expected one search page assignment")
text = text.replace(old_search, '''                search_page = self.executor.search_files(\n''', 1)
for old, new in (
    ("for item in page.items", "for item in search_page.items"),
    ("\"incomplete\": page.incomplete", "\"incomplete\": search_page.incomplete"),
    ("\"next_cursor\": page.next_cursor", "\"next_cursor\": search_page.next_cursor"),
    ("\"warnings\": list(page.warnings)", "\"warnings\": list(search_page.warnings)"),
):
    if text.count(old) != 1:
        raise RuntimeError(f"expected one search response use: {old}")
    text = text.replace(old, new, 1)
file_routes.write_text(text, encoding="utf-8")

# SSH must implement the same bounded FileListPage contract as every FileOps backend.
replace_once(
    "src/pilot107/adapters/ssh_relay.py",
    '''    FileEntry,\n    FileSearchEntry,\n''',
    '''    FileEntry,\n    FileListPage,\n    FileSearchEntry,\n''',
)
ssh_list_script = r'''_SSH_FILE_LIST_SCRIPT = r"""
import hashlib
import heapq
import json
import os
import stat
import sys

request = json.loads(sys.argv[1])
path = sys.argv[2]
limit = request["limit"]
after = request["after"]
directory = os.stat(path, follow_symlinks=False)
raw_revision = f"{directory.st_dev}:{directory.st_ino}:{directory.st_mtime_ns}:{directory.st_ctime_ns}"
revision = hashlib.sha256(raw_revision.encode()).hexdigest()[:24]


def classify(info):
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "other"


def candidates():
    with os.scandir(path) as handle:
        for item in handle:
            try:
                info = item.stat(follow_symlinks=False)
            except OSError:
                continue
            kind = classify(info)
            key = [0 if kind == "dir" else 1, item.name.casefold(), item.name]
            if after is not None and key <= after:
                continue
            yield (
                tuple(key),
                {
                    "name": item.name,
                    "type": kind,
                    "size": info.st_size,
                    "mtime": int(info.st_mtime),
                },
            )


selected = heapq.nsmallest(limit + 1, candidates(), key=lambda pair: pair[0])
has_more = len(selected) > limit
entries = [item for _, item in selected[:limit]]
print(
    json.dumps(
        {
            "entries": entries,
            "has_more": has_more,
            "directory_revision": revision,
        },
        separators=(",", ":"),
    )
)
"""


'''
replace_once(
    "src/pilot107/adapters/ssh_relay.py",
    '_SSH_FILE_SEARCH_SCRIPT = r"""\n',
    ssh_list_script + '_SSH_FILE_SEARCH_SCRIPT = r"""\n',
)
old_ssh_list = '''    def list_dir(\n        self, *, path: str, owner: str, timeout_seconds: float = 30.0\n    ) -> list[FileEntry]:\n        self._require_file_owner(owner)\n        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())\n        script = (\n            'python3 -c "import json,os,stat,sys;p=sys.argv[1];'\n            "print(json.dumps([{'name':n,"\n            "'type':'symlink' if stat.S_ISLNK(s.st_mode) "\n            "else ('dir' if stat.S_ISDIR(s.st_mode) "\n            "else ('file' if stat.S_ISREG(s.st_mode) "\n            "else ('socket' if stat.S_ISSOCK(s.st_mode) "\n            "else ('fifo' if stat.S_ISFIFO(s.st_mode) "\n            "else ('device' if stat.S_ISCHR(s.st_mode) or stat.S_ISBLK(s.st_mode) "\n            "else 'other'))))),"\n            "'size':s.st_size,'mtime':int(s.st_mtime)}"\n            " for n in sorted(os.listdir(p)) for s in [os.lstat(os.path.join(p,n))]]))"\n            '" '\n        ) + shlex.quote(safe_path)\n        result = self._file_shell(script, timeout_seconds=timeout_seconds)\n        if result.returncode != 0:\n            raise SlurmTransportError("SSH.LIST_DIR_FAILED")\n        payload = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "[]"\n        try:\n            decoded = json.loads(payload)\n        except json.JSONDecodeError as exc:\n            raise SlurmTransportError("SSH.LIST_DIR_INVALID") from exc\n        return [\n            FileEntry(\n                name=str(item.get("name", "")),\n                type=str(item.get("type", "other")),\n                size=int(item.get("size", 0)),\n                mtime=int(item.get("mtime", 0)),\n            )\n            for item in decoded\n            if isinstance(item, dict)\n        ]\n'''
new_ssh_list = '''    def list_dir(\n        self,\n        *,\n        path: str,\n        owner: str,\n        limit: int = 500,\n        cursor: str | None = None,\n        timeout_seconds: float = 30.0,\n    ) -> FileListPage:\n        self._require_file_owner(owner)\n        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())\n        if not 1 <= limit <= 1000:\n            raise SlurmSubmissionRejected("limit must be between 1 and 1000")\n\n        cursor_binding: dict[str, object] | None = None\n        after: list[int | str] | None = None\n        if cursor:\n            state = _decode_local_search_cursor(cursor, self._search_cursor_key)\n            raw_binding = state.get("binding")\n            if not isinstance(raw_binding, dict):\n                raise SlurmSubmissionRejected("invalid directory listing cursor")\n            if (\n                raw_binding.get("kind") != "list_dir"\n                or raw_binding.get("owner") != owner\n                or raw_binding.get("path") != safe_path\n            ):\n                raise SlurmSubmissionRejected(\n                    "directory listing cursor does not match request"\n                )\n            raw_after = state.get("after")\n            if (\n                not isinstance(raw_after, list)\n                or len(raw_after) != 3\n                or not isinstance(raw_after[0], int)\n                or not isinstance(raw_after[1], str)\n                or not isinstance(raw_after[2], str)\n            ):\n                raise SlurmSubmissionRejected("invalid directory listing cursor")\n            cursor_binding = raw_binding\n            after = [raw_after[0], raw_after[1], raw_after[2]]\n\n        request = json.dumps(\n            {"limit": limit, "after": after},\n            sort_keys=True,\n            separators=(",", ":"),\n        )\n        command = " ".join(\n            shlex.quote(token)\n            for token in ("python3", "-c", _SSH_FILE_LIST_SCRIPT, request, safe_path)\n        )\n        result = self._file_shell(command, timeout_seconds=timeout_seconds)\n        if result.returncode != 0:\n            raise SlurmTransportError("SSH.LIST_DIR_FAILED")\n        payload = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "{}"\n        try:\n            decoded = json.loads(payload)\n        except json.JSONDecodeError as exc:\n            raise SlurmTransportError("SSH.LIST_DIR_INVALID") from exc\n        if not isinstance(decoded, dict):\n            raise SlurmTransportError("SSH.LIST_DIR_INVALID")\n        raw_entries = decoded.get("entries")\n        revision = decoded.get("directory_revision")\n        has_more = decoded.get("has_more")\n        if not isinstance(raw_entries, list):\n            raise SlurmTransportError("SSH.LIST_DIR_INVALID")\n        if not isinstance(revision, str) or not revision:\n            raise SlurmTransportError("SSH.LIST_DIR_INVALID")\n        if not isinstance(has_more, bool):\n            raise SlurmTransportError("SSH.LIST_DIR_INVALID")\n        if cursor_binding is not None and cursor_binding.get("revision") != revision:\n            raise SlurmSubmissionRejected("directory listing cursor is stale")\n\n        entries = tuple(\n            FileEntry(\n                name=str(item.get("name", "")),\n                type=str(item.get("type", "other")),\n                size=int(item.get("size", 0)),\n                mtime=int(item.get("mtime", 0)),\n            )\n            for item in raw_entries\n            if isinstance(item, dict)\n        )\n        if len(entries) > limit or (has_more and not entries):\n            raise SlurmTransportError("SSH.LIST_DIR_INVALID")\n        binding = {\n            "kind": "list_dir",\n            "owner": owner,\n            "path": safe_path,\n            "revision": revision,\n        }\n        next_cursor = None\n        if has_more:\n            last = entries[-1]\n            key = [0 if last.type == "dir" else 1, last.name.casefold(), last.name]\n            next_cursor = _encode_local_search_cursor(\n                {"binding": binding, "after": key}, self._search_cursor_key\n            )\n        return FileListPage(\n            path=safe_path,\n            entries=entries,\n            limit=limit,\n            has_more=has_more,\n            next_cursor=next_cursor,\n            directory_revision=revision,\n        )\n'''
replace_once("src/pilot107/adapters/ssh_relay.py", old_ssh_list, new_ssh_list)

# Wire the bounded adapter for Agent Workspace without changing its core contract.
replace_once(
    "src/pilot107/api/service.py",
    '''from pilot107.adapters.ssh_relay import (\n    SshRelayClient,\n    SshRelayConfig,\n    SshRelayExecutor,\n    SubprocessSshRelayClient,\n)\n''',
    '''from pilot107.adapters.ssh_relay import (\n    SshRelayClient,\n    SshRelayConfig,\n    SshRelayExecutor,\n    SubprocessSshRelayClient,\n)\nfrom pilot107.adapters.workspace_source import PagedWorkspaceSourceReader\n''',
)
replace_once(
    "src/pilot107/api/service.py",
    '''            workspace_source = HttpCommandGatewayExecutor(\n                base_url=config.command_gateway_url,\n                token=config.command_gateway_token,\n                timeout_seconds=config.command_timeout_seconds,\n            )\n''',
    '''            workspace_source = PagedWorkspaceSourceReader(\n                HttpCommandGatewayExecutor(\n                    base_url=config.command_gateway_url,\n                    token=config.command_gateway_token,\n                    timeout_seconds=config.command_timeout_seconds,\n                )\n            )\n''',
)
replace_once(
    "src/pilot107/api/service.py",
    '''            workspace_source = SshRelayExecutor(ssh_relay_client)\n''',
    '''            workspace_source = PagedWorkspaceSourceReader(\n                SshRelayExecutor(ssh_relay_client)\n            )\n''',
)

# Update the SSH manifest regression to assert the paged contract.
replace_once(
    "tests/test_ssh_relay.py",
    '''        return CommandResult(\n            0,\n            '[{"name":"control.sock","type":"socket","size":0,"mtime":1}]\\n',\n            "",\n        )\n''',
    '''        return CommandResult(\n            0,\n            json.dumps(\n                {\n                    "entries": [\n                        {\n                            "name": "control.sock",\n                            "type": "socket",\n                            "size": 0,\n                            "mtime": 1,\n                        }\n                    ],\n                    "has_more": False,\n                    "directory_revision": "revision-1",\n                }\n            )\n            + "\\n",\n            "",\n        )\n''',
)
replace_once(
    "tests/test_ssh_relay.py",
    '''    entries = SshRelayExecutor(client).list_dir(\n        path="/public/home/alice/exp",\n        owner="alice",\n    )\n\n    assert entries[0].type == "socket"\n''',
    '''    page = SshRelayExecutor(client).list_dir(\n        path="/public/home/alice/exp",\n        owner="alice",\n    )\n\n    assert page.entries[0].type == "socket"\n    assert page.has_more is False\n    assert page.next_cursor is None\n''',
)

print("paged backend contract closure applied")
