from __future__ import annotations

import difflib
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from .providers import ToolCall

READ_LIMIT_BYTES = 64 * 1024
WRITE_LIMIT_BYTES = 64 * 1024
_CONTROL_DIR = ".harness"
_BACKUP_DIR = "backups"
_SENSITIVE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".docker",
    ".git",
    ".gnupg",
    ".kube",
    ".ssh",
    "credentials",
    "credential",
}
_SENSITIVE_DIRECTORY_PAIRS = {
    (".config", "gcloud"),
    (".config", "gh"),
    (".config", "azure"),
    (".config", "rclone"),
}
_SENSITIVE_FILENAMES = {
    ".git-credentials",
    ".gitconfig",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".ppk", ".jks", ".kdbx"}
_SENSITIVE_NAME_PART = re.compile(
    r"(^|[._-])(credential|credentials|secret|secrets|token|tokens|password|passwd|key|keys|api[-_]?key|access[-_]?key|private[-_]?key)([._-]|$)",
    re.IGNORECASE,
)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the current workspace; the user must approve every read.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Preview and write a UTF-8 text file inside the current workspace; the user must approve every change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
]


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActionRequest:
    operation: str
    path: str
    diff: str = ""
    backup_id: str | None = None
    source: str = "tool"


@dataclass(frozen=True)
class WritePreview:
    path: str
    canonical_path: str
    target: Path
    existed: bool
    before_bytes: bytes | None
    before_content: str
    new_content: str
    diff: str
    changed: bool


ApprovalCallback = Callable[[ActionRequest], bool]


def authorize_subagent_plan(
    tasks: tuple[str, ...], approve_action: ApprovalCallback
) -> bool:
    """Request explicit local approval for the exact text a child will receive."""
    if not tasks or len(tasks) > 3 or any(not isinstance(task, str) or len(task) > 240 for task in tasks):
        raise WorkspaceError("子代理计划超出本地任务范围上限。")
    preview = "\n".join(
        f"子任务 {index}: {task}\n输入范围：仅此任务文字；不包含主会话、文件引用或工具结果。"
        for index, task in enumerate(tasks, 1)
    )
    return approve_action(
        ActionRequest(
            operation="delegate",
            path="受限只读子代理",
            diff=preview,
            source="subagent",
        )
    )


