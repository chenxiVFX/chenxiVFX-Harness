from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from .providers import ToolCall

READ_LIMIT_BYTES = 64 * 1024

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the current workspace.",
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
            "description": "Write a UTF-8 text file inside the current workspace; the user must approve every write.",
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


class Workspace:
    """File access constrained to a canonical workspace root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise WorkspaceError("工作区必须是已存在的目录。")

    def resolve(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise WorkspaceError("文件路径不能为空。")
        if "\x00" in relative_path:
            raise WorkspaceError("文件路径包含无效字符。")
        if Path(relative_path).is_absolute() or PureWindowsPath(relative_path).is_absolute():
            raise WorkspaceError("只允许工作区内的相对路径。")

        candidate = (self.root / relative_path).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise WorkspaceError("拒绝访问工作区之外的路径。") from None
        if candidate == self.root:
            raise WorkspaceError("路径必须指向工作区中的文件。")
        return candidate

    def read_text(self, relative_path: str) -> str:
        target = self.resolve(relative_path)
        if not target.is_file():
            raise WorkspaceError(f"文件不存在或不是普通文件：{relative_path}")
        if target.stat().st_size > READ_LIMIT_BYTES:
            raise WorkspaceError("文件超过 64 KiB 单文件读取上限。")
        try:
            return target.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            raise WorkspaceError("当前仅支持 UTF-8 文本文件。") from None
        except OSError as exc:
            raise WorkspaceError(f"读取文件失败：{exc}") from None

    def write_text(self, relative_path: str, content: str) -> Path:
        target = self.resolve(relative_path)
        if not target.parent.is_dir():
            raise WorkspaceError("目标文件的父目录必须已存在。")
        if target.exists() and not target.is_file():
            raise WorkspaceError("目标路径不是普通文件。")
        try:
            with target.open("w", encoding="utf-8", newline="") as handle:
                handle.write(content)
        except OSError as exc:
            raise WorkspaceError(f"写入文件失败：{exc}") from None
        return target


def _string_argument(call: ToolCall, name: str) -> str:
    value = call.arguments.get(name)
    if not isinstance(value, str):
        raise WorkspaceError(f"工具参数 {name} 必须是字符串。")
    return value


def run_tool(
    workspace: Workspace,
    call: ToolCall,
    *,
    approve_write: Callable[[str, bool], bool],
    plan_only: bool = False,
) -> str:
    """Execute one known tool. Model-provided names never select arbitrary code."""

    if call.name == "read_file":
        path = _string_argument(call, "path")
        return workspace.read_text(path)

    if call.name == "write_file":
        path = _string_argument(call, "path")
        content = _string_argument(call, "content")
        target = workspace.resolve(path)
        if plan_only:
            return "计划模式：已拦截写入；文件未修改。"
        existed = target.exists()
        if not approve_write(path, existed):
            return "用户拒绝本次写入；文件未修改。"
        workspace.write_text(path, content)
        return f"已写入工作区文件：{path}"

    return f"未授权的工具名称：{call.name}；没有执行任何操作。"
