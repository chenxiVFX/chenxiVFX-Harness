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
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from .providers import ProviderCancelled, ToolCall

READ_LIMIT_BYTES = 64 * 1024
WRITE_LIMIT_BYTES = 64 * 1024
PATCH_FRAGMENT_LIMIT_BYTES = 16 * 1024
PATCH_DIFF_LIMIT_BYTES = 32 * 1024
PATCH_APPROVAL_DISPLAY_LIMIT = 32 * 1024
DIRECTORY_ENTRY_LIMIT = 100
DIRECTORY_SCAN_LIMIT = 1000
PATH_COMPLETION_LIMIT = 50
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
            "name": "list_directory",
            "description": "List names and types in one workspace directory; the user must approve every listing. File contents are not read.",
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
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": (
                "Apply one exact UTF-8 text replacement in one existing workspace file. "
                "Provide the SHA-256 from the most recent approved read_file result. "
                "The old_text must be non-empty and occur exactly once; no fuzzy or global replacement is performed. "
                "The user must approve the exact diff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "expected_sha256", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
]


class WorkspaceError(RuntimeError):
    pass


def _safe_terminal_display_length(text: str) -> int:
    """Measure the escaped rendering used by the CLI approval panel."""
    visible_length = 0
    for char in text:
        if char == "\n":
            visible_length += 1
            continue
        if unicodedata.category(char).startswith("C"):
            codepoint = ord(char)
            visible_length += 4 if codepoint <= 0xFF else 6 if codepoint <= 0xFFFF else 10
        else:
            visible_length += 1
    return visible_length