class Workspace:
    """File access constrained to a canonical workspace root."""

    def __init__(self, root: str | Path) -> None:
        try:
            self.root = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(f"工作区路径不可用：{type(exc).__name__}") from None
        if not self.root.is_dir():
            raise WorkspaceError("工作区必须是已存在的目录。")
        root_parts = tuple(part.casefold() for part in self.root.parts)
        if _CONTROL_DIR.casefold() in root_parts or any(
            part in _SENSITIVE_DIRECTORIES for part in root_parts
        ):
            raise WorkspaceError("不能把凭据目录、版本控制元数据或 Harness 内部目录设为工作区。")
        if any(
            tuple(root_parts[index : index + 2]) in _SENSITIVE_DIRECTORY_PAIRS
            for index in range(len(root_parts) - 1)
        ):
            raise WorkspaceError("不能把凭据目录设为工作区。")

    def resolve(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise WorkspaceError("文件路径不能为空。")
        if "\x00" in relative_path or any(ord(char) < 32 for char in relative_path):
            raise WorkspaceError("文件路径包含无效控制字符。")

        windows_path = PureWindowsPath(relative_path)
        if (
            Path(relative_path).is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or bool(windows_path.root)
        ):
            raise WorkspaceError("只允许工作区内的相对路径。")
        if ":" in relative_path:
            raise WorkspaceError("不允许 Windows 备用数据流或带冒号的路径。")

        try:
            candidate = (self.root / relative_path).resolve(strict=False)
            candidate.relative_to(self.root)
        except (OSError, RuntimeError):
            raise WorkspaceError("路径解析失败或包含循环链接。") from None
        except ValueError:
            raise WorkspaceError("拒绝访问工作区之外的路径。") from None

        if candidate == self.root:
            raise WorkspaceError("路径必须指向工作区中的文件。")
        self._reject_internal_state_path(candidate)
        self._reject_sensitive_path(candidate, relative_path)
        return candidate

    def _relative_parts(self, candidate: Path) -> tuple[str, ...]:
        try:
            return tuple(part.casefold() for part in candidate.relative_to(self.root).parts)
        except ValueError:
            raise WorkspaceError("拒绝访问工作区之外的路径。") from None

    def _reject_internal_state_path(self, candidate: Path) -> None:
        parts = self._relative_parts(candidate)
        if _CONTROL_DIR.casefold() in parts:
            raise WorkspaceError("Harness 备份目录是内部状态，工具不能读取或修改。")

    def _reject_sensitive_path(self, candidate: Path, raw_path: str) -> None:
        parts = list(self._relative_parts(candidate))
        raw_parts = [part.rstrip(" .").casefold() for part in re.split(r"[\\/]+", raw_path)]
        all_parts = parts + raw_parts
        if any(part in _SENSITIVE_DIRECTORIES for part in all_parts):
            raise WorkspaceError("敏感凭据目录默认禁止工具读取或修改。")
        if any(_SENSITIVE_NAME_PART.search(part) for part in parts[:-1] + raw_parts[:-1]):
            raise WorkspaceError("敏感凭据目录默认禁止工具读取或修改。")
        if any(
            tuple(all_parts[index : index + 2]) in _SENSITIVE_DIRECTORY_PAIRS
            for index in range(len(all_parts) - 1)
        ):
            raise WorkspaceError("敏感凭据目录默认禁止工具读取或修改。")

        basename = parts[-1] if parts else ""
        raw_basename = raw_parts[-1] if raw_parts else ""
        names = {basename, raw_basename}
        if any(
            name.startswith(".env")
            or name in _SENSITIVE_FILENAMES
            or Path(name).suffix.casefold() in _SENSITIVE_SUFFIXES
            or bool(_SENSITIVE_NAME_PART.search(name))
            for name in names
        ):
            raise WorkspaceError("敏感文件默认禁止工具读取或修改。")

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise WorkspaceError(f"无法检查路径属性：{type(exc).__name__}") from None
        if os.name == "nt":
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            return bool(getattr(info, "st_file_attributes", 0) & reparse_flag)
        return stat.S_ISLNK(info.st_mode)

    def _read_snapshot(self, target: Path) -> bytes | None:
        if not target.exists():
            return None
        try:
            if not target.is_file():
                raise WorkspaceError("目标路径不是普通文件。")
            info = target.stat()
            if getattr(info, "st_nlink", 1) > 1:
                raise WorkspaceError("拒绝访问含多个硬链接的文件，避免越过工作区边界。")
            if info.st_size > READ_LIMIT_BYTES:
                raise WorkspaceError("文件超过 64 KiB 安全上限。")
            data = target.read_bytes()
        except WorkspaceError:
            raise
        except OSError as exc:
            raise WorkspaceError(f"读取文件失败：{type(exc).__name__}") from None
        if len(data) > READ_LIMIT_BYTES:
            raise WorkspaceError("文件超过 64 KiB 安全上限。")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("当前仅支持 UTF-8 文本文件。") from None
        return data

    def read_text(self, relative_path: str) -> str:
        target = self.resolve(relative_path)
        data = self._read_snapshot(target)
        if data is None:
            raise WorkspaceError(f"文件不存在或不是普通文件：{relative_path}")
        return data.decode("utf-8-sig")

    @staticmethod
    def _diff(
        path: str,
        before: str,
        after: str,
        *,
        before_exists: bool = True,
        after_exists: bool = True,
    ) -> str:
        old_lines = before.splitlines(keepends=True) if before_exists else []
        new_lines = after.splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path}" if before_exists else "/dev/null",
                tofile=f"b/{path}" if after_exists else "/dev/null",
            )
        )

    def preview_write(self, relative_path: str, content: str) -> WritePreview:
        if not isinstance(content, str):
            raise WorkspaceError("写入内容必须是 UTF-8 文本。")
        try:
            new_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkspaceError("写入内容不是有效 UTF-8 文本。") from None
        if len(new_bytes) > WRITE_LIMIT_BYTES:
            raise WorkspaceError("单次写入超过 64 KiB 上限。")

        target = self.resolve(relative_path)
        if not target.parent.is_dir():
            raise WorkspaceError("目标文件的父目录必须已存在。")
        before_bytes = self._read_snapshot(target)
        existed = before_bytes is not None
        before_content = before_bytes.decode("utf-8") if before_bytes is not None else ""
        canonical_path = target.relative_to(self.root).as_posix()
        diff = self._diff(canonical_path, before_content, content, before_exists=existed)
        return WritePreview(
            path=relative_path,
            canonical_path=canonical_path,
            target=target,
            existed=existed,
            before_bytes=before_bytes,
            before_content=before_content,
            new_content=content,
            diff=diff,
            changed=before_bytes != new_bytes,
        )

    def internal_storage_dir(self, name: str, *, create: bool) -> Path | None:
        if name not in {_BACKUP_DIR, "sessions"}:
            raise WorkspaceError("未授权的 Harness 内部存储目录。")
        control = self.root / _CONTROL_DIR
        storage_dir = control / name
        for directory in (control, storage_dir):
            if not directory.exists():
                if not create:
                    return None
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise WorkspaceError(f"无法创建撤销备份目录：{type(exc).__name__}") from None
            if self._is_reparse_point(directory):
                raise WorkspaceError("拒绝使用 junction/symlink 作为 Harness 备份目录。")
            try:
                resolved = directory.resolve(strict=True)
                resolved.relative_to(self.root)
            except (OSError, RuntimeError, ValueError):
                raise WorkspaceError("Harness 备份目录必须位于工作区内且不能链接到外部。") from None
            if not directory.is_dir():
                raise WorkspaceError("Harness 备份路径不是目录。")
        return storage_dir

    def _backup_dir(self, *, create: bool) -> Path | None:
        return self.internal_storage_dir(_BACKUP_DIR, create=create)

    @staticmethod
    def _atomic_write_bytes(target: Path, data: bytes) -> None:
        if not target.parent.is_dir():
            raise WorkspaceError("目标文件的父目录必须已存在。")
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".harness-tmp-", suffix=".tmp", dir=target.parent
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
            temporary_name = None
        except OSError as exc:
            raise WorkspaceError(f"原子写入失败：{type(exc).__name__}") from None
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    @staticmethod
    def _atomic_write_json(target: Path, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        Workspace._atomic_write_bytes(target, encoded)

    def commit_write(self, preview: WritePreview) -> str | None:
        if not preview.changed:
            return None

        current_target = self.resolve(preview.path)
        if current_target != preview.target:
            raise WorkspaceError("审批后目标路径发生变化；为防止写错文件，本次操作已取消。")
        current_bytes = self._read_snapshot(current_target)
        if current_bytes != preview.before_bytes:
            raise WorkspaceError("审批预览后文件发生变化；请重新请求并检查新的差异。")

        backup_dir = self._backup_dir(create=True)
        assert backup_dir is not None
        backup_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
        new_bytes = preview.new_content.encode("utf-8")
        record: dict[str, Any] = {
            "version": 1,
            "id": backup_id,
            "created_ns": time.time_ns(),
            "path": preview.canonical_path,
            "before_exists": preview.existed,
            "before_base64": (
                base64.b64encode(preview.before_bytes).decode("ascii")
                if preview.before_bytes is not None
                else None
            ),
            "after_sha256": hashlib.sha256(new_bytes).hexdigest(),
            "undone": False,
        }
        backup_path = backup_dir / f"{backup_id}.json"
        self._atomic_write_json(backup_path, record)
        try:
            self._atomic_write_bytes(current_target, new_bytes)
        except WorkspaceError:
            try:
                backup_path.unlink()
            except OSError:
                pass
            raise
        return backup_id

    def _load_backups(self) -> list[tuple[Path, dict[str, Any]]]:
        backup_dir = self._backup_dir(create=False)
        if backup_dir is None:
            return []
        records: list[tuple[Path, dict[str, Any]]] = []
        try:
            for path in backup_dir.glob("*.json"):
                if self._is_reparse_point(path):
                    raise WorkspaceError(f"拒绝读取链接形式的撤销备份：{path.name}")
                if path.stat().st_size > WRITE_LIMIT_BYTES * 2:
                    raise WorkspaceError(f"撤销备份超过安全大小上限：{path.name}")
                data = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(data, dict)
                    or data.get("version") != 1
                    or not isinstance(data.get("id"), str)
                    or data.get("id") != path.stem
                    or type(data.get("created_ns")) is not int
                    or data.get("created_ns", -1) < 0
                    or not isinstance(data.get("path"), str)
                    or not data.get("path")
                    or not isinstance(data.get("undone"), bool)
                    or not isinstance(data.get("before_exists"), bool)
                    or not isinstance(data.get("after_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", data.get("after_sha256", "")) is None
                    or (
                        data.get("before_exists")
                        and not isinstance(data.get("before_base64"), str)
                    )
                    or (not data.get("before_exists") and data.get("before_base64") is not None)
                ):
                    raise WorkspaceError(f"撤销备份格式无效：{path.name}")
                if data.get("before_exists"):
                    try:
                        before_bytes = base64.b64decode(data["before_base64"], validate=True)
                        before_bytes.decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        raise WorkspaceError(f"撤销备份格式无效：{path.name}") from None
                    if len(before_bytes) > READ_LIMIT_BYTES:
                        raise WorkspaceError(f"撤销备份超过安全大小上限：{path.name}")
                records.append((path, data))
        except WorkspaceError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"无法读取撤销备份：{type(exc).__name__}") from None
        records.sort(key=lambda entry: (int(entry[1].get("created_ns", 0)), str(entry[1].get("id", ""))))
        return records

    def undo_last(
        self,
        *,
        approve_action: ApprovalCallback,
        plan_only: bool = False,
    ) -> str:
        if plan_only:
            return "计划模式：已在工具层拦截撤销；文件未修改。"

        active: tuple[Path, dict[str, Any]] | None = None
        for entry in reversed(self._load_backups()):
            if not entry[1].get("undone", False):
                active = entry
                break
        if active is None:
            return "没有可撤销的 Harness 文件写入。"

        backup_path, record = active
        relative_path = record.get("path")
        backup_id = record.get("id")
        if not isinstance(relative_path, str) or not isinstance(backup_id, str):
            raise WorkspaceError("撤销备份缺少有效路径或编号。")
        target = self.resolve(relative_path)
        current_bytes = self._read_snapshot(target)
        expected_hash = record.get("after_sha256")
        if current_bytes is None or not isinstance(expected_hash, str):
            raise WorkspaceError("撤销被阻止：当前文件与最新备份不匹配。")
        if hashlib.sha256(current_bytes).hexdigest() != expected_hash:
            raise WorkspaceError("撤销被阻止：文件在 Harness 写入后又发生变化。")

        before_exists = record.get("before_exists")
        old_base64 = record.get("before_base64")
        if not isinstance(before_exists, bool) or (before_exists and not isinstance(old_base64, str)):
            raise WorkspaceError("撤销备份缺少可恢复的文件内容。")
        try:
            restore_bytes = base64.b64decode(old_base64, validate=True) if before_exists else b""
            restore_content = restore_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise WorkspaceError("撤销备份内容无效。") from None
        current_content = current_bytes.decode("utf-8")
        diff = self._diff(
            relative_path,
            current_content,
            restore_content,
            before_exists=True,
            after_exists=before_exists,
        )

        request = ActionRequest(
            operation="undo",
            path=relative_path,
            diff=diff,
            backup_id=backup_id,
            source="command",
        )
        if not approve_action(request):
            return "用户拒绝本次撤销；文件未修改。"

        current_target = self.resolve(relative_path)
        latest_bytes = self._read_snapshot(current_target)
        if current_target != target or latest_bytes != current_bytes:
            raise WorkspaceError("审批后文件发生变化；为防止覆盖新内容，本次撤销已取消。")
        if before_exists:
            self._atomic_write_bytes(target, restore_bytes)
        else:
            try:
                target.unlink()
            except OSError as exc:
                raise WorkspaceError(f"撤销时删除新文件失败：{type(exc).__name__}") from None
        record["undone"] = True
        record["undone_ns"] = time.time_ns()
        self._atomic_write_json(backup_path, record)
        return f"已撤销写入并恢复备份 {backup_id}：{relative_path}"


