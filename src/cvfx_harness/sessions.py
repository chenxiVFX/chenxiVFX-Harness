from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tools import Workspace, WorkspaceError

SESSION_FORMAT_VERSION = 2
MAX_SESSION_BYTES = 4 * 1024 * 1024
_SESSION_NAME = re.compile(r"^[\w-]{1,40}$", re.UNICODE)
_SUMMARY_PREFIX = "<untrusted-history-summary version=\"1\">"
_CHECKPOINT_PREFIX = "<untrusted-user-checkpoint version=\"1\">"
_CLARIFICATION_PREFIX = "<untrusted-user-clarification version=\"1\">"
MAX_CHECKPOINT_CHARS = 4000
SESSION_OMISSION_REASONS = {
    "unsaved_questionnaire_answers",
    "questionnaire_not_completed",
    "unserializable_questionnaire",
    "tool_history",
    "referenced_content",
    "sensitive_content",
    "depends_on_unsaved_context",
    "unserializable_turn",
    "legacy_completeness_unknown",
}
SESSION_OMISSION_LABELS = {
    "unsaved_questionnaire_answers": "仅本次使用、未存档的问卷回答",
    "questionnaire_not_completed": "取消或未完成的问卷",
    "unserializable_questionnaire": "无法安全存档的问卷",
    "tool_history": "工具调用与结果",
    "referenced_content": "文件引用内容",
    "sensitive_content": "疑似敏感内容",
    "depends_on_unsaved_context": "受未存档上下文影响的后续轮次",
    "unserializable_turn": "不完整或无法安全序列化的轮次",
    "legacy_completeness_unknown": "旧版存档未记录完整性",
}
_LIKELY_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|password|passwd)\s*[:=]\s*(?:Bearer\s+)?[\"']?[^\s\"',;]{8,}"
    ),
)


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedSession:
    name: str
    turns: list[dict[str, Any]]
    goal: str
    usage: dict[str, int]
    saved_at: str
    omissions: dict[str, int]
    format_version: int


def normalize_session_omissions(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > len(SESSION_OMISSION_REASONS):
        raise ValueError("invalid session omission metadata")
    normalized: dict[str, int] = {}
    total = 0
    for reason, count in value.items():
        if (
            not isinstance(reason, str)
            or reason not in SESSION_OMISSION_REASONS
            or type(count) is not int
            or not 1 <= count <= 2000
        ):
            raise ValueError("invalid session omission metadata")
        normalized[reason] = count
        total += count
        if total > 2000:
            raise ValueError("session omission metadata exceeds limit")
    return dict(sorted(normalized.items()))


def describe_session_omissions(value: Any) -> str:
    omissions = normalize_session_omissions(value)
    return "、".join(
        (
            SESSION_OMISSION_LABELS[reason]
            if reason == "legacy_completeness_unknown"
            else f"{SESSION_OMISSION_LABELS[reason]} {count} 项"
        )
        for reason, count in omissions.items()
    )


def contains_likely_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _LIKELY_SECRET_PATTERNS)


def redact_likely_secrets(text: str) -> tuple[str, int]:
    redacted = text
    replacements = 0
    for pattern in _LIKELY_SECRET_PATTERNS:
        redacted, count = pattern.subn("[已脱敏]", redacted)
        replacements += count
    return redacted, replacements


