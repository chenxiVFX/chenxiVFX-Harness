from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .agent import Agent, DEFAULT_MAX_OUTPUT_TOKENS
from .config import (
    CONFIG_RELATIVE_PATH,
    ConfigError,
    WorkspaceConfig,
    WorkspaceConfigStore,
    resolve_config,
    validate_workspace_config,
)
from .providers import (
    DEEPSEEK_API_KEY_ENV,
    ProviderCancelled,
    ProviderError,
    provider_from_environment,
)
from .sessions import SessionError, SessionStore
from .subagents import DelegationPlanError, build_delegation_plan, run_bounded_subagents
from .tools import ActionRequest, Workspace, WorkspaceError
from .usage import DEFAULT_TOKEN_BUDGET

_REFERENCE = re.compile(r"(?<![\w@])@(?:\{([^{}]+)\}|([^\s]+))", re.UNICODE)
_TRAILING_PUNCTUATION = ",，。;；!?！？)]}"


def _safe_terminal_text(text: str) -> str:
    """Render model-controlled control/format characters visibly, not as terminal codes."""
    visible: list[str] = []
    for char in text:
        if char == "\n":
            visible.append(char)
            continue
        category = unicodedata.category(char)
        if category.startswith("C"):
            codepoint = ord(char)
            if codepoint <= 0xFF:
                visible.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                visible.append(f"\\u{codepoint:04x}")
            else:
                visible.append(f"\\U{codepoint:08x}")
        else:
            visible.append(char)
    return "".join(visible)