@dataclass(frozen=True)
class ActionRequest:
    operation: str
    path: str
    diff: str = ""
    backup_id: str | None = None
    source: str = "tool"
    file_existed: bool | None = None


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
    tasks: tuple[str, ...],
    approve_action: ApprovalCallback,
    *,
    read_scopes: tuple[tuple[str, str], ...] = (),
    timeout_seconds: float = 60.0,
) -> bool:
    """Approve exact task text and optional exact child-only read scopes."""
    if not tasks or len(tasks) > 3 or any(not isinstance(task, str) or len(task) > 240 for task in tasks):
        raise WorkspaceError("子代理计划超出本地任务范围上限。")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 5 <= timeout_seconds <= 120
    ):
        raise WorkspaceError("子代理运行时限必须在 5 到 120 秒之间。")
    if len(read_scopes) > 8 or any(
        not isinstance(path, str)
        or not path
        or len(path) > 1024
        or operation not in {"read_file", "list_directory"}
        for path, operation in read_scopes
    ):
        raise WorkspaceError("子代理只读范围格式无效或超出上限。")
    preview = "\n".join(
        f"子任务 {index}: {task}\n"
        "输入范围：仅此任务文字和下列明确批准的数据；不包含主会话或父代理工具结果。"
        for index, task in enumerate(tasks, 1)
    )
    if read_scopes:
        preview += (
            "\n子代理只读范围（子任务专属，不延伸到子目录或其他路径）；"
            "经批准的文件内容或目录清单会发送给当前 provider：\n"
        ) + "\n".join(
            f"- {operation}: {path}" for path, operation in read_scopes
        )
    else:
        preview += "\n子代理不获准读取任何工作区文件。"
    preview += (
        f"\n每项最多两轮模型请求；运行总时限 {timeout_seconds:g} 秒，"
        "从首个模型请求开始计时，审批和执行前排队不计入；"
        "无写入、shell 或递归委派权限；结果是未可信文本。"
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
        try:
            root_info = self.root.stat()
            self._root_identity = (root_info.st_dev, root_info.st_ino)
        except OSError:
            raise WorkspaceError("工作区路径不可用。") from None
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

    def _resolve_path(self, relative_path: str, *, allow_root: bool) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise WorkspaceError("文件路径不能为空。")
        if len(relative_path) > 4096:
            raise WorkspaceError("路径超过本地长度上限。")
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
        if ".." in re.split(r"[\\/]+", relative_path):
            raise WorkspaceError("路径不能包含父目录遍历片段。")

        try:
            candidate = (self.root / relative_path).resolve(strict=False)
            candidate.relative_to(self.root)
        except (OSError, RuntimeError):
            raise WorkspaceError("路径解析失败或包含循环链接。") from None
        except ValueError:
            raise WorkspaceError("拒绝访问工作区之外的路径。") from None

        if candidate == self.root and not allow_root:
            raise WorkspaceError("路径必须指向工作区中的文件。")
        self._reject_internal_state_path(candidate)
        self._reject_sensitive_path(candidate, relative_path)
        return candidate

    def resolve(self, relative_path: str) -> Path:
        return self._resolve_path(relative_path, allow_root=False)

    def resolve_directory(self, relative_path: str) -> Path:
        target = self._resolve_path(relative_path, allow_root=True)
        current = self.root
        for part in Path(relative_path).parts:
            if part in {".", ""}:
                continue
            current = current / part
            if self._is_reparse_point(current):
                raise WorkspaceError("目录列表不跟随 symlink 或 junction。")
        if not target.is_dir():
            raise WorkspaceError(f"目录不存在或不是目录：{relative_path}")
        return target

    def suggest_path_entries(
        self,
        relative_directory: str,
        prefix: str,
        *,
        limit: int = PATH_COMPLETION_LIMIT,
    ) -> list[tuple[str, bool]]:
        """Return bounded, safe child names without opening file contents.

        Each item is ``(name, is_directory)``. Suggestions are conveniences only;
        reading a selected file still goes through the existing per-read approval.
        """
        if (
            not isinstance(prefix, str)
            or len(prefix) > 255
            or any(unicodedata.category(char).startswith("C") for char in prefix)
        ):
            return []
        if type(limit) is not int or not 1 <= limit <= PATH_COMPLETION_LIMIT:
            raise ValueError("path suggestion limit is outside the local bound")
        try:
            target = self.resolve_directory(relative_directory or ".")
            suggestions: list[tuple[str, bool]] = []
            scanned = 0
            with os.scandir(target) as entries:
                for entry in entries:
                    if scanned >= DIRECTORY_SCAN_LIMIT:
                        break
                    scanned += 1
                    name = entry.name
                    if (
                        name in {".", ".."}
                        or not name.casefold().startswith(prefix.casefold())
                        or any(char in name for char in "/\\:{}")
                        or name.rstrip(" .") != name
                        or any(unicodedata.category(char).startswith("C") for char in name)
                    ):
                        continue
                    candidate_path = Path(entry.path)
                    try:
                        if self._is_reparse_point(candidate_path):
                            continue
                        info = candidate_path.stat(follow_symlinks=False)
                    except (OSError, WorkspaceError):
                        continue
                    is_directory = stat.S_ISDIR(info.st_mode)
                    if not is_directory:
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                            continue
                        if info.st_size > READ_LIMIT_BYTES:
                            continue
                    relative = (PurePosixPath(relative_directory or ".") / name).as_posix()
                    try:
                        if is_directory:
                            self.resolve_directory(relative)
                        else:
                            self.resolve(relative)
                    except WorkspaceError:
                        continue
                    suggestions.append((name, is_directory))
        except (OSError, WorkspaceError):
            return []
        suggestions.sort(key=lambda item: (item[0].casefold(), item[0]))
        return suggestions[:limit]

    def _relative_parts(self, candidate: Path) -> tuple[str, ...]:
        try:
            return tuple(part.casefold() for part in candidate.relative_to(self.root).parts)
        except ValueError:
            raise WorkspaceError("拒绝访问工作区之外的路径。") from None

    def _reject_internal_state_path(self, candidate: Path) -> None:
        parts = self._relative_parts(candidate)
        if _CONTROL_DIR.casefold() in parts:
            raise WorkspaceError("Harness 内部状态目录不能由模型文件工具读取或修改。")

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
        if self._supports_dir_fd_operations():
            try:
                parent_fd, name = self._open_parent_directory_fd(target)
            except FileNotFoundError:
                return None
            except OSError:
                raise WorkspaceError("拒绝通过链接或工作区外路径读取文件。") from None
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                try:
                    descriptor = os.open(name, flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    return None
                except OSError:
                    raise WorkspaceError("目标不是普通文件，或文件路径在访问期间发生变化。") from None
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode):
                        raise WorkspaceError("目标路径不是普通文件。")
                    if getattr(info, "st_nlink", 1) > 1:
                        raise WorkspaceError("拒绝访问含多个硬链接的文件，避免越过工作区边界。")
                    if info.st_size > READ_LIMIT_BYTES:
                        raise WorkspaceError("文件超过 64 KiB 安全上限。")
                    chunks: list[bytes] = []
                    remaining = READ_LIMIT_BYTES + 1
                    while remaining:
                        chunk = os.read(descriptor, min(8192, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    data = b"".join(chunks)
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent_fd)
            if len(data) > READ_LIMIT_BYTES:
                raise WorkspaceError("文件超过 64 KiB 安全上限。")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise WorkspaceError("当前仅支持 UTF-8 文本文件。") from None
            return data

        if not target.exists():
            return None
        try:
            self._assert_no_reparse_points(target)
            if not target.is_file():
                raise WorkspaceError("目标路径不是普通文件。")
            info = target.stat()
            if getattr(info, "st_nlink", 1) > 1:
                raise WorkspaceError("拒绝访问含多个硬链接的文件，避免越过工作区边界。")
            if info.st_size > READ_LIMIT_BYTES:
                raise WorkspaceError("文件超过 64 KiB 安全上限。")
            data = target.read_bytes()
            self._assert_no_reparse_points(target)
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

        target = self._resolve_write_target(relative_path)
        if not target.parent.is_dir():
            raise WorkspaceError("目标文件的父目录必须已存在。")
        before_bytes = self._read_snapshot(target)
        return self._build_write_preview(relative_path, target, content, before_bytes)

    def preview_patch(
        self,
        relative_path: str,
        *,
        expected_sha256: str,
        old_text: str,
        new_text: str,
    ) -> tuple[WritePreview, str]:
        """Build a single exact replacement preview against an explicit byte version."""
        if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
            raise WorkspaceError("补丁必须提供 64 位十六进制原文件 SHA-256。")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise WorkspaceError("补丁文本必须是 UTF-8 字符串。")
        if old_text == "":
            raise WorkspaceError("补丁旧文本不能为空。")
        self._validate_patch_fragment(old_text, "旧文本")
        self._validate_patch_fragment(new_text, "新文本")

        target = self._resolve_write_target(relative_path)
        if not target.parent.is_dir():
            raise WorkspaceError("目标文件的父目录必须已存在。")
        before_bytes = self._read_snapshot(target)
        if before_bytes is None:
            raise WorkspaceError("精确补丁只允许修改已存在的普通文件。")
        actual_sha256 = hashlib.sha256(before_bytes).hexdigest()
        if actual_sha256 != expected_sha256.casefold():
            raise WorkspaceError("原文件 SHA-256 与补丁预期版本不符；请重新读取后再请求补丁。")

        try:
            before_text = before_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("当前仅支持 UTF-8 文本文件。") from None
        self._validate_writable_text(before_text)
        occurrences = before_text.count(old_text)
        if occurrences == 0:
            raise WorkspaceError("旧文本在当前文件中没有精确匹配；文件未修改。")
        if occurrences != 1:
            raise WorkspaceError("旧文本出现多次；为避免误改，本次补丁已拒绝。")
        updated_text = before_text.replace(old_text, new_text, 1)
        preview = self._build_write_preview(relative_path, target, updated_text, before_bytes)
        try:
            diff_size = len(preview.diff.encode("utf-8"))
        except UnicodeEncodeError:
            raise WorkspaceError("补丁差异不是有效 UTF-8 文本。") from None
        if diff_size > PATCH_DIFF_LIMIT_BYTES:
            raise WorkspaceError("补丁差异超过 32 KiB 审批显示上限。")
        return preview, actual_sha256

    @staticmethod
    def _validate_patch_fragment(content: str, label: str) -> None:
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            raise WorkspaceError(f"补丁{label}不是有效 UTF-8 文本。") from None
        if size > PATCH_FRAGMENT_LIMIT_BYTES:
            raise WorkspaceError(f"补丁{label}超过 16 KiB 上限。")
        if any(
            (unicodedata.category(char) == "Cc" and char not in "\t\r\n")
            or unicodedata.category(char) in {"Cf", "Cs"}
            for char in content
        ):
            raise WorkspaceError(f"补丁{label}含终端控制或格式字符。")

    def _build_write_preview(
        self,
        relative_path: str,
        target: Path,
        content: str,
        before_bytes: bytes | None,
    ) -> WritePreview:
        try:
            new_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkspaceError("写入内容不是有效 UTF-8 文本。") from None
        if len(new_bytes) > WRITE_LIMIT_BYTES:
            raise WorkspaceError("单次写入超过 64 KiB 上限。")
        existed = before_bytes is not None
        before_content = before_bytes.decode("utf-8") if before_bytes is not None else ""
        if existed and content == "" and before_bytes != b"":
            raise WorkspaceError("拒绝清空已有文件；当前没有删除文件工具。")
        self._validate_writable_text(content)
        if existed:
            self._validate_writable_text(before_content)
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

    @staticmethod
    def _validate_writable_text(content: str) -> None:
        if any(
            unicodedata.category(char) == "Cc" and char not in "\t\r\n"
            for char in content
        ):
            raise WorkspaceError("拒绝写入包含 NUL 或控制字符的二进制内容。")

    def _resolve_write_target(self, relative_path: str) -> Path:
        target = self.resolve(relative_path)
        current = self.root
        for part in Path(relative_path).parts:
            if part in {"", "."}:
                continue
            current = current / part
            if self._is_reparse_point(current):
                raise WorkspaceError("拒绝通过 symlink/junction 路径写入文件。")
        return target

    def internal_storage_dir(self, name: str, *, create: bool) -> Path | None:
        if name not in {_BACKUP_DIR, "sessions", "config", "plans"}:
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
                    raise WorkspaceError(f"无法创建 Harness 内部目录：{type(exc).__name__}") from None
            if self._is_reparse_point(directory):
                raise WorkspaceError("拒绝使用 junction/symlink 作为 Harness 内部目录。")
            try:
                resolved = directory.resolve(strict=True)
                resolved.relative_to(self.root)
            except (OSError, RuntimeError, ValueError):
                raise WorkspaceError("Harness 内部目录必须位于工作区内且不能链接到外部。") from None
            if not directory.is_dir():
                raise WorkspaceError("Harness 内部路径不是目录。")
        return storage_dir

    def _backup_dir(self, *, create: bool) -> Path | None:
        return self.internal_storage_dir(_BACKUP_DIR, create=create)

    @staticmethod
    def _supports_dir_fd_operations() -> bool:
        return (
            os.name != "nt"
            and hasattr(os, "O_DIRECTORY")
            and hasattr(os, "O_NOFOLLOW")
            and os.open in os.supports_dir_fd
            and os.rename in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd
        )

    def _target_parts(self, target: Path) -> tuple[str, ...]:
        try:
            relative = target.relative_to(self.root)
        except ValueError:
            raise WorkspaceError("拒绝访问工作区之外的路径。") from None
        parts = relative.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise WorkspaceError("文件路径无效。")
        return tuple(parts)

    def _open_parent_directory_fd(self, target: Path) -> tuple[int, str]:
        """Open the parent by directory handles without following symlink components."""
        parts = self._target_parts(target)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(self.root, flags)
        try:
            root_info = os.fstat(directory_fd)
            if (root_info.st_dev, root_info.st_ino) != self._root_identity:
                raise WorkspaceError("工作区根目录在运行期间发生变化；操作已取消。")
            for component in parts[:-1]:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            return directory_fd, parts[-1]
        except FileNotFoundError:
            os.close(directory_fd)
            raise
        except OSError:
            os.close(directory_fd)
            raise WorkspaceError("拒绝通过链接或工作区外路径访问文件。") from None
        except BaseException:
            os.close(directory_fd)
            raise

    def _assert_no_reparse_points(self, target: Path) -> None:
        self._target_parts(target)
        current = self.root
        if self._is_reparse_point(current):
            raise WorkspaceError("工作区路径包含 symlink/junction；操作已取消。")
        for component in target.relative_to(self.root).parts:
            current = current / component
            if self._is_reparse_point(current):
                raise WorkspaceError("路径包含 symlink/junction；操作已取消。")

    def _atomic_write_bytes(self, target: Path, data: bytes) -> None:
        if not target.parent.is_dir():
            raise WorkspaceError("目标文件的父目录必须已存在。")
        if self._supports_dir_fd_operations():
            try:
                parent_fd, name = self._open_parent_directory_fd(target)
            except FileNotFoundError:
                raise WorkspaceError("目标目录在写入期间发生变化。") from None
            temporary_name = f".harness-tmp-{uuid.uuid4().hex}.tmp"
            descriptor: int | None = None
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                flags |= getattr(os, "O_CLOEXEC", 0)
                descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = None
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                # os.rename is the POSIX replace operation and explicitly
                # supports directory-relative descriptors on supported builds.
                os.rename(
                    temporary_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_name = ""
            except WorkspaceError:
                raise
            except OSError as exc:
                raise WorkspaceError(f"原子写入失败：{type(exc).__name__}") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if temporary_name:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                os.close(parent_fd)
            return

        self._assert_no_reparse_points(target)
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".harness-tmp-", suffix=".tmp", dir=target.parent
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_no_reparse_points(target)
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

    def _atomic_write_json(self, target: Path, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._atomic_write_bytes(target, encoded)

    def _unlink_workspace_file(self, target: Path) -> None:
        if self._supports_dir_fd_operations():
            try:
                parent_fd, name = self._open_parent_directory_fd(target)
            except FileNotFoundError:
                raise WorkspaceError("撤销目标路径在操作期间发生变化。") from None
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError as exc:
                raise WorkspaceError(f"撤销时删除新文件失败：{type(exc).__name__}") from None
            finally:
                os.close(parent_fd)
            return
        self._assert_no_reparse_points(target)
        try:
            target.unlink()
        except OSError as exc:
            raise WorkspaceError(f"撤销时删除新文件失败：{type(exc).__name__}") from None

    def commit_write(
        self,
        preview: WritePreview,
        *,
        cancellation_event: Any | None = None,
    ) -> str | None:
        if not preview.changed:
            return None

        current_target = self._revalidate_write_preview(preview)

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
            _raise_if_cancelled(cancellation_event)
            current_target = self._revalidate_write_preview(preview)
            _raise_if_cancelled(cancellation_event)
            self._atomic_write_bytes(current_target, new_bytes)
        except (WorkspaceError, ProviderCancelled):
            try:
                self._unlink_workspace_file(backup_path)
            except (OSError, WorkspaceError):
                pass
            raise
        return backup_id

    def _revalidate_write_preview(self, preview: WritePreview) -> Path:
        current_target = self._resolve_write_target(preview.path)
        if current_target != preview.target:
            raise WorkspaceError("审批后目标路径发生变化；为防止写错文件，本次操作已取消。")
        current_bytes = self._read_snapshot(current_target)
        if current_bytes != preview.before_bytes:
            raise WorkspaceError("审批预览后文件发生变化；请重新请求并检查新的差异。")
        return current_target

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
            self._unlink_workspace_file(target)
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


def build_tool_schemas(test_profile_ids: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Return fixed file tools plus an ID-only runner for configured user profiles."""
    schemas = list(TOOL_SCHEMAS)
    if test_profile_ids:
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": "run_test_profile",
                    "description": (
                        "Run one exact user-owned Python test profile only when the user explicitly asks to run tests. "
                        "The user configured and validated its external Python interpreter; never request or invent a path. "
                        "The local harness asks for per-run approval and separately asks before sharing redacted output. "
                        "This is not a shell and accepts no command, path, or arguments."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "profile_id": {
                                "type": "string",
                                "enum": list(test_profile_ids),
                            }
                        },
                        "required": ["profile_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    return schemas


def _raise_if_cancelled(cancellation_event: Any | None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise ProviderCancelled("tool operation cancelled")


def run_tool(
    workspace: Workspace,
    call: ToolCall,
    *,
    approve_action: ApprovalCallback,
    plan_only: bool = False,
    cancellation_event: Any | None = None,
    test_profile_store: Any | None = None,
    emit_output: Callable[[str], None] | None = None,
) -> str:
    """Execute one validated tool; model output never grants local permission."""

    if call.name == "read_file":
        _raise_if_cancelled(cancellation_event)
        _validate_arguments(call, {"path"})
        path = _string_argument(call, "path")
        target = workspace.resolve(path)
        if not approve_action(ActionRequest("read", path, source="tool")):
            return "用户拒绝读取该文件；文件内容未提供给模型。"
        _raise_if_cancelled(cancellation_event)
        # Re-resolve after the approval to catch a link changed while the prompt was open.
        if workspace.resolve(path) != target:
            raise WorkspaceError("审批后文件路径发生变化；读取已取消。")
        data = workspace._read_snapshot(target)
        if data is None:
            raise WorkspaceError(f"文件不存在或不是普通文件：{path}")
        digest = hashlib.sha256(data).hexdigest()
        return f"SHA256={digest}\n{data.decode('utf-8-sig')}"

    if call.name == "list_directory":
        _raise_if_cancelled(cancellation_event)
        _validate_arguments(call, {"path"})
        path = _string_argument(call, "path")
        target = workspace.resolve_directory(path)
        if not approve_action(ActionRequest("list", path, source="tool")):
            return "用户拒绝列出该目录；目录项未提供给模型。"
        _raise_if_cancelled(cancellation_event)
        if workspace.resolve_directory(path) != target:
            raise WorkspaceError("审批后目录路径发生变化；列表读取已取消。")
        entries: list[dict[str, str]] = []
        scanned = 0
        scan_truncated = False
        try:
            with os.scandir(target) as iterator:
                for entry in iterator:
                    _raise_if_cancelled(cancellation_event)
                    if scanned >= DIRECTORY_SCAN_LIMIT:
                        scan_truncated = True
                        break
                    scanned += 1
                    name = entry.name
                    if name in {".", ".."} or any(
                        unicodedata.category(char).startswith("C") for char in name
                    ):
                        continue
                    entry_path = Path(entry.path)
                    try:
                        if Workspace._is_reparse_point(entry_path):
                            continue
                        relative = entry_path.relative_to(workspace.root).as_posix()
                        workspace.resolve(relative)
                    except (WorkspaceError, ValueError):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        kind = "directory"
                    elif entry.is_file(follow_symlinks=False):
                        kind = "file"
                    else:
                        continue
                    entries.append({"name": name, "type": kind})
                    _raise_if_cancelled(cancellation_event)
        except OSError as exc:
            raise WorkspaceError(f"无法列出目录：{type(exc).__name__}") from None
        _raise_if_cancelled(cancellation_event)
        entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        truncated = len(entries) > DIRECTORY_ENTRY_LIMIT or scan_truncated
        result = {
            "path": "." if target == workspace.root else target.relative_to(workspace.root).as_posix(),
            "entries": entries[:DIRECTORY_ENTRY_LIMIT],
            "truncated": truncated,
        }
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    if call.name == "write_file":
        _raise_if_cancelled(cancellation_event)
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
            file_existed=preview.existed,
        )
        if not approve_action(request):
            return "用户拒绝本次写入；文件未修改，也未创建撤销备份。"
        _raise_if_cancelled(cancellation_event)
        backup_id = workspace.commit_write(
            preview,
            cancellation_event=cancellation_event,
        )
        if backup_id is None:
            return "文件内容已经相同，没有写入。"
        return f"已写入工作区文件并创建撤销备份 {backup_id}：{path}"

    if call.name == "patch_file":
        _raise_if_cancelled(cancellation_event)
        _validate_arguments(call, {"path", "expected_sha256", "old_text", "new_text"})
        path = _string_argument(call, "path")
        expected_sha256 = _string_argument(call, "expected_sha256")
        old_text = _string_argument(call, "old_text")
        new_text = _string_argument(call, "new_text")
        workspace.resolve(path)
        if plan_only:
            return "计划模式：已在工具层拦截补丁；文件未修改。"
        preview, actual_sha256 = workspace.preview_patch(
            path,
            expected_sha256=expected_sha256,
            old_text=old_text,
            new_text=new_text,
        )
        if not preview.changed:
            return "补丁结果与当前文件相同，没有写入，也未创建撤销备份。"
        approval_diff = (
            f"目标：{preview.canonical_path}\n"
            f"预期原文件 SHA-256：{actual_sha256}\n"
            f"{preview.diff}"
        )
        if _safe_terminal_display_length(approval_diff) > PATCH_APPROVAL_DISPLAY_LIMIT:
            raise WorkspaceError("补丁审批预览超过终端显示上限；请缩小本次修改范围。")
        request = ActionRequest(
            operation="patch",
            path=preview.canonical_path,
            diff=approval_diff,
            source="tool",
            file_existed=True,
        )
        if not approve_action(request):
            return "用户拒绝本次补丁；文件未修改，也未创建撤销备份。"
        _raise_if_cancelled(cancellation_event)
        backup_id = workspace.commit_write(preview, cancellation_event=cancellation_event)
        if backup_id is None:
            return "补丁结果与当前文件相同，没有写入。"
        return f"已应用精确补丁并创建撤销备份 {backup_id}：{preview.canonical_path}"

    if call.name == "run_test_profile":
        _raise_if_cancelled(cancellation_event)
        _validate_arguments(call, {"profile_id"})
        profile_id = _string_argument(call, "profile_id")
        if plan_only:
            return "计划模式：测试进程已在工具层拦截；没有启动。"
        if test_profile_store is None:
            return "没有可运行的用户测试配置；未启动测试进程。"
        from .test_runner import run_test_profile_tool

        return run_test_profile_tool(
            workspace,
            test_profile_store,
            profile_id,
            approve_action=approve_action,
            cancellation_event=cancellation_event,
            emit_output=emit_output,
        )

    return f"未授权的工具名称：{call.name}；没有执行任何操作。"
