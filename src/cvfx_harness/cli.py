from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Callable

from .agent import Agent
from .providers import ProviderError, provider_from_environment
from .tools import Workspace, WorkspaceError

_REFERENCE = re.compile(r"(?<![\w@])@(?:\{([^{}]+)\}|([^\s]+))", re.UNICODE)
_TRAILING_PUNCTUATION = ",，。;；!?！？)]}"


def configure_console_encoding() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def parse_references(text: str, workspace: Workspace) -> tuple[str, list[tuple[str, str]]]:
    references: list[tuple[str, str]] = []
    visible_text = text

    for match in reversed(list(_REFERENCE.finditer(text))):
        raw_path = match.group(1) or match.group(2) or ""
        path = raw_path.rstrip(_TRAILING_PUNCTUATION)
        if not path:
            continue
        try:
            contents = workspace.read_text(path)
        except WorkspaceError:
            # Leave unresolvable @mentions untouched so emails and prose remain usable.
            continue
        references.insert(0, (path, contents))
        visible_text = visible_text[: match.start()] + visible_text[match.end() :]

    return visible_text.strip(), references


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cvfx-harness",
        description="Windows 优先、中文交互的 AI 编程 Harness 最小原型。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="工作区目录；所有文件工具只接受该目录内的相对路径",
    )
    parser.add_argument(
        "--provider",
        choices=("mock", "openai"),
        default=os.environ.get("HARNESS_PROVIDER", "mock"),
        help="模型提供商；默认 mock，不会发送网络请求",
    )
    parser.add_argument("--model", default=None, help="覆盖 HARNESS_MODEL")
    return parser


def _help_text() -> str:
    return """命令：
  /帮助              显示帮助
  /状态              显示当前模型、目标和模式
  /模式 计划|执行    切换计划或执行模式（计划模式会拦截写入）
  /目标 <内容>       设置本会话目标；/目标 清空 可清除
  /退出              结束会话

文件引用：在提问中写 @相对路径，或用 @{包含空格的相对路径}。
写入文件前每次都会显示路径并等待你确认。当前版本不会运行 shell 命令。
"""


def _confirm_write(path: str, existed: bool, *, input_fn: Callable[[str], str]) -> bool:
    action = "覆盖" if existed else "创建"
    try:
        answer = input_fn(f"\n需要{action}工作区文件 {path!r}。确认？[y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().casefold() in {"y", "yes", "是"}


def run_console(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> int:
    try:
        workspace = Workspace(args.workspace)
        provider = provider_from_environment(args.provider, args.model)
    except (WorkspaceError, ProviderError) as exc:
        output(f"错误：{exc}")
        return 2

    mode = "执行"
    agent = Agent(
        provider,
        workspace,
        approve_write=lambda path, existed: _confirm_write(
            path, existed, input_fn=input_fn
        ),
    )
    output(f"chenxiVFX Harness v0.1.0 | 提供商：{provider.label}")
    output(f"工作区：{workspace.root}")
    output("输入 /帮助 查看命令。默认使用离线模拟模型。")

    while True:
        try:
            line = input_fn(f"你[{mode}]> ")
        except EOFError:
            output("\n会话结束。")
            return 0
        except KeyboardInterrupt:
            output("\n已取消当前交互。")
            return 130

        stripped = line.strip()
        if not stripped:
            continue
        command, _, argument = stripped.partition(" ")
        command = command.casefold()

        if command in {"/帮助", "/help"}:
            output(_help_text())
            continue
        if command in {"/退出", "/exit", "/quit"}:
            output("会话结束。")
            return 0
        if command in {"/状态", "/status"}:
            output(
                f"提供商：{provider.label}\n"
                f"模型：{getattr(provider, 'model', '未知')}\n"
                f"工作区：{workspace.root}\n"
                f"模式：{mode}\n"
                f"目标：{agent.goal or '（未设置）'}"
            )
            continue
        if command in {"/模式", "/mode"}:
            if argument.strip() in {"计划", "plan"}:
                mode = "计划"
            elif argument.strip() in {"执行", "execute"}:
                mode = "执行"
            else:
                output("用法：/模式 计划 或 /模式 执行")
                continue
            output(f"已切换到{mode}模式。")
            continue
        if command in {"/目标", "/goal"}:
            if argument.strip() in {"清空", "clear"}:
                agent.goal = ""
            else:
                agent.goal = argument.strip()
            output(f"会话目标：{agent.goal or '（已清空）'}")
            continue

        prompt, references = parse_references(line, workspace)
        try:
            answer = agent.ask(prompt, references=references, plan_only=mode == "计划")
        except ProviderError as exc:
            output(f"模型请求失败：{exc}")
            continue
        output(answer)


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    args = build_parser().parse_args(argv)
    return run_console(args)