def configure_console_encoding() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _parse_integer_argument(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是整数；为避免误泄露，输入值不会回显。") from None


def parse_references(
    text: str,
    workspace: Workspace,
    *,
    approve_action: Callable[[ActionRequest], bool],
    on_blocked: Callable[[str, str], None] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    references: list[tuple[str, str]] = []
    visible_text = text

    for match in reversed(list(_REFERENCE.finditer(text))):
        raw_path = match.group(1) or match.group(2) or ""
        path = raw_path.rstrip(_TRAILING_PUNCTUATION)
        if not path:
            continue
        try:
            workspace.resolve(path)
            if not approve_action(
                ActionRequest(operation="read", path=path, source="reference")
            ):
                if on_blocked is not None:
                    on_blocked(path, "用户拒绝读取；文件内容未提供给模型")
                continue
            contents = workspace.read_text(path)
        except WorkspaceError as exc:
            # Leave unresolvable @mentions untouched so emails and prose remain usable.
            if on_blocked is not None:
                on_blocked(path, str(exc))
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
        default=argparse.SUPPRESS,
        help="模型提供商：mock 或 openai（仅 DeepSeek 兼容模式；默认 mock）",
    )
    parser.add_argument("--model", default=argparse.SUPPRESS, help="覆盖工作区配置和 HARNESS_MODEL")
    parser.add_argument(
        "--base-url",
        default=argparse.SUPPRESS,
        help="DeepSeek HTTPS Base URL；密钥只允许发往 api.deepseek.com:443",
    )
    parser.add_argument(
        "--token-budget",
        type=_parse_integer_argument,
        default=argparse.SUPPRESS,
        help="本地上下文预算提示（默认 16384）；只是估算，不会限制提供商侧输出上限",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=_parse_integer_argument,
        default=argparse.SUPPRESS,
        help="单次提问输出上限（默认 2048，范围 1–65536）；兼容提供商收到对应 max_tokens",
    )
    parser.add_argument(
        "--ignore-config",
        action="store_true",
        help="忽略工作区配置文件；用于文件损坏时安全恢复，环境变量和命令行仍生效",
    )
    return parser


def _help_text() -> str:
    return """命令：
  /帮助              显示帮助
  /状态              显示当前模型、目标和模式
  /模式 计划|执行    切换计划或执行模式（计划模式会拦截写入与撤销）
  /目标 <内容>       设置本会话目标；/目标 清空 可清除
  /保存 [名称]       将可安全保存的普通对话保存到工作区 .harness/sessions/
  /检查点 <名称>    输入一段手写脱敏摘要；预览并确认后仅保存该摘要
  /恢复 <名称>       校验并载入会话；替换当前上下文前会询问
  /会话              列出本工作区的本地会话名称
  /压缩 [保留轮数]   预览本地上下文压缩；默认保留最近 4 轮，需确认后应用
  /统计              显示当前估算上下文、预算、累计用量和提供商报告
  /预算 [tokens]     查看或临时设置本轮上下文预算（1024–1000000）
  /配置              显示本轮和下次启动的非敏感设置；密钥值不会显示或保存
  /配置 提供商 mock|openai
  /配置 模型 <名称>|默认
  /配置 端点 <DeepSeek HTTPS URL>|默认
  /配置 预算 <1024–1000000>
  /配置 输出上限 <1–65536>
  /子代理 任务 | 任务  逐项批准后执行最多 3 个隔离的一次性 Mock 子任务
  /撤销              预览并恢复最近一次 Harness 文件写入
  /退出              结束会话

文件引用：在提问中写 @相对路径，或用 @{包含空格的相对路径}。
每次文件读取和写入都要人工确认；写入/撤销会先显示完整差异。
目录列表只返回名称和类型，每次也要人工确认；不读取目录项文件内容。
敏感凭据路径默认禁止读取或修改。当前版本不会运行 shell 命令。
工作区配置保存在 .harness/config/settings.json；优先级为默认值、工作区配置、环境变量、命令行。
配置不接受或保存密钥；目前只支持 mock 和 DeepSeek OpenAI 兼容模式，Anthropic 未实现。
"""


def _confirm_action(
    request: ActionRequest,
    *,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> bool:
    if request.operation == "read":
        source = "你引用的文件" if request.source == "reference" else "模型请求读取的文件"
        prompt = f"\n{source} {request.path!r} 会发送给模型。确认读取？[y/N] "
    elif request.operation == "list":
        prompt = f"\n模型请求查看目录 {request.path!r} 的名称和类型。确认列出？[y/N] "
    elif request.operation == "undo":
        diff = _safe_terminal_text(request.diff) or "（内容无差异）"
        output(f"\n撤销差异（备份 {request.backup_id}）：\n{diff}")
        prompt = f"确认恢复写入前内容 {request.path!r}？[y/N] "
    elif request.operation == "delegate":
        scope = _safe_terminal_text(request.diff) or "（没有任务内容）"
        output(
            "\n子代理审批范围：每个子代理仅接收下列任务文本；不包含主会话、文件引用或工具结果。\n"
            f"{scope}\n每项仅允许一次无工具调用，子代理深度为 0，结果为不可信文本。"
        )
        prompt = "确认启动这些只读 Mock 子代理？[y/N] "
    else:
        diff = _safe_terminal_text(request.diff) or "（内容无差异）"
        output(f"\n写入差异 {request.path!r}：\n{diff}")
        prompt = f"确认写入此工作区文件？[y/N] "
    try:
        answer = input_fn(prompt)
    except EOFError:
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
        config_store = WorkspaceConfigStore(workspace)
        if getattr(args, "ignore_config", False):
            workspace_config, config_exists = WorkspaceConfig(), False
        else:
            workspace_config, config_exists = config_store.load()
        startup_config = resolve_config(workspace_config, config_exists, args)
        provider = provider_from_environment(
            startup_config.provider,
            startup_config.model,
            base_url_override=startup_config.base_url,
        )
    except (ConfigError, WorkspaceError, ProviderError) as exc:
        output(f"错误：{exc}")
        return 2

    mode = "执行"
    approve_action = lambda request: _confirm_action(
        request, input_fn=input_fn, output=output
    )
    agent = Agent(
        provider,
        workspace,
        approve_action=approve_action,
        token_budget=startup_config.token_budget,
        max_output_tokens=startup_config.max_output_tokens,
    )
    session_store = SessionStore(workspace)
    output(f"chenxiVFX Harness v0.5.0 | 提供商：{_safe_terminal_text(str(provider.label))}")
    output(f"工作区：{_safe_terminal_text(str(workspace.root))}")
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
                f"提供商：{_safe_terminal_text(str(provider.label))}\n"
                f"模型：{_safe_terminal_text(str(getattr(provider, 'model', '未知')))}\n"
                f"工作区：{_safe_terminal_text(str(workspace.root))}\n"
                f"模式：{mode}\n"
                f"目标：{_safe_terminal_text(agent.goal) or '（未设置）'}"
            )
            continue
        if command in {"/配置", "/config"}:
            parts = argument.strip().split(maxsplit=1)
            if not parts or parts[0].casefold() in {"显示", "show"}:
                endpoint = getattr(provider, "endpoint", None)
                source = startup_config.sources
                output(
                    f"本轮运行提供商：{_safe_terminal_text(str(provider.label))}\n"
                    f"本轮模型：{_safe_terminal_text(str(getattr(provider, 'model', '未知')))}\n"
                    f"本轮 API 端点：{_safe_terminal_text(str(endpoint or '无（离线模拟）'))}\n"
                    f"工作区：{_safe_terminal_text(str(workspace.root))}\n"
                    f"工作区配置文件：{_safe_terminal_text(str(workspace.root / CONFIG_RELATIVE_PATH))}\n"
                    "下次启动设置（优先级：默认 < 工作区配置 < 环境变量 < 命令行）：\n"
                    f"提供商：{startup_config.provider}（{source['provider']}）\n"
                    f"模型：{_safe_terminal_text(startup_config.model)}（{source['model']}）\n"
                    f"Base URL：{_safe_terminal_text(startup_config.base_url or '无（mock）')}（{source['base_url']}）\n"
                    f"上下文预算：{startup_config.token_budget}（{source['token_budget']}）\n"
                    f"输出上限：{startup_config.max_output_tokens}（{source['max_output_tokens']}）\n"
                    f"本轮预算：{agent.token_budget}；本轮输出上限：{agent.max_output_tokens} tokens\n"
                    f"OpenAI 密钥变量：{DEEPSEEK_API_KEY_ENV}；仅 OpenAI 模式从环境读取，不展示或写入配置\n"
                    "当前只支持 mock 和 DeepSeek OpenAI 兼容模式；Anthropic 未实现。Shell：禁用"
                )
                continue

            field_name = parts[0].casefold()
            raw_value = parts[1] if len(parts) > 1 else ""
            aliases = {
                "提供商": "provider", "provider": "provider",
                "模型": "model", "model": "model",
                "端点": "base_url", "base_url": "base_url", "base-url": "base_url",
                "预算": "token_budget", "token_budget": "token_budget", "token-budget": "token_budget",
                "输出上限": "max_output_tokens", "max_output_tokens": "max_output_tokens",
                "max-output-tokens": "max_output_tokens",
            }
            if field_name in {"重置", "reset"} and not raw_value:
                new_config = WorkspaceConfig()
                changed_field = "all"
            else:
                changed_field = aliases.get(field_name)
                if changed_field is None or not raw_value:
                    output(
                        "用法：/配置 [提供商 mock|openai | 模型 <名称>|默认 | "
                        "端点 <DeepSeek HTTPS URL>|默认 | 预算 <1024–1000000> | "
                        "输出上限 <1–65536> | 重置]"
                    )
                    continue
                value = raw_value.strip()
                try:
                    if changed_field == "provider":
                        new_config = replace(workspace_config, provider=value)
                    elif changed_field == "model":
                        new_config = replace(workspace_config, model=None if value.casefold() in {"默认", "default"} else value)
                    elif changed_field == "base_url":
                        new_config = replace(workspace_config, base_url=None if value.casefold() in {"默认", "default"} else value)
                    elif changed_field == "token_budget":
                        parsed_value = DEFAULT_TOKEN_BUDGET if value.casefold() in {"默认", "default"} else int(value)
                        new_config = replace(workspace_config, token_budget=parsed_value)
                    else:
                        parsed_value = DEFAULT_MAX_OUTPUT_TOKENS if value.casefold() in {"默认", "default"} else int(value)
                        new_config = replace(workspace_config, max_output_tokens=parsed_value)
                    new_config = validate_workspace_config(new_config)
                except ConfigError as exc:
                    output(f"配置未保存：{exc}")
                    continue
                except ValueError:
                    output("配置未保存：字段值无效或不受支持。请用 /配置 查看允许的范围；输入内容未记录。")
                    continue

            try:
                config_store.save(new_config)
                workspace_config = new_config
                config_exists = True
                startup_config = resolve_config(workspace_config, config_exists, args)
            except ConfigError as exc:
                output(f"配置未保存：{exc}")
                continue

            if changed_field in {"token_budget", "all"} and startup_config.sources["token_budget"] == "工作区配置":
                agent.token_budget = startup_config.token_budget
            if changed_field in {"max_output_tokens", "all"} and startup_config.sources["max_output_tokens"] == "工作区配置":
                agent.max_output_tokens = startup_config.max_output_tokens

            output(f"已将非敏感设置保存到 {CONFIG_RELATIVE_PATH}。")
            if changed_field in {"provider", "model", "base_url", "all"}:
                output("提供商、模型或端点在下次启动时生效；本轮仍使用启动时的 provider。")
            for key in ({"token_budget", "max_output_tokens"} if changed_field == "all" else {changed_field}):
                if key in startup_config.sources and startup_config.sources[key] not in {"工作区配置", "默认"}:
                    output(f"该配置值已保存，但本次启动由更高优先级的 {startup_config.sources[key]} 覆盖。")
            if startup_config.provider == "openai":
                output(f"运行 OpenAI 兼容模式仍需从环境变量提供 {DEEPSEEK_API_KEY_ENV}；密钥不会写入配置文件。")
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
            output(f"会话目标：{_safe_terminal_text(agent.goal) or '（已清空）'}")
            continue
        if command in {"/会话", "/sessions"}:
            try:
                names = session_store.list_names()
                output("本地会话：\n" + ("\n".join(f"  {name}" for name in names) if names else "  （暂无）"))
            except SessionError as exc:
                output(f"会话读取失败：{exc}")
            continue
        if command in {"/保存", "/save"}:
            name = argument.strip() or "default"
            try:
                if session_store.exists(name):
                    try:
                        answer = input_fn(f"会话 {name!r} 已存在。确认覆盖？[y/N] ")
                    except (EOFError, KeyboardInterrupt):
                        output("已取消保存；现有存档未改变。")
                        continue
                    if answer.strip().casefold() not in {"y", "yes", "是"}:
                        output("已取消保存；现有存档未改变。")
                        continue
                safe_turns = agent.safe_session_turns()
                saved = session_store.save(
                    name,
                    safe_turns,
                    goal=agent.goal,
                    usage=agent.usage.to_dict(),
                )
                omitted = len(agent.turns) - saved
                output(f"已保存 {saved} 个安全对话轮次到会话 {name!r}；省略 {omitted} 个不完整、工具或敏感轮次。")
            except SessionError as exc:
                output(f"会话未保存：{exc}")
            continue
        if command in {"/检查点", "/checkpoint"}:
            name = argument.strip()
            if not name:
                output("用法：/检查点 <名称>")
                continue
            try:
                if session_store.exists(name):
                    try:
                        answer = input_fn(f"会话 {name!r} 已存在。确认覆盖？[y/N] ")
                    except (EOFError, KeyboardInterrupt):
                        output("已取消检查点；现有存档未改变。")
                        continue
                    if answer.strip().casefold() not in {"y", "yes", "是"}:
                        output("已取消检查点；现有存档未改变。")
                        continue
                note = input_fn(
                    "请手写愿意保存的进度摘要；不会自动读取会话、文件或工具结果：\n检查点> "
                )
                redacted, replacements = session_store.preview_checkpoint(note)
            except (EOFError, KeyboardInterrupt):
                output("已取消检查点；没有写入存档。")
                continue
            except SessionError as exc:
                output(f"检查点未保存：{exc}")
                continue
            output(
                f"检查点预览（疑似凭据替换 {replacements} 处；只保存这段用户手写摘要）：\n"
                f"{_safe_terminal_text(redacted)}"
            )
            try:
                answer = input_fn("确认保存此脱敏检查点？[y/N] ")
            except (EOFError, KeyboardInterrupt):
                output("已取消检查点；没有写入存档。")
                continue
            if answer.strip().casefold() not in {"y", "yes", "是"}:
                output("已取消检查点；没有写入存档。")
                continue
            try:
                _, saved_replacements = session_store.save_checkpoint(
                    name,
                    note,
                    usage=agent.usage.to_dict(),
                )
            except SessionError as exc:
                output(f"检查点未保存：{exc}")
                continue
            output(f"已保存检查点 {name!r}；凭据脱敏 {saved_replacements} 处。")
            continue
        if command in {"/恢复", "/load"}:
            if not argument.strip():
                output("用法：/恢复 <名称>")
                continue
            try:
                loaded = session_store.load(argument.strip())
            except SessionError as exc:
                output(f"会话未恢复：{exc}")
                continue
            if agent.turns or agent.goal:
                try:
                    answer = input_fn("恢复会话将替换当前上下文。确认？[y/N] ")
                except (EOFError, KeyboardInterrupt):
                    output("已取消恢复；当前上下文未改变。")
                    continue
                if answer.strip().casefold() not in {"y", "yes", "是"}:
                    output("已取消恢复；当前上下文未改变。")
                    continue
            agent.restore(turns=loaded.turns, goal=loaded.goal, usage=loaded.usage)
            output(f"已恢复会话 {loaded.name!r}，包含 {len(loaded.turns)} 个安全轮次。")
            continue
        if command in {"/统计", "/stats"}:
            context_tokens = agent.estimated_context_tokens()
            percent = context_tokens * 100 / agent.token_budget
            stats = agent.usage
            provider_usage = (
                f"{stats.provider_usage_reports} 次报告，输入 {stats.provider_prompt_tokens}、输出 {stats.provider_completion_tokens} tokens"
                if stats.provider_usage_reports
                else "暂无提供商 usage 报告"
            )
            output(
                f"当前上下文估算：{context_tokens} tokens（预算 {agent.token_budget}，约 {percent:.1f}%）\n"
                f"累计请求：{stats.requests}\n"
                f"累计估算输入/输出：{stats.estimated_input_tokens} / {stats.estimated_output_tokens} tokens\n"
                f"提供商报告：{provider_usage}\n"
                "估算使用本地字符启发式，不是 tokenizer 精确值；预算仅提示。"
            )
            continue
        if command in {"/预算", "/budget"}:
            if not argument.strip():
                output(f"当前 Token 预算提示：{agent.token_budget}")
                continue
            try:
                new_budget = int(argument.strip())
                if not 1024 <= new_budget <= 1_000_000:
                    raise ValueError
            except ValueError:
                output("用法：/预算 <1024–1000000>")
                continue
            agent.token_budget = new_budget
            output(f"已将本地预算提示设为 {new_budget} tokens。")
            continue
        if command in {"/压缩", "/compact"}:
            try:
                keep = int(argument.strip()) if argument.strip() else 4
                preview = agent.preview_compression(keep_recent_turns=keep)
            except ValueError as exc:
                output(f"无法预览压缩：{exc}")
                continue
            if preview is None:
                output("没有可安全压缩且能减少估算上下文的旧轮次。")
                continue
            summary = next(
                turn.messages[0]["content"]
                for turn in preview.replacement
                if turn.kind == "summary"
            )
            output(
                f"压缩预览：估算 {preview.before_tokens} → {preview.after_tokens} tokens；"
                f"概括 {preview.summarized_turns} 轮、丢弃旧轮次 {preview.dropped_turns} 个"
                f"（其中工具轮次 {preview.dropped_tool_turns} 个）。\n"
                f"将加入以下不可信用户上下文：\n{_safe_terminal_text(summary)}"
            )
            try:
                answer = input_fn("确认应用压缩？[y/N] ")
            except (EOFError, KeyboardInterrupt):
                output("已取消压缩；当前上下文未改变。")
                continue
            if answer.strip().casefold() not in {"y", "yes", "是"}:
                output("已取消压缩；当前上下文未改变。")
                continue
            try:
                agent.apply_compression(preview)
            except ValueError as exc:
                output(f"压缩未应用：{exc}")
                continue
            output("压缩已应用；历史摘要保留为不可信用户消息，系统提示未更改。")
            continue
        if command in {"/子代理", "/delegate"}:
            try:
                plan = build_delegation_plan(argument)
            except DelegationPlanError as exc:
                output(f"无法生成委派计划：{exc}")
                continue
            try:
                outcome = run_bounded_subagents(
                    plan,
                    provider,
                    approve_action=approve_action,
                )
            except DelegationPlanError as exc:
                output(f"无法执行受限子代理计划：{exc}")
                continue
            except KeyboardInterrupt:
                output("已取消子代理审批；没有启动未确认的任务。")
                continue
            output(_safe_terminal_text(outcome.summary))
            continue
        if command in {"/撤销", "/undo"}:
            try:
                output(
                    workspace.undo_last(
                        approve_action=approve_action,
                        plan_only=mode == "计划",
                    )
                )
            except WorkspaceError as exc:
                output(f"撤销失败：{exc}")
            except KeyboardInterrupt:
                output("已取消撤销审批；文件未修改。")
            continue

        try:
            prompt, references = parse_references(
                line,
                workspace,
                approve_action=approve_action,
                on_blocked=lambda path, reason: output(f"已跳过文件引用 {path!r}：{reason}"),
            )
        except KeyboardInterrupt:
            output("已取消文件引用审批；没有读取或发送文件内容。")
            continue
        try:
            answer = agent.ask(prompt, references=references, plan_only=mode == "计划")
        except KeyboardInterrupt:
            output("\n已取消当前模型请求；不完整轮次不会写入会话存档。前序已批准并完成的文件操作不会自动回滚，可用 /撤销检查。")
            continue
        except ProviderError as exc:
            output(f"模型请求失败：{exc}。前序已批准并完成的文件操作不会自动回滚，可用 /撤销检查。")
            continue
        except ProviderCancelled:
            output("已取消当前模型请求；不完整轮次不会写入会话存档。前序已批准并完成的文件操作不会自动回滚，可用 /撤销检查。")
            continue
        output(_safe_terminal_text(answer))


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    args = build_parser().parse_args(argv)
    return run_console(args)