def _validate_arguments(call: ToolCall, expected: set[str]) -> None:
    if not isinstance(call.arguments, dict):
        raise WorkspaceError("工具参数必须是 JSON 对象。")
    actual = set(call.arguments)
    if actual != expected:
        raise WorkspaceError("工具参数缺失或包含未授权字段。")


def _string_argument(call: ToolCall, name: str) -> str:
    value = call.arguments.get(name)
    if not isinstance(value, str):
        raise WorkspaceError(f"工具参数 {name} 必须是字符串。")
    return value


def run_tool(
    workspace: Workspace,
    call: ToolCall,
    *,
    approve_action: ApprovalCallback,
    plan_only: bool = False,
) -> str:
    """Execute one validated tool; model output never grants local permission."""

    if call.name == "read_file":
        _validate_arguments(call, {"path"})
        path = _string_argument(call, "path")
        target = workspace.resolve(path)
        if not approve_action(ActionRequest("read", path, source="tool")):
            return "用户拒绝读取该文件；文件内容未提供给模型。"
        # Re-resolve after the approval to catch a link changed while the prompt was open.
        if workspace.resolve(path) != target:
            raise WorkspaceError("审批后文件路径发生变化；读取已取消。")
        data = workspace._read_snapshot(target)
        if data is None:
            raise WorkspaceError(f"文件不存在或不是普通文件：{path}")
        return data.decode("utf-8-sig")

    if call.name == "write_file":
        _validate_arguments(call, {"path", "content"})
        path = _string_argument(call, "path")
        content = _string_argument(call, "content")
        workspace.resolve(path)
        if plan_only:
            return "计划模式：已在工具层拦截写入；文件未修改。"
        preview = workspace.preview_write(path, content)
        if not preview.changed:
            return "文件内容已经相同，没有写入，也未创建撤销备份。"
        request = ActionRequest(
            operation="write",
            path=path,
            diff=preview.diff,
            source="tool",
        )
        if not approve_action(request):
            return "用户拒绝本次写入；文件未修改，也未创建撤销备份。"
        backup_id = workspace.commit_write(preview)
        if backup_id is None:
            return "文件内容已经相同，没有写入。"
        return f"已写入工作区文件并创建撤销备份 {backup_id}：{path}"

    return f"未授权的工具名称：{call.name}；没有执行任何操作。"
