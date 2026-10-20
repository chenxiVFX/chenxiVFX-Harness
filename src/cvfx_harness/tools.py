from __future__ import annotations

import base64
import binascii
import difflib
import errno
import fnmatch
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
from .questions import (
    QuestionnaireRequest,
    QuestionnaireResult,
    QuestionRequestError,
    UserQuestionAnswer,
    parse_questionnaire,
    questionnaire_result_json,
    validate_question_answers,
)

READ_LIMIT_BYTES = 64 * 1024
WRITE_LIMIT_BYTES = 64 * 1024
PATCH_FRAGMENT_LIMIT_BYTES = 16 * 1024
PATCH_DIFF_LIMIT_BYTES = 32 * 1024
PATCH_APPROVAL_DISPLAY_LIMIT = 32 * 1024
DIRECTORY_ENTRY_LIMIT = 100
DIRECTORY_SCAN_LIMIT = 1000
PATH_COMPLETION_LIMIT = 50
SEARCH_FILE_LIMIT_BYTES = READ_LIMIT_BYTES
SEARCH_TOTAL_BYTES_LIMIT = 8 * 1024 * 1024
SEARCH_ENTRY_LIMIT = 5000
SEARCH_FILE_COUNT_LIMIT = 2000
SEARCH_DIRECTORY_COUNT_LIMIT = 500
SEARCH_DEPTH_LIMIT = 64
SEARCH_MATCH_LIMIT = 100
SEARCH_MATCH_SNIPPET_CHARS = 240
SEARCH_OUTPUT_LIMIT_BYTES = 32 * 1024
SEARCH_TIME_LIMIT_SECONDS = 5.0
SEARCH_QUERY_MAX_CHARS = 200
SEARCH_GLOB_MAX_CHARS = 256
SEARCH_GITIGNORE_RULE_LIMIT = 256
SEARCH_GITIGNORE_PATTERN_MAX_CHARS = 1024
CHANGE_SET_MIN_FILES = 2
CHANGE_SET_MAX_FILES = 5
CHANGE_SET_PAYLOAD_LIMIT_BYTES = 96 * 1024
CHANGE_SET_DIFF_LIMIT_BYTES = 128 * 1024
CHANGE_SET_BACKUP_LIMIT_BYTES = 256 * 1024
CHANGE_SET_JOURNAL_LIMIT_BYTES = 16 * 1024
CHANGE_SET_LOG_LIMIT = 128
CHANGE_SET_TERMINAL_LOG_RETENTION = 32
CHANGE_SET_JOURNAL_SCAN_LIMIT = CHANGE_SET_LOG_LIMIT + CHANGE_SET_TERMINAL_LOG_RETENTION
CHANGE_SET_BACKUP_SCAN_LIMIT = 2048
CHANGE_SET_EVIDENCE_DISPLAY_LIMIT = 24
_CHANGE_SET_DIR = "change-sets"
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
            "name": "ask_user",
            "description": (
                "Ask the user one to three short clarifying questions. Each question may include a few suggested answers, "
                "and the user may always write a free-form answer. This collects untrusted user data only; it never "
                "approves file access, edits, tests, commands, or other actions. Do not ask for passwords, API keys, "
                "tokens, secrets, or credentials; direct the user to /配置 or the supported environment settings instead. "
                "A cancelled request has no answers and must not be guessed or treated as consent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "maxLength": 32},
                                "question": {"type": "string", "maxLength": 240},
                                "suggestions": {
                                    "type": "array",
                                    "maxItems": 4,
                                    "items": {"type": "string", "maxLength": 96},
                                },
                            },
                            "required": ["id", "question"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["questions"],
                "additionalProperties": False,
            },
        },
    },
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
            "name": "search_text",
            "description": (
                "Search allowed UTF-8 text files in a user-approved workspace-relative directory. "
                "The query is a case-sensitive literal substring, never a regular expression. "
                "Honors .gitignore rules and returns bounded, redacted untrusted snippets with relative paths and line numbers. "
                "The user must approve each search scope before file contents are scanned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Optional workspace-relative directory scope; defaults to the workspace root.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional relative filename/path glob filter within the selected directory.",
                    },
                },
                "required": ["query"],
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
    {
        "type": "function",
        "function": {
            "name": "apply_change_set",
            "description": (
                "Apply one explicitly reviewed set of 2 to 5 exact replacements to existing UTF-8 workspace files. "
                "Each file needs the SHA-256 from its latest approved read_file result, a non-empty old_text that occurs "
                "exactly once, and new_text. The complete set is preflighted and shown together for one explicit approval; "
                "any stale file aborts the set. No create, delete, rename, directory creation, shell, or test execution. "
                "A multi-file set is not a cross-file atomic transaction."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "changes": {
                        "type": "array",
                        "minItems": CHANGE_SET_MIN_FILES,
                        "maxItems": CHANGE_SET_MAX_FILES,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["path", "expected_sha256", "old_text", "new_text"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["changes"],
                "additionalProperties": False,
            },
        },
    },
]


class WorkspaceError(RuntimeError):
    pass


class WorkspaceConflictError(WorkspaceError):
    def __init__(
        self,
        message: str,
        *,
        current_bytes: bytes | None,
        current_mode: int | None = None,
        current_acl_token: bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.current_bytes = current_bytes
        self.current_mode = current_mode
        self.current_acl_token = current_acl_token


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
    trusted_file_sha256: str | None = None


@dataclass(frozen=True)
class WritePreview:
    path: str
    canonical_path: str
    target: Path
    existed: bool
    before_bytes: bytes | None
    before_mode: int | None
    before_token: tuple[int, ...] | None
    before_acl_token: bytes | None
    before_content: str
    new_content: str
    diff: str
    changed: bool


@dataclass(frozen=True)
class ChangeSetPreview:
    changes: tuple[WritePreview, ...]
    file_sha256: tuple[str, ...]
    diff: str
    digest: str
    payload_bytes: int
    backup_bytes: int


@dataclass(frozen=True)
class ChangeSetFileStatus:
    path: str
    status: str
    backup_id: str
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True)
class ChangeSetRecoveryItem:
    path: str
    status: str
    backup_id: str
    before_sha256: str
    after_sha256: str
    current_bytes: bytes | None = None
    current_info: os.stat_result | None = None
    current_acl_token: bytes | None = None
    restore_bytes: bytes | None = None
    backup_record: dict[str, Any] | None = None


@dataclass(frozen=True)
class ChangeSetRecoveryPreview:
    change_set_id: str
    record: dict[str, Any]
    items: tuple[ChangeSetRecoveryItem, ...]
    diff: str
    digest: str


@dataclass(frozen=True)
class _GitIgnoreRule:
    base_parts: tuple[str, ...]
    pattern: str
    negated: bool
    directory_only: bool
    basename_pattern: bool


def _translate_gitignore_pattern(pattern: str) -> str | None:
    translated: list[str] = []
    literal_glob_chars = {"*": "[*]", "?": "[?]", "[": "[[]", "]": "[]]"}
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 1
            if index >= len(pattern):
                return None
            escaped = pattern[index]
            translated.append(literal_glob_chars.get(escaped, escaped))
        else:
            translated.append(char)
        index += 1
    return "".join(translated)


def _parse_gitignore(text: str, base_parts: tuple[str, ...]) -> list[_GitIgnoreRule] | None:
    rules: list[_GitIgnoreRule] = []
    for raw_line in text.splitlines():
        line = raw_line
        if any(
            unicodedata.category(char).startswith("C")
            or unicodedata.category(char) in {"Zl", "Zp"}
            for char in line
        ):
            return None
        while line.endswith(" "):
            escaped = 0
            cursor = len(line) - 2
            while cursor >= 0 and line[cursor] == "\\":
                escaped += 1
                cursor -= 1
            if escaped % 2:
                break
            line = line[:-1]
        if not line:
            continue
        if len(line) > SEARCH_GITIGNORE_PATTERN_MAX_CHARS:
            return None
        if line.startswith("\\#") or line.startswith("\\!"):
            line = line[1:]
        elif line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        if not line:
            continue
        if len(rules) >= SEARCH_GITIGNORE_RULE_LIMIT:
            return None
        if len(line.split("/")) > SEARCH_DEPTH_LIMIT:
            return None
        anchored = line.startswith("/")
        if anchored:
            line = line.lstrip("/")
        directory_only = line.endswith("/")
        if directory_only:
            line = line.rstrip("/")
        if not line:
            continue
        translated = _translate_gitignore_pattern(line)
        if translated is None:
            return None
        rules.append(
            _GitIgnoreRule(
                base_parts=base_parts,
                pattern=translated,
                negated=negated,
                directory_only=directory_only,
                basename_pattern="/" not in line and not anchored,
            )
        )
    return rules


def _matches_gitignore_rule(
    rule: _GitIgnoreRule,
    relative_parts: tuple[str, ...],
    *,
    is_directory: bool,
) -> bool:
    if len(relative_parts) <= len(rule.base_parts):
        return False
    if relative_parts[: len(rule.base_parts)] != rule.base_parts:
        return False
    if rule.directory_only and not is_directory:
        return False
    suffix = relative_parts[len(rule.base_parts) :]
    if rule.basename_pattern:
        return any(fnmatch.fnmatchcase(part, rule.pattern) for part in suffix)
    return _match_glob_parts(suffix, tuple(rule.pattern.split("/")))


def _match_glob_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    """Match slash-separated globs without letting * cross a path separator."""
    states: dict[tuple[int, int], bool] = {}

    def match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in states:
            return states[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and match(path_index + 1, pattern_index + 1)
            )
        states[key] = result
        return result

    return match(0, 0)


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
            requested_root = Path(root).expanduser()
            if self._is_reparse_point(requested_root):
                raise WorkspaceError("工作区根目录不能是 symlink/junction。")
            self.root = requested_root.resolve(strict=True)
        except WorkspaceError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
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
        self._assert_root_identity()
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
        data, _info = self._read_snapshot_state(target)
        return data

    def _read_snapshot_state(
        self,
        target: Path,
        *,
        validate_utf8: bool = True,
    ) -> tuple[bytes | None, os.stat_result | None]:
        if self._supports_dir_fd_operations():
            try:
                parent_fd, name = self._open_parent_directory_fd(target)
            except FileNotFoundError:
                return None, None
            except OSError:
                raise WorkspaceError("拒绝通过链接或工作区外路径读取文件。") from None
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                try:
                    descriptor = os.open(name, flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    return None, None
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
                    final_info = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent_fd)
            if len(data) > READ_LIMIT_BYTES:
                raise WorkspaceError("文件超过 64 KiB 安全上限。")
            if self._stat_token(info) != self._stat_token(final_info):
                raise WorkspaceConflictError(
                    "读取期间文件发生变化；请重新读取后再操作。",
                    current_bytes=data,
                    current_mode=stat.S_IMODE(final_info.st_mode),
                )
            if validate_utf8:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    raise WorkspaceError("当前仅支持 UTF-8 文本文件。") from None
            return data, final_info

        try:
            self._assert_no_reparse_points(target)
            try:
                handle = target.open("rb")
            except FileNotFoundError:
                return None, None
            with handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise WorkspaceError("目标路径不是普通文件。")
                if getattr(info, "st_nlink", 1) > 1:
                    raise WorkspaceError("拒绝访问含多个硬链接的文件，避免越过工作区边界。")
                if info.st_size > READ_LIMIT_BYTES:
                    raise WorkspaceError("文件超过 64 KiB 安全上限。")
                data = handle.read(READ_LIMIT_BYTES + 1)
                final_info = os.fstat(handle.fileno())
            self._assert_no_reparse_points(target)
        except WorkspaceError:
            raise
        except OSError as exc:
            raise WorkspaceError(f"读取文件失败：{type(exc).__name__}") from None
        if len(data) > READ_LIMIT_BYTES:
            raise WorkspaceError("文件超过 64 KiB 安全上限。")
        if self._stat_token(info) != self._stat_token(final_info):
            raise WorkspaceConflictError(
                "读取期间文件发生变化；请重新读取后再操作。",
                current_bytes=data,
                current_mode=stat.S_IMODE(final_info.st_mode),
            )
        if validate_utf8:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise WorkspaceError("当前仅支持 UTF-8 文本文件。") from None
        return data, final_info

    @staticmethod
    def _stat_token(info: os.stat_result | None) -> tuple[int, ...] | None:
        if info is None:
            return None
        return (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_mode),
            int(info.st_size),
            int(getattr(info, "st_nlink", 1)),
            int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
            int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
        )

    def read_text(self, relative_path: str) -> str:
        target = self.resolve(relative_path)
        data = self._read_snapshot(target)
        if data is None:
            raise WorkspaceError(f"文件不存在或不是普通文件：{relative_path}")
        return data.decode("utf-8-sig")

    @staticmethod
    def _search_path_matches(relative_path: str, glob_pattern: str) -> bool:
        if not glob_pattern:
            return True
        if "/" not in glob_pattern:
            return fnmatch.fnmatchcase(PurePosixPath(relative_path).name, glob_pattern)
        return _match_glob_parts(
            tuple(PurePosixPath(relative_path).parts),
            tuple(glob_pattern.split("/")),
        )

    @staticmethod
    def _search_ignored(
        relative_path: str,
        *,
        is_directory: bool,
        rules: list[_GitIgnoreRule],
        checkpoint: Callable[[], bool] | None = None,
    ) -> bool:
        parts = tuple(PurePosixPath(relative_path).parts)
        ignored = False
        for rule in rules:
            if checkpoint is not None and not checkpoint():
                return False
            if _matches_gitignore_rule(rule, parts, is_directory=is_directory):
                ignored = not rule.negated
        return ignored

    def _validate_search_request(
        self,
        query: str,
        relative_directory: str,
        glob_pattern: str,
    ) -> Path:
        from .sessions import contains_likely_secret

        if (
            not isinstance(query, str)
            or not query
            or len(query) > SEARCH_QUERY_MAX_CHARS
            or any(
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
                for char in query
            )
            or contains_likely_secret(query)
        ):
            raise WorkspaceError("搜索字面量无效、超长或疑似凭据；没有扫描文件。")
        if (
            not isinstance(relative_directory, str)
            or len(relative_directory) > 1024
            or any(
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
                for char in relative_directory
            )
            or contains_likely_secret(relative_directory)
        ):
            raise WorkspaceError("搜索目录无效；没有扫描文件。")
        if (
            not isinstance(glob_pattern, str)
            or len(glob_pattern) > SEARCH_GLOB_MAX_CHARS
            or any(
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
                for char in glob_pattern
            )
            or contains_likely_secret(glob_pattern)
        ):
            raise WorkspaceError("文件名过滤规则无效或疑似凭据；没有扫描文件。")
        if glob_pattern and (
            PureWindowsPath(glob_pattern).is_absolute()
            or PureWindowsPath(glob_pattern).drive
            or glob_pattern.startswith("/")
            or ":" in glob_pattern
            or ".." in re.split(r"[\\/]+", glob_pattern)
            or "\\" in glob_pattern
        ):
            raise WorkspaceError("文件名过滤规则必须是工作区内的相对 glob。")
        try:
            scope = self.resolve_directory(relative_directory or ".")
        except WorkspaceError:
            raise
        if len(scope.relative_to(self.root).as_posix()) > 1024:
            raise WorkspaceError("搜索目录超过本地显示长度上限。")
        return scope

    @staticmethod
    def _escape_search_text(value: str) -> tuple[str, int]:
        from .sessions import redact_likely_secrets

        safe, redaction_count = redact_likely_secrets(value)
        escaped: list[str] = []
        for char in safe:
            if (
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
            ):
                codepoint = ord(char)
                if codepoint <= 0xFF:
                    escaped.append(f"\\x{codepoint:02x}")
                elif codepoint <= 0xFFFF:
                    escaped.append(f"\\u{codepoint:04x}")
                else:
                    escaped.append(f"\\U{codepoint:08x}")
            else:
                escaped.append(char)
        return "".join(escaped), redaction_count

    def search_text(
        self,
        query: str,
        *,
        relative_directory: str = ".",
        glob_pattern: str = "",
        cancellation_event: Any | None = None,
    ) -> str:
        """Search bounded UTF-8 text below an approved workspace-relative directory."""
        from .sessions import contains_likely_secret

        scope = self._validate_search_request(query, relative_directory, glob_pattern)
        scope_parts = tuple(scope.relative_to(self.root).parts)
        scope_posix = PurePosixPath(*scope_parts).as_posix() if scope_parts else "."
        started = time.monotonic()
        deadline = started + SEARCH_TIME_LIMIT_SECONDS
        counts = {
            "directories": 0,
            "entries": 0,
            "files": 0,
            "bytes": 0,
            "ignored": 0,
            "sensitive_or_linked": 0,
            "oversized": 0,
            "binary": 0,
            "unreadable": 0,
            "redactions": 0,
        }
        matches: list[dict[str, Any]] = []
        reasons: list[str] = []
        scope_ignored = False
        output_bytes = 0

        def stop(reason: str) -> None:
            if reason not in reasons:
                reasons.append(reason)

        def checkpoint() -> bool:
            _raise_if_cancelled(cancellation_event)
            if time.monotonic() >= deadline:
                stop("time_limit")
                return False
            return True

        def read_ignore_rules(
            directory: Path,
            directory_parts: tuple[str, ...],
            inherited: list[_GitIgnoreRule],
        ) -> list[_GitIgnoreRule] | None:
            ignore_relative = PurePosixPath(*(directory_parts + (".gitignore",))).as_posix()
            try:
                ignore_path = directory / ".gitignore"
                if self._is_reparse_point(ignore_path):
                    stop("ignore_rules_unreadable")
                    return None
                ignore_target = self.resolve(ignore_relative)
                info = ignore_target.stat()
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1:
                    stop("ignore_rules_unreadable")
                    return None
                if info.st_size > SEARCH_FILE_LIMIT_BYTES:
                    stop("ignore_rules_unreadable")
                    return None
                if counts["bytes"] + info.st_size > SEARCH_TOTAL_BYTES_LIMIT:
                    stop("byte_limit")
                    return None
                data = self._read_snapshot(ignore_target)
                if data is None:
                    stop("ignore_rules_unreadable")
                    return None
                if len(data) > SEARCH_FILE_LIMIT_BYTES:
                    stop("ignore_rules_unreadable")
                    return None
                if counts["bytes"] + len(data) > SEARCH_TOTAL_BYTES_LIMIT:
                    stop("byte_limit")
                    return None
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    stop("ignore_rules_unreadable")
                    return None
                counts["bytes"] += len(data)
                local = _parse_gitignore(text, directory_parts)
                if local is None or len(inherited) + len(local) > SEARCH_GITIGNORE_RULE_LIMIT:
                    stop("ignore_rules_unreadable")
                    return None
                return local
            except FileNotFoundError:
                return []
            except WorkspaceError:
                # A present but unreadable ignore file makes the effective scope
                # ambiguous, so fail closed instead of scanning unfiltered content.
                if (directory / ".gitignore").exists():
                    stop("ignore_rules_unreadable")
                    return None
                return []
            except OSError:
                stop("ignore_rules_unreadable")
                return None

        rules: list[_GitIgnoreRule] = []
        current_dir = self.root
        current_parts: tuple[str, ...] = ()
        for part in scope_parts:
            if not checkpoint():
                break
            local = read_ignore_rules(current_dir, current_parts, rules)
            if local is None:
                break
            rules.extend(local)
            current_parts = current_parts + (part,)
            relative_part = PurePosixPath(*current_parts).as_posix()
            ignored_scope = self._search_ignored(
                relative_part,
                is_directory=True,
                rules=rules,
                checkpoint=checkpoint,
            )
            if reasons:
                break
            if ignored_scope:
                counts["ignored"] += 1
                scope_ignored = True
                break
            current_dir = current_dir / part

        stack: list[tuple[Path, tuple[str, ...], list[_GitIgnoreRule]]] = []
        if not reasons and not scope_ignored:
            try:
                if self.resolve_directory(relative_directory or ".") != scope:
                    raise WorkspaceError("审批后搜索目录发生变化；没有扫描文件。")
            except WorkspaceError:
                raise
            local = read_ignore_rules(scope, scope_parts, rules)
            if local is None:
                pass
            else:
                stack.append((scope, scope_parts, rules + local))

        while stack and not reasons and not scope_ignored:
            if not checkpoint():
                break
            directory, directory_parts, directory_rules = stack.pop()
            if counts["directories"] >= SEARCH_DIRECTORY_COUNT_LIMIT:
                stop("directory_limit")
                break
            counts["directories"] += 1
            relative_directory_now = PurePosixPath(*directory_parts).as_posix() if directory_parts else "."
            try:
                if self.resolve_directory(relative_directory_now) != directory:
                    raise WorkspaceError("搜索期间目录路径发生变化。")
                scan_fd: int | None = None
                scan_target: int | Path = directory
                if self._supports_dir_fd_operations():
                    scan_fd, _unused_name = self._open_parent_directory_fd(
                        directory / ".cvfx-search-directory-handle"
                    )
                    scan_target = scan_fd
                else:
                    if directory == self.root:
                        if self._is_reparse_point(directory):
                            raise WorkspaceError(
                                "工作区根路径包含 symlink/junction；操作已取消。"
                            )
                    else:
                        self._assert_no_reparse_points(directory)
                try:
                    with os.scandir(scan_target) as iterator:
                        names: list[str] = []
                        while True:
                            if not checkpoint():
                                break
                            try:
                                entry = next(iterator)
                            except StopIteration:
                                break
                            if counts["entries"] >= SEARCH_ENTRY_LIMIT:
                                stop("entry_limit")
                                break
                            counts["entries"] += 1
                            names.append(entry.name)
                finally:
                    if scan_fd is not None:
                        os.close(scan_fd)
                if self.resolve_directory(relative_directory_now) != directory:
                    raise WorkspaceError("搜索期间目录路径发生变化。")
                names.sort(key=lambda name: (name.casefold(), name))
                child_directories: list[tuple[Path, tuple[str, ...], list[_GitIgnoreRule]]] = []
                for name in names:
                    if not checkpoint():
                        break
                    if name in {".", ".."} or any(
                        unicodedata.category(char).startswith("C")
                        or unicodedata.category(char) in {"Zl", "Zp"}
                        for char in name
                    ):
                        continue
                    child_parts = directory_parts + (name,)
                    relative = PurePosixPath(*child_parts).as_posix()
                    if len(relative) > 1024:
                        stop("path_limit")
                        break
                    child_path = self.root.joinpath(*child_parts)
                    try:
                        if self._is_reparse_point(child_path):
                            counts["sensitive_or_linked"] += 1
                            continue
                        info = child_path.stat(follow_symlinks=False)
                    except (OSError, WorkspaceError):
                        counts["unreadable"] += 1
                        continue
                    is_directory = stat.S_ISDIR(info.st_mode)
                    if not is_directory and not stat.S_ISREG(info.st_mode):
                        counts["binary"] += 1
                        continue
                    ignored = self._search_ignored(
                        relative,
                        is_directory=is_directory,
                        rules=directory_rules,
                        checkpoint=checkpoint,
                    )
                    if reasons:
                        break
                    if ignored:
                        counts["ignored"] += 1
                        continue
                    try:
                        if is_directory:
                            resolved_child = self.resolve_directory(relative)
                        else:
                            resolved_child = self.resolve(relative)
                    except WorkspaceError:
                        counts["sensitive_or_linked"] += 1
                        continue

                    if is_directory:
                        if len(child_parts) - len(scope_parts) > SEARCH_DEPTH_LIMIT:
                            stop("depth_limit")
                            break
                        if counts["directories"] + len(stack) + len(child_directories) >= SEARCH_DIRECTORY_COUNT_LIMIT:
                            stop("directory_limit")
                            break
                        child_local = read_ignore_rules(resolved_child, child_parts, directory_rules)
                        if child_local is None:
                            break
                        child_directories.append(
                            (resolved_child, child_parts, directory_rules + child_local)
                        )
                        continue

                    if getattr(info, "st_nlink", 1) > 1 or contains_likely_secret(relative):
                        counts["sensitive_or_linked"] += 1
                        continue
                    if not self._search_path_matches(
                        PurePosixPath(*child_parts[len(scope_parts) :]).as_posix(),
                        glob_pattern,
                    ):
                        continue
                    if info.st_size > SEARCH_FILE_LIMIT_BYTES:
                        counts["oversized"] += 1
                        continue
                    if counts["files"] >= SEARCH_FILE_COUNT_LIMIT:
                        stop("file_limit")
                        break
                    if counts["bytes"] + info.st_size > SEARCH_TOTAL_BYTES_LIMIT:
                        stop("byte_limit")
                        break
                    counts["files"] += 1
                    try:
                        data, _final_info = self._read_snapshot_state(
                            resolved_child,
                            validate_utf8=False,
                        )
                    except WorkspaceError:
                        counts["unreadable"] += 1
                        continue
                    if data is None:
                        counts["unreadable"] += 1
                        continue
                    if len(data) > SEARCH_FILE_LIMIT_BYTES:
                        counts["oversized"] += 1
                        continue
                    counts["bytes"] += len(data)
                    if b"\0" in data:
                        counts["binary"] += 1
                        continue
                    try:
                        text_value = data.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        counts["binary"] += 1
                        continue
                    for line_number, line in enumerate(text_value.splitlines(), 1):
                        if not checkpoint():
                            break
                        safe_line, line_redactions = self._escape_search_text(line)
                        counts["redactions"] += line_redactions
                        match_at = safe_line.find(query)
                        if match_at < 0:
                            continue
                        room = max(0, SEARCH_MATCH_SNIPPET_CHARS - len(query))
                        start = max(0, match_at - room // 2)
                        end = min(len(safe_line), start + SEARCH_MATCH_SNIPPET_CHARS)
                        start = max(0, end - SEARCH_MATCH_SNIPPET_CHARS)
                        snippet = safe_line[start:end]
                        if start:
                            snippet = "…" + snippet
                        if end < len(safe_line):
                            snippet += "…"
                        rendered_path, path_redactions = self._escape_search_text(relative)
                        counts["redactions"] += path_redactions
                        match = {"path": rendered_path, "line": line_number, "text": snippet}
                        encoded_match = json.dumps(
                            match, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                        if output_bytes + len(encoded_match) > SEARCH_OUTPUT_LIMIT_BYTES - 4096:
                            stop("output_limit")
                            break
                        matches.append(match)
                        output_bytes += len(encoded_match)
                        if len(matches) >= SEARCH_MATCH_LIMIT:
                            stop("result_limit")
                            break
                    if reasons:
                        break
                if reasons:
                    break
                # Reverse so traversal remains sorted while using a LIFO stack.
                stack.extend(reversed(child_directories))
            except WorkspaceError:
                raise
            except OSError as exc:
                raise WorkspaceError(f"工作区文本搜索失败：{type(exc).__name__}") from None

        _raise_if_cancelled(cancellation_event)
        result = {
            "untrusted": True,
            "mode": "literal_case_sensitive",
            "query": query,
            "scope": scope_posix,
            "scope_message": (
                "所选目录被 `.gitignore` 排除；没有读取其中的文件。"
                if scope_ignored
                else None
            ),
            "glob": glob_pattern or None,
            "matches": matches,
            "counts": counts,
            "scope_ignored": scope_ignored,
            "truncated": bool(reasons),
            "truncation_reasons": reasons,
            "messages": {
                "time_limit": "达到 5 秒时限；搜索结果不完整。",
                "entry_limit": "达到目录项扫描上限；搜索结果不完整。",
                "file_limit": "达到文件扫描上限；搜索结果不完整。",
                "directory_limit": "达到目录扫描上限；搜索结果不完整。",
                "depth_limit": "达到递归深度上限；搜索结果不完整。",
                "byte_limit": "达到累计读取字节上限；搜索结果不完整。",
                "result_limit": "达到匹配结果条数上限；后续内容未检查。",
                "output_limit": "达到搜索结果输出上限；部分匹配行未返回。",
                "path_limit": "工作区相对路径过长；搜索已停止。",
                "ignore_rules_unreadable": "无法安全解析 .gitignore；搜索已停止，没有继续扫描。",
            },
        }
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        while len(encoded.encode("utf-8")) > SEARCH_OUTPUT_LIMIT_BYTES and matches:
            matches.pop()
            stop("output_limit")
            result["truncated"] = True
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return encoded

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
        generated = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}" if before_exists else "/dev/null",
            tofile=f"b/{path}" if after_exists else "/dev/null",
            lineterm="\n",
        )
        output: list[str] = []
        for index, line in enumerate(generated):
            output.append(line)
            if (
                index >= 2
                and line.startswith((" ", "+", "-"))
                and not line.endswith(("\n", "\r"))
            ):
                output.append("\n\\ No newline at end of file\n")
        return "".join(output)

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
        before_bytes, before_info = self._read_snapshot_state(target)
        return self._build_write_preview(relative_path, target, content, before_bytes, before_info)

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
        before_bytes, before_info = self._read_snapshot_state(target)
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
        preview = self._build_write_preview(relative_path, target, updated_text, before_bytes, before_info)
        try:
            diff_size = len(preview.diff.encode("utf-8"))
        except UnicodeEncodeError:
            raise WorkspaceError("补丁差异不是有效 UTF-8 文本。") from None
        if diff_size > PATCH_DIFF_LIMIT_BYTES:
            raise WorkspaceError("补丁差异超过 32 KiB 审批显示上限。")
        return preview, actual_sha256

    def preview_change_set(self, changes: Any) -> ChangeSetPreview:
        """Validate the whole bounded set before producing an approval preview."""
        if not isinstance(changes, list) or not CHANGE_SET_MIN_FILES <= len(changes) <= CHANGE_SET_MAX_FILES:
            raise WorkspaceError(
                f"变更集必须包含 {CHANGE_SET_MIN_FILES}–{CHANGE_SET_MAX_FILES} 个文件。"
            )
        try:
            payload_bytes = len(
                json.dumps({"changes": changes}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise WorkspaceError("变更集参数格式无效。") from None
        if payload_bytes > CHANGE_SET_PAYLOAD_LIMIT_BYTES:
            raise WorkspaceError(
                f"变更集输入超过 {CHANGE_SET_PAYLOAD_LIMIT_BYTES // 1024} KiB 上限。"
            )

        from .sessions import contains_likely_secret

        previews: list[tuple[WritePreview, str]] = []
        seen_targets: set[str] = set()
        backup_bytes = 0
        for index, item in enumerate(changes, 1):
            if not isinstance(item, dict) or set(item) != {
                "path", "expected_sha256", "old_text", "new_text"
            }:
                raise WorkspaceError(f"变更集第 {index} 项字段无效。")
            if any(not isinstance(item[name], str) for name in item):
                raise WorkspaceError(f"变更集第 {index} 项字段类型无效。")
            path = item["path"]
            if any(
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
                for char in path
            ) or contains_likely_secret(path):
                raise WorkspaceError(f"变更集第 {index} 项路径含控制字符或疑似凭据。")
            preview, actual_sha256 = self.preview_patch(
                path,
                expected_sha256=item["expected_sha256"],
                old_text=item["old_text"],
                new_text=item["new_text"],
            )
            canonical_key = os.path.normcase(preview.canonical_path).casefold()
            if canonical_key in seen_targets:
                raise WorkspaceError("变更集包含重复或平台大小写冲突的文件路径。")
            seen_targets.add(canonical_key)
            if not preview.changed:
                raise WorkspaceError(f"变更集第 {index} 项没有实际变化；整批未审批、未写入。")
            if preview.before_bytes is None:
                raise WorkspaceError("变更集只允许修改已存在的普通 UTF-8 文件。")
            if (
                contains_likely_secret(preview.before_content)
                or contains_likely_secret(preview.new_content)
                or contains_likely_secret(preview.canonical_path)
            ):
                raise WorkspaceError(f"变更集第 {index} 项含疑似凭据；整批未审批、未写入。")
            backup_bytes += len(preview.before_bytes)
            previews.append((preview, actual_sha256))
        if backup_bytes > CHANGE_SET_BACKUP_LIMIT_BYTES:
            raise WorkspaceError(
                f"变更集原始备份合计超过 {CHANGE_SET_BACKUP_LIMIT_BYTES // 1024} KiB 上限。"
            )

        previews.sort(key=lambda item: (item[0].canonical_path.casefold(), item[0].canonical_path))
        digest_data: list[dict[str, Any]] = []
        rendered = [
            f"变更集共 {len(previews)} 个现有文件；预检与审批覆盖全部文件，执行按路径排序。"
        ]
        diff_bytes = 0
        for index, (preview, sha256) in enumerate(previews, 1):
            new_bytes = preview.new_content.encode("utf-8")
            file_diff = preview.diff
            diff_bytes += len(file_diff.encode("utf-8"))
            rendered.append(
                f"\n[{index}/{len(previews)}] 文件：{preview.canonical_path}\n"
                f"原文件 SHA-256：{sha256[:16]}…\n"
                "精确差异（+ 新增，- 删除）：\n"
                f"{file_diff}"
            )
            digest_data.append(
                {
                    "path": preview.canonical_path,
                    "before_sha256": sha256,
                    "after_sha256": hashlib.sha256(new_bytes).hexdigest(),
                    "before_mode": preview.before_mode,
                    "before_token": preview.before_token,
                    "before_acl_sha256": self._acl_sha256(preview.before_acl_token),
                    "diff_sha256": hashlib.sha256(file_diff.encode("utf-8")).hexdigest(),
                }
            )
        diff = "".join(rendered)
        try:
            complete_diff_bytes = len(diff.encode("utf-8"))
        except UnicodeEncodeError:
            raise WorkspaceError("变更集差异不是有效 UTF-8；整批未审批、未写入。") from None
        if diff_bytes > CHANGE_SET_DIFF_LIMIT_BYTES or complete_diff_bytes > CHANGE_SET_DIFF_LIMIT_BYTES:
            raise WorkspaceError(
                f"变更集完整差异超过 {CHANGE_SET_DIFF_LIMIT_BYTES // 1024} KiB 审阅上限；整批未审批、未写入。"
            )
        digest_payload = json.dumps(
            digest_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return ChangeSetPreview(
            changes=tuple(preview for preview, _sha256 in previews),
            file_sha256=tuple(sha256 for _preview, sha256 in previews),
            diff=diff,
            digest=hashlib.sha256(digest_payload).hexdigest(),
            payload_bytes=payload_bytes,
            backup_bytes=backup_bytes,
        )

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
        before_info: os.stat_result | None,
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
        before_mode = stat.S_IMODE(before_info.st_mode) if before_info is not None else None
        before_acl_token = (
            self._windows_dacl_token(target)
            if os.name == "nt" and before_info is not None
            else None
        )
        if os.name != "nt" and before_mode is not None and before_mode & 0o7000:
            diff = (
                f"POSIX 权限位：{before_mode:04o} → {before_mode & 0o777:04o}"
                "（替换时清除 setuid/setgid/sticky 特殊位）\n"
                + diff
            )
        return WritePreview(
            path=relative_path,
            canonical_path=canonical_path,
            target=target,
            existed=existed,
            before_bytes=before_bytes,
            before_mode=before_mode,
            before_token=self._stat_token(before_info),
            before_acl_token=before_acl_token,
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
        if name not in {_BACKUP_DIR, "sessions", "config", "plans", _CHANGE_SET_DIR}:
            raise WorkspaceError("未授权的 Harness 内部存储目录。")
        self._assert_root_identity()
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

    def _change_set_dir(self, *, create: bool) -> Path | None:
        return self.internal_storage_dir(_CHANGE_SET_DIR, create=create)

    @staticmethod
    def _validate_change_set_id(change_set_id: str) -> None:
        if not isinstance(change_set_id, str) or re.fullmatch(r"[0-9a-f]{32}", change_set_id) is None:
            raise WorkspaceError("变更集编号无效。")

    def _change_set_path(self, change_set_id: str, *, create: bool = False) -> Path:
        self._validate_change_set_id(change_set_id)
        directory = self._change_set_dir(create=create)
        if directory is None:
            raise WorkspaceError("没有可读取的变更集日志。")
        return directory / f"{change_set_id}.json"

    @staticmethod
    def _validate_change_set_record(record: Any) -> dict[str, Any]:
        from .sessions import contains_likely_secret

        states = {
            "applying", "rolling_back", "recovery_required", "recovery_in_progress",
            "completed", "rolled_back", "recovered", "pruning",
        }
        file_states = {
            "pending", "applying", "committed", "failed", "restoring",
            "restore_failed", "restored", "not_applied", "conflict",
        }
        base_record_keys = {"version", "id", "created_ns", "updated_ns", "state", "files"}
        state_value = record.get("state") if isinstance(record, dict) else None
        record_keys = (
            base_record_keys | {"prune_terminal_state", "prune_next_index"}
            if state_value == "pruning"
            else base_record_keys
        )
        if (
            not isinstance(record, dict)
            or set(record) != record_keys
            or type(record.get("version")) is not int
            or record.get("version") != 1
            or not isinstance(record.get("id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", record["id"]) is None
            or type(record.get("created_ns")) is not int
            or record["created_ns"] < 0
            or type(record.get("updated_ns")) is not int
            or record["updated_ns"] < record["created_ns"]
            or not isinstance(record.get("state"), str)
            or record.get("state") not in states
            or not isinstance(record.get("files"), list)
            or not CHANGE_SET_MIN_FILES <= len(record["files"]) <= CHANGE_SET_MAX_FILES
        ):
            raise WorkspaceError("变更集日志格式无效；未自动恢复或执行。")
        seen: set[str] = set()
        seen_backup_ids: set[str] = set()
        for item in record["files"]:
            if (
                not isinstance(item, dict)
                or set(item) != {
                    "path", "before_sha256", "after_sha256", "before_mode", "after_mode",
                    "before_acl_sha256", "after_acl_permissions_sha256", "backup_id", "status",
                }
                or not isinstance(item.get("path"), str)
                or not item["path"]
                or any(
                    unicodedata.category(char).startswith("C")
                    or unicodedata.category(char) in {"Zl", "Zp"}
                    for char in item["path"]
                )
                or contains_likely_secret(item["path"])
                or any(
                    not isinstance(item.get(key), str)
                    or re.fullmatch(r"[0-9a-f]{64}", item.get(key, "")) is None
                    for key in ("before_sha256", "after_sha256")
                )
                or type(item.get("before_mode")) is not int
                or not 0 <= item["before_mode"] <= 0o7777
                or type(item.get("after_mode")) is not int
                or not 0 <= item["after_mode"] <= 0o777
                or any(
                    item.get(key) is not None
                    and (
                        not isinstance(item.get(key), str)
                        or re.fullmatch(r"[0-9a-f]{64}", item.get(key, "")) is None
                    )
                    for key in ("before_acl_sha256", "after_acl_permissions_sha256")
                )
                or not isinstance(item.get("backup_id"), str)
                or re.fullmatch(r"[0-9]+-[0-9a-f]{32}", item.get("backup_id", "")) is None
                or not isinstance(item.get("status"), str)
                or item.get("status") not in file_states
            ):
                raise WorkspaceError("变更集日志中的文件状态无效；未自动恢复或执行。")
            key = os.path.normcase(item["path"]).casefold()
            if key in seen:
                raise WorkspaceError("变更集日志包含重复文件路径；未自动恢复或执行。")
            seen.add(key)
            if item["backup_id"] in seen_backup_ids:
                raise WorkspaceError("变更集日志包含重复备份编号；未自动恢复或执行。")
            seen_backup_ids.add(item["backup_id"])
        statuses = [item["status"] for item in record["files"]]
        state = record["state"]
        if state == "pruning":
            terminal_state = record.get("prune_terminal_state")
            next_index = record.get("prune_next_index")
            if (
                not isinstance(terminal_state, str)
                or terminal_state not in {"completed", "rolled_back", "recovered"}
                or type(next_index) is not int
                or not 0 <= next_index <= len(statuses)
            ):
                raise WorkspaceError("变更集日志清理状态无效；未自动恢复或执行。")
        else:
            terminal_state = state
        if terminal_state == "completed" and any(status != "committed" for status in statuses):
            raise WorkspaceError("变更集终态与文件状态矛盾；未自动恢复或执行。")
        if terminal_state in {"rolled_back", "recovered"} and any(
            status not in {"restored", "not_applied"} for status in statuses
        ):
            raise WorkspaceError("变更集终态与文件状态矛盾；未自动恢复或执行。")
        if state == "applying":
            unfinished_seen = False
            applying_seen = False
            for status in statuses:
                if status == "committed" and not unfinished_seen:
                    continue
                if status == "applying" and not unfinished_seen and not applying_seen:
                    applying_seen = True
                    unfinished_seen = True
                    continue
                if status == "pending":
                    unfinished_seen = True
                    continue
                raise WorkspaceError("变更集执行状态与文件状态矛盾；未自动恢复或执行。")
        if state == "recovery_in_progress" and any(
            status not in {"restoring", "restored", "not_applied", "conflict"}
            for status in statuses
        ):
            raise WorkspaceError("变更集恢复状态与文件状态矛盾；未自动恢复或执行。")
        return record

    def _write_change_set_record(self, record: dict[str, Any]) -> None:
        self._validate_change_set_record(record)
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > CHANGE_SET_JOURNAL_LIMIT_BYTES:
            raise WorkspaceError("变更集日志超过本地安全上限；没有继续写入。")
        target = self._change_set_path(record["id"], create=True)
        record["updated_ns"] = max(time.time_ns(), record["created_ns"])
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > CHANGE_SET_JOURNAL_LIMIT_BYTES:
            raise WorkspaceError("变更集日志超过本地安全上限；没有继续写入。")
        self._atomic_write_bytes(target, encoded)

    def _load_change_set_record(self, change_set_id: str) -> dict[str, Any]:
        path = self._change_set_path(change_set_id, create=False)
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise WorkspaceError("没有找到该变更集日志。") from None
        except OSError as exc:
            raise WorkspaceError(f"无法检查变更集日志：{type(exc).__name__}") from None
        if (
            self._is_reparse_point(path)
            or not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_nlink", 1) != 1
            or info.st_size > CHANGE_SET_JOURNAL_LIMIT_BYTES
        ):
            raise WorkspaceError("变更集日志路径或大小无效；未自动恢复或执行。")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise WorkspaceError("变更集日志损坏或无法读取；未自动恢复或执行。") from None
        record = self._validate_change_set_record(record)
        if record["id"] != change_set_id:
            raise WorkspaceError("变更集日志编号不匹配；未自动恢复或执行。")
        for item in record["files"]:
            try:
                target = self._resolve_write_target(item["path"])
                canonical = target.relative_to(self.root).as_posix()
            except (ValueError, WorkspaceError):
                raise WorkspaceError("变更集日志路径已越界或不再安全；未自动恢复或执行。") from None
            if canonical != item["path"]:
                raise WorkspaceError("变更集日志路径规范已变化；未自动恢复或执行。")
        return record

    def _list_change_set_records(self) -> list[dict[str, Any]]:
        directory = self._change_set_dir(create=False)
        if directory is None:
            return []
        paths: list[Path] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json"):
                        continue
                    if len(paths) >= CHANGE_SET_JOURNAL_SCAN_LIMIT:
                        raise WorkspaceError(
                            "变更集日志超过安全扫描上限；没有据此判定为无待办。"
                        )
                    paths.append(Path(entry.path))
        except OSError as exc:
            raise WorkspaceError(f"无法列出变更集日志：{type(exc).__name__}") from None
        records: list[dict[str, Any]] = []
        for path in paths:
            if self._is_reparse_point(path):
                raise WorkspaceError("拒绝读取链接形式的变更集日志。")
            if re.fullmatch(r"[0-9a-f]{32}", path.stem) is None:
                raise WorkspaceError("变更集日志名称无效；未自动恢复或执行。")
            record = self._load_change_set_record(path.stem)
            records.append(record)
        records = sorted(records, key=lambda item: (item["created_ns"], item["id"]))
        pruning = [record for record in records if record["state"] == "pruning"]
        for record in pruning:
            self._resume_terminal_change_set_log_pruning(record)
        if pruning:
            # Reload after pruning so callers never mistake a cleanup journal
            # for an incomplete batch or inspect its now-unlinked backups.
            return self._list_change_set_records()
        return records

    def list_incomplete_change_sets(self) -> list[dict[str, Any]]:
        records = self._list_change_set_records()
        evidence = self._inspect_change_set_backup_evidence(records)
        if self._change_set_evidence_has_warnings(evidence):
            raise WorkspaceError(
                "变更集存储包含孤立、矛盾或无法完整核对的备份证据；"
                "不得据此判定为没有待办，请使用 /变更集 证据人工核对。"
            )
        return [
            record
            for record in records
            if record["state"] not in {"completed", "rolled_back", "recovered"}
        ]

    @staticmethod
    def _change_set_evidence_has_warnings(evidence: dict[str, Any]) -> bool:
        return bool(
            evidence["orphans"]
            or evidence["unverified"]
            or evidence["mismatches"]
            or evidence["scan_limited"]
        )

    def _prune_change_set_logs(self) -> None:
        directory = self._change_set_dir(create=False)
        if directory is None:
            return
        records = self._list_change_set_records()
        evidence = self._inspect_change_set_backup_evidence(records)
        if self._change_set_evidence_has_warnings(evidence):
            raise WorkspaceError(
                "变更集存储证据无法完整核对；拒绝清理状态日志或开始新批次。"
            )
        incomplete = [
            record
            for record in records
            if record["state"] not in {"completed", "rolled_back", "recovered"}
        ]
        if len(incomplete) >= CHANGE_SET_LOG_LIMIT:
            raise WorkspaceError("未完成变更集已达本地上限；请先审阅并处理恢复项。")
        terminal = [
            (self._change_set_path(record["id"], create=False), record)
            for record in records
            if record["state"] in {"completed", "rolled_back", "recovered"}
        ]
        terminal.sort(key=lambda item: (item[1]["created_ns"], item[1]["id"]))
        retained_before_new_log = max(0, CHANGE_SET_TERMINAL_LOG_RETENTION - 1)
        remove_count = max(0, len(terminal) - retained_before_new_log)
        for _path, record in terminal[:remove_count]:
            self._prune_terminal_change_set_log(record)

    def _prune_terminal_change_set_log(self, record: dict[str, Any]) -> None:
        if record["state"] not in {"completed", "rolled_back", "recovered"}:
            raise WorkspaceError("只允许清理已完成的变更集日志。")
        pruning = dict(record)
        pruning["state"] = "pruning"
        pruning["prune_terminal_state"] = record["state"]
        pruning["prune_next_index"] = 0
        self._write_change_set_record(pruning)
        self._resume_terminal_change_set_log_pruning(pruning)

    def _load_prunable_change_set_backup(
        self,
        record: dict[str, Any],
        index: int,
        *,
        allow_unlinked: bool,
    ) -> tuple[Path, dict[str, Any]] | None:
        entry = record["files"][index]
        loaded = self._load_backups(backup_id=entry["backup_id"])
        if not loaded:
            if entry["status"] in {"committed", "restored"}:
                raise WorkspaceError("终态变更集缺少原始备份；拒绝清理其状态日志。")
            return None
        backup_path, backup = loaded[0]
        has_batch_id = "change_set_id" in backup
        has_batch_index = "change_set_index" in backup
        if not has_batch_id and not has_batch_index:
            if not allow_unlinked or not self._change_set_backup_payload_matches_entry(entry, backup):
                raise WorkspaceError("终态变更集备份关联不一致；拒绝续跑状态日志清理。")
            return None
        if not has_batch_id or not has_batch_index or not self._change_set_backup_matches_entry(
            record["id"], index, entry, backup
        ):
            raise WorkspaceError("终态变更集备份关联不一致；拒绝续跑状态日志清理。")
        return backup_path, backup

    def _resume_terminal_change_set_log_pruning(self, record: dict[str, Any]) -> None:
        if record["state"] != "pruning":
            raise WorkspaceError("变更集日志并非处于可续跑清理状态。")
        for index in range(len(record["files"])):
            next_index = record["prune_next_index"]
            if index < next_index:
                if self._load_prunable_change_set_backup(
                    record, index, allow_unlinked=True
                ) is not None:
                    raise WorkspaceError("变更集清理进度与备份标记矛盾；未继续清理。")
                continue
            if index > next_index:
                self._load_prunable_change_set_backup(
                    record, index, allow_unlinked=False
                )
                continue
            backup = self._load_prunable_change_set_backup(
                record, index, allow_unlinked=True
            )
            if backup is not None:
                backup_path, backup_record = backup
                backup_record.pop("change_set_id", None)
                backup_record.pop("change_set_index", None)
                self._atomic_write_json(backup_path, backup_record)
            record["prune_next_index"] = index + 1
            self._write_change_set_record(record)
        self._unlink_workspace_file(self._change_set_path(record["id"], create=False))

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

    def _assert_root_identity(self) -> None:
        if self._is_reparse_point(self.root):
            raise WorkspaceError("工作区路径包含 symlink/junction；操作已取消。")
        try:
            root_info = self.root.stat()
        except OSError:
            raise WorkspaceError("工作区根目录已不可用或发生变化；操作已取消。") from None
        if (root_info.st_dev, root_info.st_ino) != self._root_identity:
            raise WorkspaceError("工作区根目录在运行期间发生变化；操作已取消。")

    def _assert_no_reparse_points(self, target: Path) -> None:
        self._target_parts(target)
        self._assert_root_identity()
        current = self.root
        for component in target.relative_to(self.root).parts:
            current = current / component
            if self._is_reparse_point(current):
                raise WorkspaceError("路径包含 symlink/junction；操作已取消。")

    def _read_snapshot_at(
        self,
        parent_fd: int,
        name: str,
    ) -> tuple[bytes | None, os.stat_result | None, int | None]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return None, None, None
        except OSError:
            raise WorkspaceError("目标不是普通文件，或路径在替换前发生变化。") from None
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
            final_info = os.fstat(descriptor)
            if len(data) > READ_LIMIT_BYTES:
                raise WorkspaceError("文件超过 64 KiB 安全上限。")
            if self._stat_token(info) != self._stat_token(final_info):
                raise WorkspaceConflictError(
                    "替换前读取目标时文件仍在变化；未覆盖该文件。",
                    current_bytes=data,
                    current_mode=stat.S_IMODE(final_info.st_mode),
                )
            return data, final_info, descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _copy_posix_acl(source_fd: int, destination_fd: int) -> None:
        if os.name == "nt":
            return
        if not all(hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr", "removexattr")):
            raise WorkspaceError("当前平台无法核对并保留 POSIX ACL；为避免改变访问范围，写入已取消。")
        try:
            source_attrs = os.listxattr(source_fd)
            destination_attrs = os.listxattr(destination_fd)
        except OSError as exc:
            if exc.errno in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
                return
            raise WorkspaceError("无法核对现有 POSIX ACL；为避免改变访问范围，写入已取消。") from None
        source_acl = {name for name in source_attrs if "acl" in name.casefold()}
        destination_acl = {name for name in destination_attrs if "acl" in name.casefold()}
        try:
            for name in destination_acl - source_acl:
                os.removexattr(destination_fd, name)
            for name in source_acl:
                os.setxattr(destination_fd, name, os.getxattr(source_fd, name))
        except OSError:
            raise WorkspaceError("无法保留现有 POSIX ACL；目标文件未替换。") from None

    @staticmethod
    def _windows_dacl_token(target: Path) -> bytes:
        if os.name != "nt":
            return b""
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("Advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
        get_named_security_info = advapi32.GetNamedSecurityInfoW
        get_named_security_info.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        get_named_security_info.restype = wintypes.DWORD
        get_security_descriptor_control = advapi32.GetSecurityDescriptorControl
        get_security_descriptor_control.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_security_descriptor_control.restype = wintypes.BOOL
        get_security_descriptor_dacl = advapi32.GetSecurityDescriptorDacl
        get_security_descriptor_dacl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        get_security_descriptor_dacl.restype = wintypes.BOOL
        get_acl_information = advapi32.GetAclInformation
        get_acl_information.argtypes = [
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        get_acl_information.restype = wintypes.BOOL
        local_free = kernel32.LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL

        dacl_information = 0x00000004
        file_object = 1
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        status = get_named_security_info(
            str(target),
            file_object,
            dacl_information,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0 or not descriptor.value:
            raise WorkspaceError("无法读取目标 Windows DACL；操作已取消。")
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not get_security_descriptor_control(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise WorkspaceError("无法检查目标 Windows ACL 继承状态；操作已取消。")
            dacl_present = wintypes.BOOL()
            dacl_defaulted = wintypes.BOOL()
            dacl_from_descriptor = wintypes.LPVOID()
            if not get_security_descriptor_dacl(
                descriptor,
                ctypes.byref(dacl_present),
                ctypes.byref(dacl_from_descriptor),
                ctypes.byref(dacl_defaulted),
            ):
                raise WorkspaceError("无法检查目标 Windows DACL；操作已取消。")

            acl_bytes = b""
            if dacl_present.value and dacl_from_descriptor.value:
                class AclSizeInformation(ctypes.Structure):
                    _fields_ = [
                        ("AceCount", wintypes.DWORD),
                        ("AclBytesInUse", wintypes.DWORD),
                        ("AclBytesFree", wintypes.DWORD),
                    ]

                acl_size = AclSizeInformation()
                if not get_acl_information(
                    dacl_from_descriptor,
                    ctypes.byref(acl_size),
                    ctypes.sizeof(acl_size),
                    2,  # AclSizeInformation
                ):
                    raise WorkspaceError("无法读取目标 Windows DACL 内容；操作已取消。")
                acl_bytes = ctypes.string_at(dacl_from_descriptor, acl_size.AclBytesInUse)

            dacl_control_mask = 0x1500  # protected, auto-inherit requested/inherited
            control_bits = control.value & dacl_control_mask
            presence_bits = bytes((
                int(bool(dacl_present.value)),
                int(bool(dacl_defaulted.value)),
            ))
            return (
                b"cvfx-windows-dacl-v1\0"
                + control_bits.to_bytes(2, "little")
                + presence_bits
                + len(acl_bytes).to_bytes(4, "little")
                + acl_bytes
            )
        finally:
            local_free(descriptor)

    @staticmethod
    def _windows_dacl_copy_token(snapshot: bytes) -> bytes:
        """Compare file access/inheritance semantics after Windows normalization."""
        prefix = b"cvfx-windows-dacl-v1\0"
        header_size = len(prefix) + 8
        if not snapshot.startswith(prefix) or len(snapshot) < header_size:
            raise WorkspaceError("Windows DACL 快照无效；目标文件未替换。")
        control = int.from_bytes(snapshot[len(prefix) : len(prefix) + 2], "little")
        # SetNamedSecurityInfo may convert an ACL to the current automatic
        # inheritance model and recompute AUTO_INHERIT_* / DEFAULTED markers.
        # Keep the actual protection barrier and DACL presence. Every ACE flag,
        # including INHERITED_ACE and propagation flags, is compared below.
        state = (control & 0x1000).to_bytes(2, "little") + snapshot[len(prefix) + 2 : len(prefix) + 3]
        acl_length = int.from_bytes(snapshot[len(prefix) + 4 : header_size], "little")
        acl = snapshot[header_size:]
        if len(acl) != acl_length:
            raise WorkspaceError("Windows DACL 快照长度无效；目标文件未替换。")
        if not acl:
            return state + b"\0"
        if (
            len(acl) < 8
            or acl[0] not in (2, 4)  # ACL_REVISION / ACL_REVISION_DS
            or int.from_bytes(acl[2:4], "little") < len(acl)
        ):
            raise WorkspaceError("Windows DACL 结构无效；目标文件未替换。")
        ace_count = int.from_bytes(acl[4:6], "little")
        entries: list[bytes] = []
        offset = 8
        for _ in range(ace_count):
            if offset + 4 > len(acl):
                raise WorkspaceError("Windows DACL 条目无效；目标文件未替换。")
            size = int.from_bytes(acl[offset + 2 : offset + 4], "little")
            if size < 4 or offset + size > len(acl):
                raise WorkspaceError("Windows DACL 条目长度无效；目标文件未替换。")
            entries.append(acl[offset : offset + size])
            offset += size
        if offset != len(acl):
            raise WorkspaceError("Windows DACL 条目范围无效；目标文件未替换。")
        # Preserve ACE order and every ACE byte, including inheritance flags,
        # access masks, SIDs, object GUIDs, and callback/conditional data. The ACL
        # header's valid revision, capacity and reserved fields do not describe
        # access permissions. Raw approval/race snapshots remain unchanged.
        return state + b"\1" + ace_count.to_bytes(2, "little") + b"".join(entries)

    @staticmethod
    def _copy_windows_dacl(source: Path, destination: Path) -> None:
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("Advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
        get_named_security_info = advapi32.GetNamedSecurityInfoW
        get_named_security_info.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        get_named_security_info.restype = wintypes.DWORD
        get_security_descriptor_control = advapi32.GetSecurityDescriptorControl
        get_security_descriptor_control.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_security_descriptor_control.restype = wintypes.BOOL
        set_named_security_info = advapi32.SetNamedSecurityInfoW
        set_named_security_info.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        set_named_security_info.restype = wintypes.DWORD
        local_free = kernel32.LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL

        dacl_information = 0x00000004
        protected_dacl_information = 0x80000000
        unprotected_dacl_information = 0x20000000
        file_object = 1
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        status = get_named_security_info(
            str(source),
            file_object,
            dacl_information,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0 or not descriptor.value:
            raise WorkspaceError("无法读取目标 Windows DACL；目标文件未替换。")
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not get_security_descriptor_control(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise WorkspaceError("无法检查目标 Windows ACL 继承状态；目标文件未替换。")
            protection_flag = (
                protected_dacl_information
                if control.value & 0x1000
                else unprotected_dacl_information
            )
            status = set_named_security_info(
                str(destination),
                file_object,
                dacl_information | protection_flag,
                None,
                None,
                dacl,
                None,
            )
            if status != 0:
                raise WorkspaceError("无法保留目标 Windows DACL 及继承状态；目标文件未替换。")
        finally:
            local_free(descriptor)
        source_token = Workspace._windows_dacl_copy_token(Workspace._windows_dacl_token(source))
        destination_token = Workspace._windows_dacl_copy_token(Workspace._windows_dacl_token(destination))
        if source_token != destination_token:
            category = "保护/存在状态" if source_token[:3] != destination_token[:3] else "有序权限条目"
            raise WorkspaceError(f"替换文件的 Windows DACL 与继承状态不匹配（{category}）；目标文件未替换。")

    def _snapshot_matches(
        self,
        data: bytes | None,
        info: os.stat_result | None,
        expected_bytes: bytes | None,
        expected_mode: int | None,
        expected_token: tuple[int, ...] | None,
        expected_acl_token: bytes | None = None,
        current_acl_token: bytes | None = None,
    ) -> bool:
        if data != expected_bytes:
            return False
        if info is None:
            return (
                expected_mode is None
                and expected_token is None
                and expected_acl_token is None
            )
        if expected_mode is not None and stat.S_IMODE(info.st_mode) != expected_mode:
            return False
        if expected_token is not None and self._stat_token(info) != expected_token:
            return False
        if expected_acl_token is not None and current_acl_token != expected_acl_token:
            return False
        return True

    def _atomic_write_bytes(
        self,
        target: Path,
        data: bytes,
        *,
        guarded: bool = False,
        expected_before_bytes: bytes | None = None,
        expected_before_mode: int | None = None,
        expected_before_token: tuple[int, ...] | None = None,
        expected_before_acl_token: bytes | None = None,
    ) -> None:
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
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                source_fd: int | None = None
                if guarded:
                    current_bytes, current_info, source_fd = self._read_snapshot_at(parent_fd, name)
                    if not self._snapshot_matches(
                        current_bytes,
                        current_info,
                        expected_before_bytes,
                        expected_before_mode,
                        expected_before_token,
                    ):
                        if source_fd is not None:
                            os.close(source_fd)
                        raise WorkspaceConflictError(
                            "最后一次版本检查发现并发文件变化；没有替换目标。",
                            current_bytes=current_bytes,
                            current_mode=stat.S_IMODE(current_info.st_mode) if current_info else None,
                        )
                    if source_fd is not None:
                        try:
                            self._copy_posix_acl(source_fd, descriptor)
                        finally:
                            os.close(source_fd)
                        source_fd = None
                    if expected_before_mode is not None:
                        os.fchmod(descriptor, expected_before_mode & 0o777)
                    os.fsync(descriptor)
                    # Recheck after ACL/mode preparation. Only rename follows this
                    # comparison; generic filesystems do not offer a portable CAS.
                    current_bytes, current_info, source_fd = self._read_snapshot_at(parent_fd, name)
                    if not self._snapshot_matches(
                        current_bytes,
                        current_info,
                        expected_before_bytes,
                        expected_before_mode,
                        expected_before_token,
                    ):
                        if source_fd is not None:
                            os.close(source_fd)
                        raise WorkspaceConflictError(
                            "原子替换前检测到并发文件变化；没有替换目标。",
                            current_bytes=current_bytes,
                            current_mode=stat.S_IMODE(current_info.st_mode) if current_info else None,
                        )
                    if source_fd is not None:
                        os.close(source_fd)
                if descriptor is not None:
                    os.close(descriptor)
                    descriptor = None
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
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".harness-tmp-", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if guarded and expected_before_bytes is not None:
                if os.name == "nt":
                    self._copy_windows_dacl(target, temporary_path)
                else:
                    with target.open("rb") as source_handle:
                        self._copy_posix_acl(source_handle.fileno(), descriptor)
                if expected_before_mode is not None:
                    os.chmod(temporary_path, expected_before_mode & 0o777)
            self._assert_no_reparse_points(target)
            if guarded:
                current_bytes, current_info = self._read_snapshot_state(target, validate_utf8=False)
                current_acl_token: bytes | None = None
                if os.name == "nt" and current_info is not None:
                    try:
                        current_acl_token = self._windows_dacl_token(target)
                    except WorkspaceError:
                        raise WorkspaceConflictError(
                            "无法复核目标 Windows DACL；没有替换目标。",
                            current_bytes=current_bytes,
                            current_mode=stat.S_IMODE(current_info.st_mode),
                        ) from None
                if not self._snapshot_matches(
                    current_bytes,
                    current_info,
                    expected_before_bytes,
                    expected_before_mode,
                    expected_before_token,
                    expected_before_acl_token,
                    current_acl_token,
                ):
                    raise WorkspaceConflictError(
                        "原子替换前检测到并发文件变化；没有替换目标。",
                        current_bytes=current_bytes,
                        current_mode=stat.S_IMODE(current_info.st_mode) if current_info else None,
                        current_acl_token=current_acl_token,
                    )
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary_name, target)
            temporary_name = None
            temporary_path = None
        except WorkspaceConflictError:
            raise
        except WorkspaceError:
            raise
        except OSError as exc:
            raise WorkspaceError(f"原子写入失败：{type(exc).__name__}") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
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

    @staticmethod
    def _conflict_snapshot(error: WorkspaceConflictError) -> dict[str, Any]:
        current = error.current_bytes
        return {
            "captured_ns": time.time_ns(),
            "existed": current is not None,
            "sha256": hashlib.sha256(current).hexdigest() if current is not None else None,
            "bytes_base64": base64.b64encode(current).decode("ascii") if current is not None else None,
            "mode": error.current_mode,
            "acl_sha256": Workspace._acl_sha256(error.current_acl_token),
        }

    @staticmethod
    def _acl_sha256(token: bytes | None) -> str | None:
        return hashlib.sha256(token).hexdigest() if token is not None else None

    @staticmethod
    def _acl_permissions_sha256(token: bytes | None) -> str | None:
        if token is None:
            return None
        return hashlib.sha256(Workspace._windows_dacl_copy_token(token)).hexdigest()

    def _persist_conflict_snapshot(
        self,
        backup_path: Path,
        record: dict[str, Any],
        error: WorkspaceConflictError,
    ) -> bool:
        record["conflict_snapshot"] = self._conflict_snapshot(error)
        try:
            self._atomic_write_json(backup_path, record)
        except (OSError, WorkspaceError):
            return False
        return True

    def _unlink_workspace_file(
        self,
        target: Path,
        *,
        guarded: bool = False,
        expected_before_bytes: bytes | None = None,
        expected_before_mode: int | None = None,
        expected_before_token: tuple[int, ...] | None = None,
        expected_before_acl_token: bytes | None = None,
    ) -> None:
        if self._supports_dir_fd_operations():
            try:
                parent_fd, name = self._open_parent_directory_fd(target)
            except FileNotFoundError:
                raise WorkspaceError("撤销目标路径在操作期间发生变化。") from None
            try:
                if guarded:
                    current_bytes, current_info, descriptor = self._read_snapshot_at(parent_fd, name)
                    try:
                        if not self._snapshot_matches(
                            current_bytes,
                            current_info,
                            expected_before_bytes,
                            expected_before_mode,
                            expected_before_token,
                        ):
                            raise WorkspaceConflictError(
                                "撤销删除前发现并发文件变化；未删除目标。",
                                current_bytes=current_bytes,
                                current_mode=(
                                    stat.S_IMODE(current_info.st_mode) if current_info else None
                                ),
                            )
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
                os.unlink(name, dir_fd=parent_fd)
            except WorkspaceConflictError:
                raise
            except OSError as exc:
                raise WorkspaceError(f"撤销时删除新文件失败：{type(exc).__name__}") from None
            finally:
                os.close(parent_fd)
            return
        self._assert_no_reparse_points(target)
        if guarded:
            current_bytes, current_info = self._read_snapshot_state(target, validate_utf8=False)
            current_acl_token: bytes | None = None
            if os.name == "nt" and current_info is not None:
                try:
                    current_acl_token = self._windows_dacl_token(target)
                except WorkspaceError:
                    raise WorkspaceConflictError(
                        "无法复核目标 Windows DACL；未删除目标。",
                        current_bytes=current_bytes,
                        current_mode=stat.S_IMODE(current_info.st_mode),
                    ) from None
            if not self._snapshot_matches(
                current_bytes,
                current_info,
                expected_before_bytes,
                expected_before_mode,
                expected_before_token,
                expected_before_acl_token,
                current_acl_token,
            ):
                raise WorkspaceConflictError(
                    "撤销删除前发现并发文件变化；未删除目标。",
                    current_bytes=current_bytes,
                    current_mode=stat.S_IMODE(current_info.st_mode) if current_info else None,
                    current_acl_token=current_acl_token,
                )
        try:
            target.unlink()
        except OSError as exc:
            raise WorkspaceError(f"撤销时删除新文件失败：{type(exc).__name__}") from None

    def commit_write(
        self,
        preview: WritePreview,
        *,
        cancellation_event: Any | None = None,
        backup_id: str | None = None,
        change_set_id: str | None = None,
        change_set_index: int | None = None,
        persist_conflict_snapshot: bool = True,
    ) -> str | None:
        if not preview.changed:
            return None

        if backup_id is None:
            backup_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
        if not isinstance(backup_id, str) or re.fullmatch(r"[0-9]+-[0-9a-f]{32}", backup_id) is None:
            raise WorkspaceError("撤销备份编号无效；没有写入文件。")
        if change_set_id is not None:
            self._validate_change_set_id(change_set_id)
            if type(change_set_index) is not int or not 1 <= change_set_index <= CHANGE_SET_MAX_FILES:
                raise WorkspaceError("变更集备份序号无效；没有写入文件。")
        elif change_set_index is not None:
            raise WorkspaceError("缺少变更集编号；没有写入文件。")
        if type(persist_conflict_snapshot) is not bool:
            raise WorkspaceError("冲突快照策略无效；没有写入文件。")

        _raise_if_cancelled(cancellation_event)
        backup_dir = self._backup_dir(create=True)
        assert backup_dir is not None
        new_bytes = preview.new_content.encode("utf-8")
        record: dict[str, Any] = {
            "version": 1,
            "id": backup_id,
            "created_ns": time.time_ns(),
            "path": preview.canonical_path,
            "before_exists": preview.existed,
            "before_mode": preview.before_mode,
            "after_mode": (preview.before_mode & 0o777) if preview.before_mode is not None else None,
            "before_acl_sha256": self._acl_sha256(preview.before_acl_token),
            "after_acl_sha256": (
                self._acl_sha256(preview.before_acl_token) if preview.existed else None
            ),
            "after_acl_permissions_sha256": self._acl_permissions_sha256(preview.before_acl_token),
            "before_base64": (
                base64.b64encode(preview.before_bytes).decode("ascii")
                if preview.before_bytes is not None
                else None
            ),
            "after_sha256": hashlib.sha256(new_bytes).hexdigest(),
            "state": "pending",
            "conflict_snapshot": None,
            "undone": False,
        }
        if change_set_id is not None:
            record["change_set_id"] = change_set_id
            record["change_set_index"] = change_set_index
        backup_path = backup_dir / f"{backup_id}.json"
        self._atomic_write_json(backup_path, record)
        try:
            _raise_if_cancelled(cancellation_event)
            current_target = self._revalidate_write_preview(preview)
            _raise_if_cancelled(cancellation_event)
            self._atomic_write_bytes(
                current_target,
                new_bytes,
                guarded=True,
                expected_before_bytes=preview.before_bytes,
                expected_before_mode=preview.before_mode,
                expected_before_token=preview.before_token,
                expected_before_acl_token=preview.before_acl_token,
            )
        except WorkspaceConflictError as exc:
            record["state"] = "conflict"
            if persist_conflict_snapshot:
                saved = self._persist_conflict_snapshot(backup_path, record, exc)
            else:
                try:
                    self._atomic_write_json(backup_path, record)
                    saved = True
                except (OSError, WorkspaceError):
                    saved = False
            if persist_conflict_snapshot:
                snapshot_status = (
                    f"冲突快照已保留在备份 {backup_id}。"
                    if saved
                    else f"冲突快照写入失败；原始备份 {backup_id} 仍保留。"
                )
            else:
                snapshot_status = (
                    f"原始备份 {backup_id} 已保留，竞争版本内容未写入日志。"
                    if saved
                    else f"冲突状态日志写入失败；原始备份 {backup_id} 仍保留。"
                )
            raise WorkspaceConflictError(
                f"检测到并发文件变化，目标未被替换；{snapshot_status}",
                current_bytes=exc.current_bytes,
                current_mode=exc.current_mode,
                current_acl_token=exc.current_acl_token,
            ) from None
        except (WorkspaceError, ProviderCancelled):
            try:
                self._unlink_workspace_file(backup_path)
            except (OSError, WorkspaceError):
                pass
            raise
        record["state"] = "committed"
        if os.name == "nt":
            try:
                after_acl_token = self._windows_dacl_token(current_target)
                record["after_acl_sha256"] = self._acl_sha256(after_acl_token)
                record["after_acl_permissions_sha256"] = self._acl_permissions_sha256(after_acl_token)
            except WorkspaceError:
                # Existing-file copy already verified these access semantics.
                # New files without readable ACL readback stay unverified and
                # undo rejects them rather than assuming their permissions.
                if not preview.existed:
                    record["after_acl_sha256"] = None
                    record["after_acl_permissions_sha256"] = None
        try:
            self._atomic_write_json(backup_path, record)
        except (OSError, WorkspaceError):
            # The pending manifest is enough to identify the committed version by
            # after_sha256 if the process or filesystem failed during this update.
            pass
        return backup_id

    def _revalidate_write_preview(self, preview: WritePreview) -> Path:
        current_target = self._resolve_write_target(preview.path)
        if current_target != preview.target:
            raise WorkspaceError("审批后目标路径发生变化；为防止写错文件，本次操作已取消。")
        current_bytes, current_info = self._read_snapshot_state(
            current_target, validate_utf8=False
        )
        current_acl_token: bytes | None = None
        if os.name == "nt" and current_info is not None:
            try:
                current_acl_token = self._windows_dacl_token(current_target)
            except WorkspaceError:
                raise WorkspaceConflictError(
                    "无法复核目标 Windows DACL；请重新检查后再写入。",
                    current_bytes=current_bytes,
                    current_mode=stat.S_IMODE(current_info.st_mode),
                ) from None
        if not self._snapshot_matches(
            current_bytes,
            current_info,
            preview.before_bytes,
            preview.before_mode,
            preview.before_token,
            preview.before_acl_token,
            current_acl_token,
        ):
            raise WorkspaceConflictError(
                "审批预览后文件或权限信息发生变化；请重新请求并检查新的差异。",
                current_bytes=current_bytes,
                current_mode=stat.S_IMODE(current_info.st_mode) if current_info else None,
                current_acl_token=current_acl_token,
            )
        return current_target

    def _change_set_recovery_preview(
        self,
        change_set_id: str,
        record: dict[str, Any] | None = None,
    ) -> ChangeSetRecoveryPreview:
        record = self._load_change_set_record(change_set_id) if record is None else self._validate_change_set_record(record)
        if record["id"] != change_set_id:
            raise WorkspaceError("变更集编号不匹配。")
        backups: dict[str, dict[str, Any]] = {}
        for entry in record["files"]:
            matched = self._load_backups(backup_id=entry["backup_id"])
            if matched:
                backups[entry["backup_id"]] = matched[0][1]
        from .sessions import contains_likely_secret

        items: list[ChangeSetRecoveryItem] = []
        rendered = [
            f"变更集 {change_set_id} 恢复审阅；只恢复仍与本批写入版本匹配的文件。",
            "跨文件不是原子事务；未列为待恢复的冲突文件保持原样。",
        ]
        digest_items: list[dict[str, Any]] = []
        for index, entry in enumerate(record["files"]):
            backup = backups.get(entry["backup_id"])
            if backup is not None and not self._change_set_backup_matches_entry(
                change_set_id, index, entry, backup
            ):
                raise WorkspaceError("变更集备份关联无效；未执行恢复。")
            try:
                target = self._resolve_write_target(entry["path"])
                current_bytes, current_info = self._read_snapshot_state(target, validate_utf8=False)
                current_acl_token = (
                    self._windows_dacl_token(target)
                    if os.name == "nt" and current_info is not None
                    else None
                )
            except WorkspaceError:
                target = None
                current_bytes = None
                current_info = None
                current_acl_token = None
            current_sha256 = hashlib.sha256(current_bytes).hexdigest() if current_bytes is not None else None
            current_mode = stat.S_IMODE(current_info.st_mode) if current_info is not None else None
            before_bytes: bytes | None = None
            if backup is not None:
                encoded_before = backup.get("before_base64")
                if not isinstance(encoded_before, str):
                    raise WorkspaceError("变更集备份缺少原文件内容；未执行恢复。")
                try:
                    before_bytes = base64.b64decode(encoded_before, validate=True)
                    before_bytes.decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    raise WorkspaceError("变更集备份内容无效；未执行恢复。") from None
                if (
                    len(before_bytes) > READ_LIMIT_BYTES
                    or hashlib.sha256(before_bytes).hexdigest() != entry["before_sha256"]
                    or contains_likely_secret(before_bytes.decode("utf-8"))
                ):
                    raise WorkspaceError("变更集备份校验失败或含疑似凭据；未执行恢复。")

            status = "conflict"
            if current_bytes is not None and current_info is not None:
                if backup is None:
                    if (
                        current_sha256 == entry["before_sha256"]
                        and current_mode == entry["before_mode"]
                        and self._acl_sha256(current_acl_token) == entry["before_acl_sha256"]
                    ):
                        status = "not_applied"
                elif backup.get("state") != "conflict":
                    permissions_match = True
                    if os.name == "nt":
                        try:
                            permissions_match = (
                                self._acl_permissions_sha256(current_acl_token)
                                == backup.get("after_acl_permissions_sha256")
                            )
                        except WorkspaceError:
                            permissions_match = False
                    if (
                        current_sha256 == entry["before_sha256"]
                        and current_mode == entry["after_mode"]
                        and permissions_match
                        and (
                            backup.get("undone") is True
                            or entry["status"] in {"restoring", "restore_failed", "restored"}
                        )
                    ):
                        status = "restored"
                    elif backup.get("undone") is True:
                        status = "conflict"
                    else:
                        after_permissions_match = True
                        if os.name == "nt":
                            try:
                                after_permissions_match = (
                                    self._acl_permissions_sha256(current_acl_token)
                                    == entry["after_acl_permissions_sha256"]
                                )
                            except WorkspaceError:
                                after_permissions_match = False
                        before_permissions_match = (
                            self._acl_sha256(current_acl_token) == entry["before_acl_sha256"]
                        )
                        if (
                            current_sha256 == entry["after_sha256"]
                            and current_mode == entry["after_mode"]
                            and after_permissions_match
                        ):
                            status = "will_restore"
                        elif (
                            current_sha256 == entry["before_sha256"]
                            and current_mode == entry["before_mode"]
                            and before_permissions_match
                        ):
                            status = "not_applied"

            restore_bytes = before_bytes if status == "will_restore" else None
            if status == "will_restore":
                assert current_bytes is not None and current_info is not None and restore_bytes is not None
                try:
                    current_text = current_bytes.decode("utf-8")
                    restore_text = restore_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    raise WorkspaceError("待恢复文件不再是 UTF-8 文本；未执行恢复。") from None
                diff = self._diff(entry["path"], current_text, restore_text)
                rendered.append(f"\n[{index + 1}/{len(record['files'])}] {entry['path']}：待恢复\n{diff}")
            else:
                labels = {
                    "not_applied": "尚未写入，当前仍为审阅的原文件",
                    "failed": "本文件写入失败，当前仍为原文件",
                    "restored": "已恢复",
                    "conflict": "冲突；当前内容或权限与本批版本不符，保持不动",
                }
                rendered.append(f"\n[{index + 1}/{len(record['files'])}] {entry['path']}：{labels[status]}")
            items.append(
                ChangeSetRecoveryItem(
                    path=entry["path"],
                    status=status,
                    backup_id=entry["backup_id"],
                    before_sha256=entry["before_sha256"],
                    after_sha256=entry["after_sha256"],
                    current_bytes=current_bytes,
                    current_info=current_info,
                    current_acl_token=current_acl_token,
                    restore_bytes=restore_bytes,
                    backup_record=backup,
                )
            )
            digest_items.append(
                {
                    "index": index,
                    "status": status,
                    "sha256": current_sha256,
                    "mode": current_mode,
                    "stat_token": self._stat_token(current_info),
                    "acl_sha256": self._acl_sha256(current_acl_token),
                }
            )
        diff = "".join(rendered)
        if len(diff.encode("utf-8")) > CHANGE_SET_DIFF_LIMIT_BYTES:
            raise WorkspaceError("变更集恢复差异超过审阅上限；没有执行恢复。")
        digest = hashlib.sha256(
            json.dumps(digest_items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ChangeSetRecoveryPreview(change_set_id, record, tuple(items), diff, digest)

    @staticmethod
    def _change_set_result(record: dict[str, Any], prefix: str) -> str:
        labels = {
            "pending": "未开始",
            "applying": "处理中",
            "committed": "已改",
            "failed": "失败，未确认写入",
            "restoring": "恢复中",
            "restore_failed": "恢复未完成",
            "restored": "已恢复",
            "not_applied": "未改",
            "conflict": "冲突，未覆盖",
        }
        states = "；".join(
            f"{entry['path']}：{labels.get(entry['status'], '状态未知')}"
            for entry in record["files"]
        )
        return f"{prefix} 变更集 {record['id']}；{states}。"

    def _mark_change_set_backup_undone(self, backup_id: str) -> None:
        backup_dir = self._backup_dir(create=False)
        if backup_dir is None:
            raise WorkspaceError("变更集原始备份目录缺失。")
        backup_path = backup_dir / f"{backup_id}.json"
        records = self._load_backups(backup_id=backup_id)
        if not records:
            raise WorkspaceError("变更集原始备份无法读取。")
        backup_path, backup = records[0]
        backup["undone"] = True
        backup["undone_ns"] = time.time_ns()
        self._atomic_write_json(backup_path, backup)

    def _execute_change_set_restoration(
        self,
        preview: ChangeSetRecoveryPreview,
        *,
        explicit: bool,
        cancellation_event: Any | None = None,
    ) -> str:
        record = preview.record
        fresh = self._change_set_recovery_preview(preview.change_set_id, record)
        if fresh.digest != preview.digest:
            return self._change_set_result(record, "恢复预览期间文件状态发生变化，整批恢复已取消；请重新审阅。")
        candidates = [item for item in fresh.items if item.status == "will_restore"]
        has_reconciliations = any(
            item.status == "restored"
            and item.backup_record is not None
            and item.backup_record.get("undone") is not True
            for item in fresh.items
        )
        if not candidates and not has_reconciliations:
            for index, item in enumerate(fresh.items):
                record["files"][index]["status"] = item.status
            complete = all(item.status in {"restored", "not_applied"} for item in fresh.items)
            record["state"] = (
                ("recovered" if explicit else "rolled_back")
                if complete
                else "recovery_required"
            )
            try:
                self._write_change_set_record(record)
            except WorkspaceError:
                record["state"] = "recovery_required"
                return self._change_set_result(record, "没有文件可安全恢复，且无法更新状态日志。")
            return self._change_set_result(
                record,
                "所有文件均未应用或已恢复；没有文件内容写入。"
                if complete
                else "没有仍匹配本批写入版本的文件可恢复；冲突文件保持不动。",
            )

        record["state"] = "recovery_in_progress" if explicit else "rolling_back"
        for index, item in enumerate(fresh.items):
            record["files"][index]["status"] = (
                "restoring" if item.status == "will_restore" else item.status
            )
        try:
            self._write_change_set_record(record)
        except WorkspaceError:
            return self._change_set_result(record, "无法更新变更集恢复日志，未开始恢复。")

        for index, item in enumerate(fresh.items):
            if (
                item.status == "restored"
                and item.backup_record is not None
                and item.backup_record.get("undone") is not True
            ):
                try:
                    self._mark_change_set_backup_undone(item.backup_id)
                    record["files"][index]["status"] = "restored"
                    self._write_change_set_record(record)
                except WorkspaceError:
                    record["files"][index]["status"] = "restore_failed"
                    record["state"] = "recovery_required"
                    try:
                        self._write_change_set_record(record)
                    except WorkspaceError:
                        pass
                    return self._change_set_result(record, "已恢复文件的备份标记无法更新；停止后续文件操作。")

        stop_reason: str | None = None
        for item in reversed(fresh.items):
            index = next(i for i, candidate in enumerate(fresh.items) if candidate.path == item.path)
            entry = record["files"][index]
            if item.status != "will_restore":
                entry["status"] = item.status
                continue
            if explicit and cancellation_event is not None and cancellation_event.is_set():
                entry["status"] = "restore_failed"
                stop_reason = "用户取消了恢复；未开始的文件保持原样。"
                try:
                    record["state"] = "recovery_required"
                    self._write_change_set_record(record)
                except WorkspaceError:
                    pass
                break
            try:
                if item.current_bytes is None or item.current_info is None or item.restore_bytes is None:
                    raise WorkspaceConflictError(
                        "恢复预览缺少完整文件版本；目标未修改。",
                        current_bytes=item.current_bytes,
                        current_mode=(
                            stat.S_IMODE(item.current_info.st_mode)
                            if item.current_info is not None else None
                        ),
                        current_acl_token=item.current_acl_token,
                    )
                target = self._resolve_write_target(item.path)
                self._atomic_write_bytes(
                    target,
                    item.restore_bytes,
                    guarded=True,
                    expected_before_bytes=item.current_bytes,
                    expected_before_mode=stat.S_IMODE(item.current_info.st_mode),
                    expected_before_token=self._stat_token(item.current_info),
                    expected_before_acl_token=item.current_acl_token,
                )
            except WorkspaceConflictError:
                entry["status"] = "conflict"
            except WorkspaceError:
                entry["status"] = "restore_failed"
                stop_reason = "某文件恢复未完成；已停止后续恢复，请重新审阅当前状态。"
            else:
                try:
                    self._mark_change_set_backup_undone(item.backup_id)
                    entry["status"] = "restored"
                except WorkspaceError:
                    entry["status"] = "restore_failed"
                    stop_reason = "文件内容已恢复，但备份标记未完成；已停止后续恢复。"
            if stop_reason:
                record["state"] = "recovery_required"
            try:
                self._write_change_set_record(record)
            except WorkspaceError:
                entry["status"] = "restore_failed"
                record["state"] = "recovery_required"
                stop_reason = "恢复后无法更新变更集日志；停止后续文件操作。"
                break
            if stop_reason:
                record["state"] = "recovery_required"
                break

        complete = all(item["status"] in {"restored", "not_applied"} for item in record["files"])
        if complete:
            record["state"] = "recovered" if explicit else "rolled_back"
        else:
            record["state"] = "recovery_required"
        try:
            self._write_change_set_record(record)
        except WorkspaceError:
            record["state"] = "recovery_required"
            stop_reason = stop_reason or "恢复结果日志未能完整更新；请在下次启动时重新检查状态。"
        if stop_reason:
            return self._change_set_result(record, stop_reason)
        if complete:
            return self._change_set_result(
                record,
                "变更集已完成逆序恢复；没有跨文件原子事务保证。" if not explicit
                else "变更集恢复完成；没有跨文件原子事务保证。",
            )
        return self._change_set_result(record, "部分文件恢复完成，冲突或失败文件保持未覆盖；需要再次审阅恢复状态。")

    def apply_change_set(
        self,
        changes: Any,
        *,
        approve_action: ApprovalCallback,
        cancellation_event: Any | None = None,
    ) -> str:
        preview = self.preview_change_set(changes)
        change_set_id = uuid.uuid4().hex
        created_ns = time.time_ns()
        backup_ids = [f"{time.time_ns()}-{uuid.uuid4().hex}" for _ in preview.changes]
        review = (
            f"变更集审阅编号：{change_set_id}\n"
            f"整批预检：{len(preview.changes)} 个现有 UTF-8 文件；原文件备份合计 {preview.backup_bytes} bytes；"
            f"输入 {preview.payload_bytes} bytes。\n"
            f"完整差异指纹：{preview.digest}\n"
            + preview.diff
            + "\n只会审批并尝试应用上列全部文件；任一文件变化都会中止后续写入并尝试逆序恢复。"
            "跨文件不是原子事务；每个文件仍只有最后版本检查后的窄竞态窗口。"
        )
        if _safe_terminal_display_length(review) > CHANGE_SET_DIFF_LIMIT_BYTES * 2:
            raise WorkspaceError("变更集审批预览超过显示上限；整批未审批、未写入。")
        if not approve_action(
            ActionRequest(
                operation="change_set",
                path=change_set_id,
                diff=review,
                source="tool",
            )
        ):
            return f"用户拒绝整个变更集 {change_set_id}；没有写入文件或创建备份。"
        _raise_if_cancelled(cancellation_event)

        # Revalidate every member before creating any backup or replacing a file.
        for item in preview.changes:
            self._revalidate_write_preview(item)
        self._prune_change_set_logs()

        files: list[dict[str, Any]] = []
        for index, (item, before_sha256, backup_id) in enumerate(
            zip(preview.changes, preview.file_sha256, backup_ids), 1
        ):
            after_bytes = item.new_content.encode("utf-8")
            files.append(
                {
                    "path": item.canonical_path,
                    "before_sha256": before_sha256,
                    "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
                    "before_mode": item.before_mode,
                    "after_mode": item.before_mode & 0o777 if item.before_mode is not None else 0,
                    "before_acl_sha256": self._acl_sha256(item.before_acl_token),
                    "after_acl_permissions_sha256": self._acl_permissions_sha256(item.before_acl_token),
                    "backup_id": backup_id,
                    "status": "pending",
                }
            )
        record: dict[str, Any] = {
            "version": 1,
            "id": change_set_id,
            "created_ns": created_ns,
            "updated_ns": created_ns,
            "state": "applying",
            "files": files,
        }
        self._write_change_set_record(record)

        try:
            for index, (item, entry) in enumerate(zip(preview.changes, record["files"]), 1):
                _raise_if_cancelled(cancellation_event)
                entry["status"] = "applying"
                self._write_change_set_record(record)
                self.commit_write(
                    item,
                    cancellation_event=cancellation_event,
                    backup_id=entry["backup_id"],
                    change_set_id=change_set_id,
                    change_set_index=index,
                    persist_conflict_snapshot=False,
                )
                entry["status"] = "committed"
                self._write_change_set_record(record)
            record["state"] = "completed"
            self._write_change_set_record(record)
            return self._change_set_result(record, "已应用全部文件变更")
        except ProviderCancelled:
            record["state"] = "rolling_back"
            try:
                self._write_change_set_record(record)
            except WorkspaceError:
                pass
            try:
                recovery = self._change_set_recovery_preview(change_set_id, record)
                result = self._execute_change_set_restoration(recovery, explicit=False)
            except WorkspaceError:
                record["state"] = "recovery_required"
                try:
                    self._write_change_set_record(record)
                except WorkspaceError:
                    pass
                result = self._change_set_result(record, "取消后无法安全完成自动恢复")
            return result
        except WorkspaceError:
            if record["files"]:
                for entry in record["files"]:
                    if entry["status"] == "applying":
                        entry["status"] = "failed"
                        break
            record["state"] = "rolling_back"
            try:
                self._write_change_set_record(record)
            except WorkspaceError:
                pass
            try:
                recovery = self._change_set_recovery_preview(change_set_id, record)
                return self._execute_change_set_restoration(recovery, explicit=False)
            except WorkspaceError:
                record["state"] = "recovery_required"
                try:
                    self._write_change_set_record(record)
                except WorkspaceError:
                    pass
                return self._change_set_result(record, "变更集失败，自动恢复无法安全完成")
        except BaseException:
            record["state"] = "rolling_back"
            try:
                self._write_change_set_record(record)
                recovery = self._change_set_recovery_preview(change_set_id, record)
                self._execute_change_set_restoration(recovery, explicit=False)
            except BaseException:
                record["state"] = "recovery_required"
                try:
                    self._write_change_set_record(record)
                except BaseException:
                    pass
            raise

    def recover_change_set(
        self,
        change_set_id: str,
        *,
        approve_action: ApprovalCallback,
        cancellation_event: Any | None = None,
    ) -> str:
        record = self._load_change_set_record(change_set_id)
        if record["state"] in {"completed", "rolled_back", "recovered"}:
            return self._change_set_result(record, "该变更集没有待恢复状态")
        preview = self._change_set_recovery_preview(change_set_id, record)
        if not any(item.status == "will_restore" for item in preview.items):
            return self._execute_change_set_restoration(preview, explicit=True)
        if not approve_action(
            ActionRequest(
                operation="change_set_recovery",
                path=change_set_id,
                diff=preview.diff,
                source="command",
            )
        ):
            return self._change_set_result(record, "用户拒绝本次变更集恢复；文件未修改")
        _raise_if_cancelled(cancellation_event)
        return self._execute_change_set_restoration(
            preview,
            explicit=True,
            cancellation_event=cancellation_event,
        )

    @staticmethod
    def _change_set_backup_matches_entry(
        change_set_id: str,
        index: int,
        entry: dict[str, Any],
        backup: dict[str, Any],
    ) -> bool:
        return (
            backup.get("change_set_id") == change_set_id
            and backup.get("change_set_index") == index + 1
            and Workspace._change_set_backup_payload_matches_entry(entry, backup)
        )

    @staticmethod
    def _change_set_backup_payload_matches_entry(
        entry: dict[str, Any],
        backup: dict[str, Any],
    ) -> bool:
        encoded_before = backup.get("before_base64")
        if not isinstance(encoded_before, str):
            return False
        try:
            before_bytes = base64.b64decode(encoded_before, validate=True)
        except (ValueError, binascii.Error):
            return False
        return (
            backup.get("path") == entry["path"]
            and backup.get("before_exists") is True
            and hashlib.sha256(before_bytes).hexdigest() == entry["before_sha256"]
            and backup.get("state") in {"pending", "committed", "conflict"}
            and backup.get("before_mode") == entry["before_mode"]
            and backup.get("after_mode") == entry["after_mode"]
            and backup.get("before_acl_sha256") == entry["before_acl_sha256"]
            and backup.get("after_acl_permissions_sha256") == entry["after_acl_permissions_sha256"]
            and backup.get("after_sha256") == entry["after_sha256"]
        )

    def _safe_change_set_evidence_path(self, value: Any) -> str:
        from .sessions import contains_likely_secret

        if (
            not isinstance(value, str)
            or not value
            or contains_likely_secret(value)
            or any(
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
                for char in value
            )
        ):
            return "（路径已省略）"
        try:
            target = self._resolve_write_target(value)
            canonical = target.relative_to(self.root).as_posix()
        except (ValueError, WorkspaceError):
            return "（路径无法安全核对）"
        return canonical if canonical == value else "（路径规范已变化）"

    @staticmethod
    def _backup_error_can_be_reported_as_uncertain(exc: WorkspaceError) -> bool:
        message = str(exc)
        return message.startswith((
            "撤销备份格式无效",
            "冲突快照格式无效",
            "撤销备份超过安全大小上限",
            "无法读取撤销备份：",
        ))

    def _inspect_change_set_backup_evidence(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        journals = {record["id"]: record for record in records}
        expected: dict[str, tuple[str, int, dict[str, Any]]] = {}
        for record in records:
            for index, entry in enumerate(record["files"]):
                expected[entry["backup_id"]] = (record["id"], index, entry)

        result: dict[str, Any] = {
            "orphans": {},
            "unverified": [],
            "mismatches": [],
            "scan_count": 0,
            "scan_limited": False,
        }
        backup_dir = self._backup_dir(create=False)
        if backup_dir is not None:
            try:
                with os.scandir(backup_dir) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".json"):
                            continue
                        if result["scan_count"] >= CHANGE_SET_BACKUP_SCAN_LIMIT:
                            result["scan_limited"] = True
                            break
                        result["scan_count"] += 1
                        match = re.fullmatch(r"([0-9]+-[0-9a-f]{32})\.json", entry.name)
                        if match is None:
                            result["unverified"].append(
                                {"backup_id": None, "batch_id": None, "reason": "名称无效"}
                            )
                            continue
                        backup_id = match.group(1)
                        path = Path(entry.path)
                        try:
                            info = path.lstat()
                        except FileNotFoundError:
                            result["unverified"].append(
                                {"backup_id": backup_id, "batch_id": None, "reason": "扫描期间消失"}
                            )
                            continue
                        except OSError:
                            result["unverified"].append(
                                {"backup_id": backup_id, "batch_id": None, "reason": "无法检查"}
                            )
                            continue
                        if (
                            self._is_reparse_point(path)
                            or not stat.S_ISREG(info.st_mode)
                            or getattr(info, "st_nlink", 1) != 1
                        ):
                            raise WorkspaceError("拒绝扫描链接或非普通文件形式的撤销备份。")
                        if info.st_size > WRITE_LIMIT_BYTES * 4:
                            result["unverified"].append(
                                {"backup_id": backup_id, "batch_id": None, "reason": "超过大小上限"}
                            )
                            continue
                        try:
                            loaded = self._load_backups(backup_id=backup_id)
                        except WorkspaceError as exc:
                            if not self._backup_error_can_be_reported_as_uncertain(exc):
                                raise
                            expected_item = expected.get(backup_id)
                            result["unverified"].append(
                                {
                                    "backup_id": backup_id,
                                    "batch_id": expected_item[0] if expected_item else None,
                                    "reason": "格式损坏或无法读取",
                                }
                            )
                            continue
                        if not loaded:
                            result["unverified"].append(
                                {"backup_id": backup_id, "batch_id": None, "reason": "扫描期间消失"}
                            )
                            continue
                        backup = loaded[0][1]
                        batch_id = backup.get("change_set_id")
                        if batch_id is None:
                            continue
                        if batch_id not in journals:
                            result["orphans"].setdefault(batch_id, []).append(backup)
                            continue
                        expected_item = expected.get(backup_id)
                        if expected_item is None or expected_item[0] != batch_id or not self._change_set_backup_matches_entry(
                            batch_id, expected_item[1], expected_item[2], backup
                        ):
                            result["mismatches"].append(
                                {
                                    "batch_id": batch_id,
                                    "backup_id": backup_id,
                                    "entry_index": backup.get("change_set_index"),
                                    "reason": "备份关联与日志不一致",
                                }
                            )
            except WorkspaceError:
                raise
            except OSError as exc:
                raise WorkspaceError(f"无法完整扫描撤销备份：{type(exc).__name__}") from None

        # Check every journal-referenced backup directly, even if the bounded
        # directory scan stopped before reaching that file.
        for record in records:
            for index, item in enumerate(record["files"]):
                backup_id = item["backup_id"]
                try:
                    loaded = self._load_backups(backup_id=backup_id)
                except WorkspaceError as exc:
                    if not self._backup_error_can_be_reported_as_uncertain(exc):
                        raise
                    loaded = []
                    issue = "日志引用的备份格式损坏或无法读取"
                else:
                    issue = ""
                if not loaded:
                    if item["status"] in {"committed", "restoring", "restore_failed", "restored"}:
                        result["mismatches"].append(
                            {
                                "batch_id": record["id"],
                                "backup_id": backup_id,
                                "entry_index": index + 1,
                                "reason": issue or "日志引用的备份缺失",
                            }
                        )
                    continue
                backup = loaded[0][1]
                if not self._change_set_backup_matches_entry(record["id"], index, item, backup):
                    if not any(
                        mismatch["batch_id"] == record["id"]
                        and mismatch["backup_id"] == backup_id
                        for mismatch in result["mismatches"]
                    ):
                        result["mismatches"].append(
                            {
                                "batch_id": record["id"],
                                "backup_id": backup_id,
                                "entry_index": index + 1,
                                "reason": "日志引用的备份关联不一致",
                            }
                        )
        return result

    def _describe_change_set_evidence(self, *, detailed: bool) -> str:
        records = self._list_change_set_records()
        incomplete = [
            record
            for record in records
            if record["state"] not in {"completed", "rolled_back", "recovered"}
        ]
        evidence = self._inspect_change_set_backup_evidence(records)
        orphan_batches = evidence["orphans"]
        has_evidence_warning = self._change_set_evidence_has_warnings(evidence)
        if not incomplete and not has_evidence_warning and not detailed:
            return "没有未完成的多文件变更集。"

        rendered: list[str] = []
        if detailed:
            rendered.append(
                f"变更集证据检查：日志 {len(records)} 条；未完成 {len(incomplete)} 条；"
                f"撤销备份扫描 {evidence['scan_count']} 项。"
            )
        if incomplete:
            rendered.append(
                f"发现 {len(incomplete)} 个未完成变更集；启动时不会自动恢复或重放审批。"
            )
            file_status_labels = {
                "pending": "未开始",
                "applying": "处理中",
                "committed": "已修改",
                "failed": "失败，未确认写入",
                "restoring": "恢复中",
                "restore_failed": "恢复未完成",
                "restored": "已恢复",
                "not_applied": "未修改",
                "conflict": "冲突，未覆盖",
            }
            for record in incomplete[:CHANGE_SET_EVIDENCE_DISPLAY_LIMIT]:
                rendered.append(f"{record['id']}：{record['state']}")
                rendered.extend(
                    f"  {entry['path']}（{file_status_labels.get(entry['status'], '状态未知')}）"
                    for entry in record["files"]
                )
            if len(incomplete) > CHANGE_SET_EVIDENCE_DISPLAY_LIMIT:
                rendered.append(f"另有 {len(incomplete) - CHANGE_SET_EVIDENCE_DISPLAY_LIMIT} 条未展开。")
            rendered.append("使用 /变更集 恢复 <编号> 可重新审阅文件状态；恢复仍需单独确认。")
        elif has_evidence_warning:
            rendered.append("没有可直接恢复的未完成日志；发现需要人工核对的批次存储证据。")

        if orphan_batches:
            rendered.append(
                f"发现 {len(orphan_batches)} 个没有对应批次日志的孤立批次备份；"
                "启动时不会自动恢复或重放审批；不能从备份重建授权。"
            )
            if detailed:
                for batch_id, backups in list(sorted(orphan_batches.items()))[:CHANGE_SET_EVIDENCE_DISPLAY_LIMIT]:
                    rendered.append(f"孤立批次 {batch_id}：{len(backups)} 个备份")
                    for backup in backups[:8]:
                        safe_path = self._safe_change_set_evidence_path(backup.get("path"))
                        rendered.append(
                            f"  备份 {backup['id']}；序号 {backup.get('change_set_index')}；"
                            f"状态 {backup.get('state', '未知')}；路径 {safe_path}"
                        )
                if len(orphan_batches) > CHANGE_SET_EVIDENCE_DISPLAY_LIMIT:
                    rendered.append(f"另有 {len(orphan_batches) - CHANGE_SET_EVIDENCE_DISPLAY_LIMIT} 个孤立批次未展开。")
        if evidence["mismatches"]:
            rendered.append(
                f"有 {len(evidence['mismatches'])} 项日志/备份关联缺失或矛盾；"
                "不能据此判定已完成或无待恢复项，也不会自动恢复。"
            )
            if detailed:
                for item in evidence["mismatches"][:CHANGE_SET_EVIDENCE_DISPLAY_LIMIT]:
                    rendered.append(
                        f"批次 {item['batch_id']}；序号 {item['entry_index']}；"
                        f"备份 {item['backup_id']}：{item['reason']}"
                    )
        if evidence["unverified"]:
            rendered.append(
                f"有 {len(evidence['unverified'])} 个备份条目无法校验；"
                "可能包含无法识别的批次证据，不能判定为无孤立项。"
            )
            if detailed:
                for item in evidence["unverified"][:CHANGE_SET_EVIDENCE_DISPLAY_LIMIT]:
                    backup_id = item["backup_id"] or "（文件名无效）"
                    batch_id = item["batch_id"] or "（批次未知）"
                    rendered.append(
                        f"备份 {backup_id}；批次 {batch_id}：{item['reason']}"
                    )
        if evidence["scan_limited"]:
            rendered.append(
                f"撤销备份扫描达到 {CHANGE_SET_BACKUP_SCAN_LIMIT} 项上限；"
                "未扫描部分可能还有孤立证据，不能据此判定为无待办。"
            )
        if has_evidence_warning:
            rendered.append(
                "使用 /变更集 证据 查看安全可显示的本地元数据；核对 .harness/change-sets/、"
                ".harness/backups/ 与实际文件。孤立备份不会生成恢复命令或授权。"
            )
        if detailed and not has_evidence_warning:
            rendered.append("未发现孤立、损坏或矛盾的批次备份证据；没有执行任何恢复。")
        return "\n".join(rendered)

    def describe_incomplete_change_sets(self) -> str:
        return self._describe_change_set_evidence(detailed=False)

    def describe_change_set_evidence(self) -> str:
        return self._describe_change_set_evidence(detailed=True)

    def _load_backups(self, *, backup_id: str | None = None) -> list[tuple[Path, dict[str, Any]]]:
        backup_dir = self._backup_dir(create=False)
        if backup_dir is None:
            return []
        records: list[tuple[Path, dict[str, Any]]] = []
        try:
            if backup_id is None:
                paths = backup_dir.glob("*.json")
            else:
                if re.fullmatch(r"[0-9]+-[0-9a-f]{32}", backup_id) is None:
                    raise WorkspaceError("撤销备份编号无效。")
                candidate = backup_dir / f"{backup_id}.json"
                try:
                    candidate.lstat()
                except FileNotFoundError:
                    return []
                paths = (candidate,)
            for path in paths:
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if (
                    self._is_reparse_point(path)
                    or not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_nlink", 1) != 1
                ):
                    raise WorkspaceError(f"拒绝读取链接或非普通文件形式的撤销备份：{path.name}")
                if info.st_size > WRITE_LIMIT_BYTES * 4:
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
                    or data.get("state", "committed") not in ("pending", "committed", "conflict")
                    or (
                        "undo_conflict" in data
                        and not isinstance(data.get("undo_conflict"), bool)
                    )
                    or any(
                        key in data
                        and data.get(key) is not None
                        and (
                            not isinstance(data.get(key), str)
                            or re.fullmatch(r"[0-9a-f]{64}", data.get(key, "")) is None
                        )
                        for key in ("before_acl_sha256", "after_acl_sha256", "after_acl_permissions_sha256")
                    )
                    or (
                        "before_mode" in data
                        and data.get("before_mode") is not None
                        and (
                            type(data.get("before_mode")) is not int
                            or not 0 <= data["before_mode"] <= 0o7777
                        )
                    )
                    or (
                        "after_mode" in data
                        and data.get("after_mode") is not None
                        and (
                            type(data["after_mode"]) is not int
                            or not 0 <= data["after_mode"] <= 0o777
                        )
                    )
                    or (
                        "change_set_id" in data
                        and (
                            not isinstance(data.get("change_set_id"), str)
                            or re.fullmatch(r"[0-9a-f]{32}", data.get("change_set_id", "")) is None
                            or type(data.get("change_set_index")) is not int
                            or not 1 <= data["change_set_index"] <= CHANGE_SET_MAX_FILES
                        )
                    )
                    or (
                        "change_set_index" in data
                        and "change_set_id" not in data
                    )
                    or (
                        data.get("before_exists")
                        and not isinstance(data.get("before_base64"), str)
                    )
                    or (not data.get("before_exists") and data.get("before_base64") is not None)
                    or (
                        data.get("conflict_snapshot") is not None
                        and not isinstance(data.get("conflict_snapshot"), dict)
                    )
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
                conflict = data.get("conflict_snapshot")
                if conflict is not None:
                    if (
                        not isinstance(conflict.get("existed"), bool)
                        or type(conflict.get("captured_ns")) is not int
                        or conflict.get("captured_ns", -1) < 0
                        or (
                            conflict.get("acl_sha256") is not None
                            and (
                                not isinstance(conflict.get("acl_sha256"), str)
                                or re.fullmatch(
                                    r"[0-9a-f]{64}", conflict.get("acl_sha256", "")
                                ) is None
                            )
                        )
                        or (
                            conflict.get("mode") is not None
                            and (
                                type(conflict.get("mode")) is not int
                                or not 0 <= conflict["mode"] <= 0o7777
                            )
                        )
                    ):
                        raise WorkspaceError(f"冲突快照格式无效：{path.name}")
                    if conflict["existed"]:
                        if not isinstance(conflict.get("bytes_base64"), str):
                            raise WorkspaceError(f"冲突快照格式无效：{path.name}")
                        try:
                            conflict_bytes = base64.b64decode(conflict["bytes_base64"], validate=True)
                        except (KeyError, ValueError):
                            raise WorkspaceError(f"冲突快照格式无效：{path.name}") from None
                        if (
                            len(conflict_bytes) > READ_LIMIT_BYTES
                            or hashlib.sha256(conflict_bytes).hexdigest() != conflict.get("sha256")
                        ):
                            raise WorkspaceError(f"冲突快照格式无效：{path.name}")
                    elif conflict.get("bytes_base64") is not None or conflict.get("sha256") is not None:
                        raise WorkspaceError(f"冲突快照格式无效：{path.name}")
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
            record = entry[1]
            if record.get("undone", False) or record.get("state", "committed") == "conflict":
                continue
            if record.get("state") == "pending":
                relative_path = record.get("path")
                target_hash = record.get("after_sha256")
                try:
                    pending_target = self.resolve(relative_path) if isinstance(relative_path, str) else None
                    pending_bytes = self._read_snapshot(pending_target) if pending_target is not None else None
                except WorkspaceError:
                    pending_bytes = None
                if (
                    pending_bytes is None
                    or not isinstance(target_hash, str)
                    or hashlib.sha256(pending_bytes).hexdigest() != target_hash
                ):
                    continue
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
        try:
            current_bytes, current_info = self._read_snapshot_state(
                target, validate_utf8=False
            )
        except WorkspaceConflictError as exc:
            record["undo_conflict"] = True
            saved = self._persist_conflict_snapshot(backup_path, record, exc)
            suffix = "冲突快照已保存。" if saved else "冲突快照写入失败；原始备份仍保留。"
            raise WorkspaceConflictError(
                f"撤销读取期间文件发生变化；未恢复内容。{suffix}",
                current_bytes=exc.current_bytes,
                current_mode=exc.current_mode,
                current_acl_token=exc.current_acl_token,
            ) from None
        expected_hash = record.get("after_sha256")
        current_acl_token: bytes | None = None
        if os.name == "nt" and current_info is not None:
            try:
                current_acl_token = self._windows_dacl_token(target)
            except WorkspaceError:
                conflict = WorkspaceConflictError(
                    "无法读取当前 Windows DACL；撤销未执行。",
                    current_bytes=current_bytes,
                    current_mode=stat.S_IMODE(current_info.st_mode),
                )
                record["undo_conflict"] = True
                saved = self._persist_conflict_snapshot(backup_path, record, conflict)
                suffix = "冲突快照已保存。" if saved else "冲突快照写入失败；原始备份仍保留。"
                raise WorkspaceConflictError(
                    f"无法核验当前 Windows DACL；撤销未执行。{suffix}",
                    current_bytes=current_bytes,
                    current_mode=stat.S_IMODE(current_info.st_mode),
                ) from None
        expected_acl_hash = record.get("after_acl_sha256")
        content_mismatch = (
            current_bytes is None
            or not isinstance(expected_hash, str)
            or hashlib.sha256(current_bytes).hexdigest() != expected_hash
        )
        if os.name == "nt" and "after_acl_permissions_sha256" in record:
            expected_permissions_hash = record.get("after_acl_permissions_sha256")
            try:
                current_permissions_hash = self._acl_permissions_sha256(current_acl_token)
            except WorkspaceError:
                current_permissions_hash = None
            # Harness write/undo can normalize ACL bookkeeping between byte
            # versions. Compare persistent access semantics here, then retain
            # raw snapshots for every approval/final race check below.
            acl_mismatch = (
                not isinstance(expected_permissions_hash, str)
                or current_permissions_hash != expected_permissions_hash
            )
        else:
            # Old records contain only a raw digest. Keep their conservative
            # behavior; never infer permissions from an unavailable old ACL.
            acl_mismatch = (
                isinstance(expected_acl_hash, str)
                and self._acl_sha256(current_acl_token) != expected_acl_hash
            )
        if content_mismatch or acl_mismatch:
            conflict = WorkspaceConflictError(
                "当前文件内容或权限状态与 Harness 最近一次写入不匹配，检测到版本发生变化。",
                current_bytes=current_bytes,
                current_mode=stat.S_IMODE(current_info.st_mode) if current_info else None,
                current_acl_token=current_acl_token,
            )
            record["undo_conflict"] = True
            saved = self._persist_conflict_snapshot(backup_path, record, conflict)
            if saved:
                suffix = f"竞争版本快照已保存在备份 {backup_id}。"
            else:
                suffix = f"快照写入失败；原始备份 {backup_id} 仍保留。"
            raise WorkspaceConflictError(
                f"撤销已阻止：{str(conflict)}未覆盖当前文件。{suffix}",
                current_bytes=current_bytes,
                current_mode=stat.S_IMODE(current_info.st_mode) if current_info else None,
                current_acl_token=current_acl_token,
            ) from None

        before_exists = record.get("before_exists")
        old_base64 = record.get("before_base64")
        if not isinstance(before_exists, bool) or (before_exists and not isinstance(old_base64, str)):
            raise WorkspaceError("撤销备份缺少可恢复的文件内容。")
        try:
            restore_bytes = base64.b64decode(old_base64, validate=True) if before_exists else b""
            restore_content = restore_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise WorkspaceError("撤销备份内容无效。") from None
        assert current_bytes is not None and current_info is not None
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
        latest_bytes, latest_info = self._read_snapshot_state(
            current_target, validate_utf8=False
        )
        latest_acl_token: bytes | None = None
        if os.name == "nt" and latest_info is not None:
            try:
                latest_acl_token = self._windows_dacl_token(current_target)
            except WorkspaceError:
                latest_conflict = WorkspaceConflictError(
                    "无法复核审批后的 Windows DACL；未恢复文件。",
                    current_bytes=latest_bytes,
                    current_mode=stat.S_IMODE(latest_info.st_mode),
                )
                record["undo_conflict"] = True
                saved = self._persist_conflict_snapshot(
                    backup_path, record, latest_conflict
                )
                suffix = "冲突快照已保存。" if saved else "冲突快照写入失败；原始备份仍保留。"
                raise WorkspaceConflictError(
                    f"无法复核审批后的 Windows DACL；未恢复文件。{suffix}",
                    current_bytes=latest_bytes,
                    current_mode=stat.S_IMODE(latest_info.st_mode),
                ) from None
        if current_target != target or not self._snapshot_matches(
            latest_bytes,
            latest_info,
            current_bytes,
            stat.S_IMODE(current_info.st_mode),
            self._stat_token(current_info),
            current_acl_token,
            latest_acl_token,
        ):
            conflict = WorkspaceConflictError(
                "审批后文件或权限信息发生变化；为防止覆盖新内容，本次撤销已取消。",
                current_bytes=latest_bytes,
                current_mode=stat.S_IMODE(latest_info.st_mode) if latest_info else None,
                current_acl_token=latest_acl_token,
            )
            record["undo_conflict"] = True
            saved = self._persist_conflict_snapshot(backup_path, record, conflict)
            suffix = "竞争版本快照已保存。" if saved else "快照写入失败；原始备份仍保留。"
            raise WorkspaceConflictError(f"{conflict} {suffix}",
                current_bytes=latest_bytes,
                current_mode=conflict.current_mode,
                current_acl_token=latest_acl_token,
            ) from None
        try:
            if before_exists:
                self._atomic_write_bytes(
                    target,
                    restore_bytes,
                    guarded=True,
                    expected_before_bytes=current_bytes,
                    expected_before_mode=stat.S_IMODE(current_info.st_mode),
                    expected_before_token=self._stat_token(current_info),
                    expected_before_acl_token=current_acl_token,
                )
            else:
                self._unlink_workspace_file(
                    target,
                    guarded=True,
                    expected_before_bytes=current_bytes,
                    expected_before_mode=stat.S_IMODE(current_info.st_mode),
                    expected_before_token=self._stat_token(current_info),
                    expected_before_acl_token=current_acl_token,
                )
        except WorkspaceConflictError as exc:
            record["undo_conflict"] = True
            saved = self._persist_conflict_snapshot(backup_path, record, exc)
            suffix = (
                f"竞争版本快照已保存至备份 {backup_id}。"
                if saved
                else f"快照写入失败；原始备份 {backup_id} 仍保留。"
            )
            raise WorkspaceConflictError(
                f"撤销时发现并发文件或 DACL 变化，目标未被替换或删除；{suffix}",
                current_bytes=exc.current_bytes,
                current_mode=exc.current_mode,
                current_acl_token=exc.current_acl_token,
            ) from None
        record["undo_conflict"] = False
        record["undone"] = True
        record["undone_ns"] = time.time_ns()
        self._atomic_write_json(backup_path, record)
        return f"已撤销写入并恢复备份 {backup_id}：{relative_path}"


def _validate_arguments(
    call: ToolCall,
    expected: set[str],
    *,
    required: set[str] | None = None,
) -> None:
    if not isinstance(call.arguments, dict):
        raise WorkspaceError("工具参数必须是 JSON 对象。")
    actual = set(call.arguments)
    if (required is None and actual != expected) or (
        required is not None and (not required <= actual or not actual <= expected)
    ):
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
                        "Run one exact user-owned Python or fixed-command test profile only when the user explicitly asks to run tests. "
                        "The user configured a Python interpreter and fixed framework or a direct executable and fixed arguments, plus the workspace cwd; never request or invent them. "
                        "The local harness asks for per-run approval and separately asks before sharing redacted output. "
                        "The model supplies only the profile ID; it cannot add or change the configured executable, arguments, or cwd."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "profile_id": {
                                "type": "string",
                                # Profile IDs are a set of allowed choices. Config
                                # file ordering is incidental and would churn the
                                # otherwise stable request prefix.
                                "enum": sorted(test_profile_ids, key=lambda item: (item.casefold(), item)),
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
    ask_user: Callable[[QuestionnaireRequest], QuestionnaireResult] | None = None,
    allow_questionnaire_session_save: bool = True,
) -> str:
    """Execute one validated tool; model output never grants local permission."""

    if call.name == "ask_user":
        if type(allow_questionnaire_session_save) is not bool:
            allow_questionnaire_session_save = False
        _raise_if_cancelled(cancellation_event)
        _validate_arguments(call, {"questions"})
        try:
            request = parse_questionnaire(call.arguments)
        except QuestionRequestError as exc:
            if exc.code == "credential_request":
                if emit_output is not None:
                    emit_output("已阻止通过提问收集凭据；请使用 /配置 或受支持的环境变量设置。")
                return questionnaire_result_json(QuestionnaireResult("blocked_credential"))
            return json.dumps(
                {"status": "invalid_request", "answers": [], "message": "问题格式无效或超出限制；没有收集回答。"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if not allow_questionnaire_session_save:
            request = QuestionnaireRequest(request.questions, save_allowed=False)
        if ask_user is None:
            return json.dumps(
                {"status": "unavailable", "answers": [], "message": "当前界面不支持交互式提问；没有收集回答。"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        _raise_if_cancelled(cancellation_event)
        try:
            result = ask_user(request)
            if not isinstance(result, QuestionnaireResult):
                raise QuestionRequestError("invalid_request")
            if result.status == "submitted":
                if (
                    type(result.save_with_session) is not bool
                    or not isinstance(result.answers, (tuple, list))
                    or len(result.answers) != len(request.questions)
                    or any(not isinstance(answer, UserQuestionAnswer) for answer in result.answers)
                    or tuple(answer.question_id for answer in result.answers)
                    != tuple(question.question_id for question in request.questions)
                ):
                    raise QuestionRequestError("invalid_request")
                normalized = validate_question_answers(
                    request,
                    tuple(answer.answer for answer in result.answers),
                )
                result = QuestionnaireResult(
                    "submitted",
                    normalized,
                    result.save_with_session and request.save_allowed,
                )
            elif result.status in {"cancelled", "blocked_credential"}:
                if result.answers or result.save_with_session:
                    raise QuestionRequestError("invalid_request")
            else:
                raise QuestionRequestError("invalid_request")
        except QuestionRequestError as exc:
            if exc.code == "credential_request":
                if emit_output is not None:
                    emit_output("已阻止提交疑似凭据；请使用 /配置 或受支持的环境变量设置。")
                return questionnaire_result_json(QuestionnaireResult("blocked_credential"))
            return json.dumps(
                {"status": "invalid_response", "answers": [], "message": "回答格式无效；没有发送回答。"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        _raise_if_cancelled(cancellation_event)
        if emit_output is not None:
            if result.status == "submitted":
                if result.save_with_session:
                    emit_output(
                        "已将本次用户回答交给当前模型。你已允许将问卷内容保存到工作区本地会话；"
                        "只有执行 /保存 时才会写入，疑似凭据仍不会保存。"
                    )
                else:
                    emit_output(
                        "已将本次用户回答交给当前模型。你选择仅本次使用；本轮及受其影响的后续对话不会进入会话存档，"
                        "恢复后可能需要重新提供背景。"
                    )
            elif result.status == "cancelled":
                emit_output("已取消提问；没有提交回答。")
        return questionnaire_result_json(result)

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

    if call.name == "search_text":
        _raise_if_cancelled(cancellation_event)
        _validate_arguments(
            call,
            {"query", "path", "glob"},
            required={"query"},
        )
        query = _string_argument(call, "query")
        path = _string_argument(call, "path") if "path" in call.arguments else "."
        glob_pattern = _string_argument(call, "glob") if "glob" in call.arguments else ""
        target = workspace._validate_search_request(query, path, glob_pattern)
        relative_scope = target.relative_to(workspace.root).as_posix() or "."
        approval = ActionRequest(
            operation="search",
            path=relative_scope,
            diff=(
                f"固定字面量（区分大小写，不是正则）：{query}\n"
                f"工作区相对范围：{relative_scope}\n"
                f"文件路径 glob：{glob_pattern or '无（搜索所有允许的 UTF-8 文本文件）'}\n"
                "会遵守搜索路径上的 .gitignore；敏感路径、内部状态、链接、硬链接、二进制和超大文件会跳过。\n"
                f"本次最多检查 {SEARCH_ENTRY_LIMIT} 个目录项、{SEARCH_FILE_COUNT_LIMIT} 个文件、"
                f"{SEARCH_DIRECTORY_COUNT_LIMIT} 个目录（递归深度最多 {SEARCH_DEPTH_LIMIT}）、"
                f"{SEARCH_TOTAL_BYTES_LIMIT // (1024 * 1024)} MiB，最多运行 {SEARCH_TIME_LIMIT_SECONDS:g} 秒；"
                f"最多返回 {SEARCH_MATCH_LIMIT} 条、每条最多 {SEARCH_MATCH_SNIPPET_CHARS} 字符、"
                f"总输出不超过 {SEARCH_OUTPUT_LIMIT_BYTES // 1024} KiB。\n"
                "匹配行片段及相对路径会脱敏后发送给模型；搜索结果是未可信文本，不产生后续写入或运行授权。"
            ),
            source="tool",
        )
        if not approve_action(approval):
            return "用户拒绝本次工作区文本搜索；没有读取文件内容。"
        _raise_if_cancelled(cancellation_event)
        if workspace._validate_search_request(query, path, glob_pattern) != target:
            raise WorkspaceError("审批后搜索范围发生变化；没有扫描文件。")
        return workspace.search_text(
            query,
            relative_directory=path,
            glob_pattern=glob_pattern,
            cancellation_event=cancellation_event,
        )

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
        approval_diff = preview.diff
        approval_display = (
            f"目标文件：{preview.canonical_path}\n"
            f"工作区原文件指纹（SHA-256 前 16 位）：{actual_sha256[:16]}…\n"
            f"变化（+ 新增，- 删除）：\n{approval_diff}"
        )
        if _safe_terminal_display_length(approval_display) > PATCH_APPROVAL_DISPLAY_LIMIT:
            raise WorkspaceError("补丁审批预览超过终端显示上限；请缩小本次修改范围。")
        request = ActionRequest(
            operation="patch",
            path=preview.canonical_path,
            diff=approval_diff,
            source="tool",
            file_existed=True,
            trusted_file_sha256=actual_sha256,
        )
        if not approve_action(request):
            return "用户拒绝本次补丁；文件未修改，也未创建撤销备份。"
        _raise_if_cancelled(cancellation_event)
        backup_id = workspace.commit_write(preview, cancellation_event=cancellation_event)
        if backup_id is None:
            return "补丁结果与当前文件相同，没有写入。"
        return f"已应用精确补丁并创建撤销备份 {backup_id}：{preview.canonical_path}"

    if call.name == "apply_change_set":
        _raise_if_cancelled(cancellation_event)
        _validate_arguments(call, {"changes"})
        if plan_only:
            return "计划模式：已在工具层拦截多文件变更集；没有审批或修改文件。"
        result = workspace.apply_change_set(
            call.arguments["changes"],
            approve_action=approve_action,
            cancellation_event=cancellation_event,
        )
        if emit_output is not None and "已应用全部文件变更" not in result:
            emit_output(f"变更集本地执行状态：{result}")
        return result

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