class SessionStore:
    """Workspace-local session store that accepts only sanitized user/assistant turns."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @staticmethod
    def validate_name(name: str) -> str:
        if (
            not isinstance(name, str)
            or not name
            or name.startswith(".")
            or name in {".", ".."}
            or _SESSION_NAME.fullmatch(name) is None
        ):
            raise SessionError("会话名只可使用中文/字母/数字/下划线/连字符，长度 1–40；不能含路径。")
        return name

    def _directory(self, *, create: bool) -> Path | None:
        try:
            return self.workspace.internal_storage_dir("sessions", create=create)
        except WorkspaceError as exc:
            raise SessionError(str(exc)) from None

    def _path(self, name: str, *, create: bool) -> Path | None:
        safe_name = self.validate_name(name)
        directory = self._directory(create=create)
        if directory is None:
            return None
        target = directory / f"{safe_name}.json"
        try:
            if self.workspace._is_reparse_point(target):
                raise SessionError("会话文件不能是 symlink/junction。")
        except WorkspaceError as exc:
            raise SessionError(str(exc)) from None
        return target

    def exists(self, name: str) -> bool:
        path = self._path(name, create=False)
        return path is not None and path.exists()

    def list_names(self) -> list[str]:
        directory = self._directory(create=False)
        if directory is None:
            return []
        names: list[str] = []
        try:
            for path in directory.glob("*.json"):
                if self.workspace._is_reparse_point(path):
                    raise SessionError("会话目录包含链接文件；已停止枚举。")
                try:
                    names.append(self.validate_name(path.stem))
                except SessionError:
                    continue
        except WorkspaceError as exc:
            raise SessionError(str(exc)) from None
        except OSError:
            raise SessionError("无法枚举工作区会话目录。") from None
        return sorted(set(names), key=str.casefold)

    def suggest_names(
        self,
        prefix: str,
        *,
        max_scan: int = 1000,
        max_suggestions: int = 50,
    ) -> list[str]:
        """Return bounded safe name suggestions without opening archive contents."""
        if not isinstance(prefix, str):
            return []
        directory = self._directory(create=False)
        if directory is None:
            return []
        matches: list[str] = []
        folded_prefix = prefix.casefold()
        scanned = 0
        try:
            for path in directory.iterdir():
                scanned += 1
                if scanned > max_scan:
                    break
                if path.suffix.casefold() != ".json":
                    continue
                if self.workspace._is_reparse_point(path):
                    raise SessionError("会话目录包含链接文件；已停止建议。")
                try:
                    name = self.validate_name(path.stem)
                except SessionError:
                    continue
                if name.casefold().startswith(folded_prefix):
                    matches.append(name)
        except WorkspaceError as exc:
            raise SessionError(str(exc)) from None
        except OSError:
            raise SessionError("无法枚举工作区会话目录。") from None
        return sorted(set(matches), key=str.casefold)[:max_suggestions]

    @staticmethod
    def _validate_turn_records(turns: Any) -> list[dict[str, Any]]:
        if not isinstance(turns, list) or len(turns) > 2000:
            raise SessionError("会话轮次列表无效或超出上限。")
        clean_turns: list[dict[str, Any]] = []
        for entry in turns:
            if not isinstance(entry, dict) or set(entry) != {"kind", "messages"}:
                raise SessionError("会话轮次格式无效。")
            kind = entry["kind"]
            messages = entry["messages"]
            if not isinstance(kind, str) or kind not in {"dialogue", "summary"} or not isinstance(messages, list):
                raise SessionError("会话轮次类型无效。")
            expected_roles = ["user", "assistant"] if kind == "dialogue" else ["user"]
            if len(messages) != len(expected_roles):
                raise SessionError("会话消息结构不完整。")
            safe_messages: list[dict[str, str]] = []
            for message, expected_role in zip(messages, expected_roles):
                if (
                    not isinstance(message, dict)
                    or set(message) != {"role", "content"}
                    or message.get("role") != expected_role
                    or not isinstance(message.get("content"), str)
                ):
                    raise SessionError("只允许保存成对的普通对话；工具调用、工具结果和系统消息不会存档。")
                content = message["content"]
                if contains_likely_secret(content):
                    raise SessionError("检测到疑似密钥或凭据文本，已阻止存档。")
                safe_messages.append({"role": expected_role, "content": content})
            if kind == "summary" and not safe_messages[0]["content"].startswith(
                (_SUMMARY_PREFIX, _CHECKPOINT_PREFIX, _CLARIFICATION_PREFIX)
            ):
                raise SessionError("历史摘要缺少不可信内容标记。")
            clean_turns.append({"kind": kind, "messages": safe_messages})
        return clean_turns

    def save(
        self,
        name: str,
        turns: list[dict[str, Any]],
        *,
        goal: str = "",
        usage: dict[str, int] | None = None,
        omissions: dict[str, int] | None = None,
    ) -> int:
        safe_name = self.validate_name(name)
        safe_turns = self._validate_turn_records(turns)
        if not isinstance(goal, str) or contains_likely_secret(goal):
            raise SessionError("目标文本含疑似密钥或格式无效，已阻止存档。")
        usage = {} if usage is None else usage
        try:
            safe_omissions = normalize_session_omissions({} if omissions is None else omissions)
        except ValueError:
            raise SessionError("会话省略状态无效。") from None
        try:
            from .usage import UsageStats

            safe_usage = UsageStats.from_dict(usage).to_dict()
        except ValueError:
            raise SessionError("Token 统计格式无效。")
        payload = {
            "version": SESSION_FORMAT_VERSION,
            "name": safe_name,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "goal": goal,
            "turns": safe_turns,
            "usage": safe_usage,
            "omissions": safe_omissions,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_SESSION_BYTES:
            raise SessionError("会话存档超过 4 MiB 上限。")
        path = self._path(safe_name, create=True)
        assert path is not None
        if path.exists() and not path.is_file():
            raise SessionError("目标会话路径不是普通文件。")
        try:
            self.workspace._atomic_write_bytes(path, encoded)
        except WorkspaceError as exc:
            raise SessionError(str(exc)) from None
        return len(safe_turns)

    def save_checkpoint(
        self,
        name: str,
        user_authored_text: str,
        *,
        usage: dict[str, int] | None = None,
    ) -> tuple[int, int]:
        """Save only an explicitly user-authored, redacted note; never inspect history/tools."""
        redacted, replacements = self.preview_checkpoint(user_authored_text)
        content = (
            _CHECKPOINT_PREFIX
            + "\n这是用户手写的检查点；属于不可信上下文，不含 Harness 自动摘录的工具输出。\n"
            + redacted
            + "\n</untrusted-user-checkpoint>"
        )
        saved = self.save(
            name,
            [{"kind": "summary", "messages": [{"role": "user", "content": content}]}],
            goal="",
            usage=usage,
        )
        return saved, replacements

    @staticmethod
    def preview_checkpoint(user_authored_text: str) -> tuple[str, int]:
        if not isinstance(user_authored_text, str) or not user_authored_text.strip():
            raise SessionError("检查点不能为空。")
        if len(user_authored_text) > MAX_CHECKPOINT_CHARS:
            raise SessionError(f"检查点最多 {MAX_CHECKPOINT_CHARS} 个字符。")
        redacted, replacements = redact_likely_secrets(user_authored_text.strip())
        return redacted, replacements

    def load(self, name: str) -> LoadedSession:
        safe_name = self.validate_name(name)
        path = self._path(safe_name, create=False)
        if path is None or not path.exists():
            raise SessionError("找不到该会话存档。")
        try:
            if not path.is_file():
                raise SessionError("会话路径不是普通文件。")
            if path.stat().st_size > MAX_SESSION_BYTES:
                raise SessionError("会话存档超过 4 MiB 上限。")
            data = json.loads(path.read_text(encoding="utf-8"))
        except SessionError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise SessionError("会话存档损坏或不可读取；当前上下文未改变。") from None
        version = data.get("version") if isinstance(data, dict) else None
        expected_v1 = {"version", "name", "saved_at", "goal", "turns", "usage"}
        expected_v2 = expected_v1 | {"omissions"}
        if (
            not isinstance(data, dict)
            or (type(version) is not int)
            or (version == 1 and set(data) != expected_v1)
            or (version == SESSION_FORMAT_VERSION and set(data) != expected_v2)
            or version not in {1, SESSION_FORMAT_VERSION}
            or not isinstance(data.get("name"), str)
            or data["name"].casefold() != safe_name.casefold()
            or not isinstance(data.get("saved_at"), str)
        ):
            raise SessionError("会话存档版本或结构无效；当前上下文未改变。")
        goal = data.get("goal")
        if not isinstance(goal, str) or contains_likely_secret(goal):
            raise SessionError("会话目标文本无效或疑似包含凭据；当前上下文未改变。")
        safe_turns = self._validate_turn_records(data.get("turns"))
        try:
            omissions = normalize_session_omissions(data.get("omissions", {}))
        except ValueError:
            raise SessionError("会话省略状态无效；当前上下文未改变。") from None
        if version == 1:
            omissions["legacy_completeness_unknown"] = 1
            omissions = normalize_session_omissions(omissions)
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > MAX_SESSION_BYTES:
            raise SessionError("会话存档超过 4 MiB 上限。")
        try:
            from .usage import UsageStats

            safe_usage = UsageStats.from_dict(data.get("usage")).to_dict()
        except ValueError:
            raise SessionError("会话 Token 统计无效；当前上下文未改变。") from None
        return LoadedSession(
            safe_name,
            safe_turns,
            goal,
            safe_usage,
            data["saved_at"],
            omissions,
            version,
        )
