from __future__ import annotations

import asyncio
import os
import queue
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from prompt_toolkit import Application
from prompt_toolkit.clipboard import DummyClipboard
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    Dimension,
    HSplit,
    Layout,
    VSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, TextArea
from wcwidth import wcswidth

from .profiles import TestProfileError, TestProfileStore
from .config import ConfigError, WorkspaceConfigStore, validate_tui_shortcuts
from .questions import (
    MAX_TOTAL_ANSWER_BYTES,
    QuestionnaireRequest,
    QuestionnaireResult,
    QuestionRequestError,
    validate_question_answers,
)
from .sessions import (
    SessionError,
    SessionStore,
    contains_likely_secret,
    redact_likely_secrets,
)
from .tools import Workspace, WorkspaceError

MAX_TRANSCRIPT_CHARS = 200_000
POLL_INTERVAL_SECONDS = 0.025
COMMANDS = (
    ("/帮助", "显示完整说明"), ("/命令", "列出中文命令"),
    ("/状态", "查看模型、工作区和审批边界"), ("/项目说明", "预览并确认根目录项目说明"),
    ("/模式", "切换计划或执行"),
    ("/目标", "设置会话目标"), ("/计划", "生成、审阅、修订或执行计划"),
    ("/保存", "保存安全会话"), ("/检查点", "保存手写摘要"),
    ("/恢复", "恢复安全会话"), ("/会话", "列出本地会话"),
    ("/多行", "撰写多行问题"), ("/压缩", "预览、压缩或撤销"),
    ("/统计", "查看本地估算和 provider usage"), ("/预算", "设置本地估算阈值"),
    ("/配置", "查看或交互审阅非敏感配置"), ("/测试", "管理固定 Python 测试配置"),
    ("/子代理", "管理隔离子任务"),
    ("/变更集", "查看批次证据或安全恢复"),
    ("/撤销", "预览并撤销写入"), ("/快捷键", "查看当前 TUI 按键映射"),
    ("/按键", "查看快捷键（/快捷键 的中文别名）"),
    ("/退出", "结束会话"),
)
ENUM_COMPLETIONS = {
    "/模式": ("计划", "执行"),
    "/项目说明": ("清除",),
    "/配置": ("显示", "编辑", "重置", "提供商", "模型", "端点", "预算", "输出上限"),
    "/配置 提供商": ("mock", "openai", "deepseek-anthropic"),
    "/测试": ("列表", "添加", "运行", "移除"),
    "/计划": ("生成", "显示", "修订", "接受", "拒绝", "添加", "执行", "完成", "阻塞", "取消", "重试", "清空"),
    "/压缩": ("撤销",),
    "/变更集": ("列表", "证据", "恢复"),
}
_APPROVAL_PROMPT = re.compile(r"\[y/N\]\s*$", re.IGNORECASE)


def _terminal_background_is_light(environment: Mapping[str, str]) -> bool:
    """Recognize the common COLORFGBG light-background palette indices."""
    colorfgbg = environment.get("COLORFGBG", "")
    if not colorfgbg:
        return False
    background = colorfgbg.rsplit(";", 1)[-1].strip().casefold()
    if background in {"white", "light", "lightgray", "lightgrey"}:
        return True
    try:
        return int(background, 10) in {7, 15}
    except ValueError:
        return False


def _style_rules_for_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ if environment is None else environment
    dark = {
        "": "fg:#d8dee6 bg:#0b0d10",
        "header": "bg:#0b0d10",
        "header.brand": "fg:#6bbdb4 bold",
        "header.muted": "fg:#84909b",
        "header.workspace": "fg:#c1c9d2",
        "status": "fg:#aab4bf bg:#11151a",
        "prompt": "fg:#6bbdb4 bold",
        "input": "fg:#e1e6ec",
        "input.separator": "fg:#303942",
        "transcript": "fg:#d1d7df",
        "transcript.diff.add": "fg:#8bd49c",
        "transcript.diff.remove": "fg:#f09ca5",
        "transcript.diff.meta": "fg:#89939f",
        "empty.background": "bg:#0b0d10",
        "empty.title": "fg:#6bbdb4 bg:#0b0d10 bold",
        "empty.hint": "fg:#8b96a1 bg:#0b0d10",
        "approval": "fg:#d8dee6",
        "approval.heading": "fg:#6bbdb4 bold",
        "button": "fg:#adb7c1 bg:#0b0d10",
        "button.focused": "fg:#0b0d10 bg:#6bbdb4 bold underline",
        "completion-menu": "bg:#0b0d10",
        "completion-menu.completion": "fg:#d4dbe3 bg:#0b0d10",
        "completion-menu.completion.current": "fg:#0b0d10 bg:#6bbdb4 bold underline",
        "completion-menu.meta.completion": "fg:#8c98a4 bg:#0b0d10",
        "completion-menu.meta.completion.current": "fg:#0b0d10 bg:#6bbdb4 bold underline",
    }
    if env.get("NO_COLOR"):
        return {name: "" for name in dark}
    if not _terminal_background_is_light(env):
        return dark

    return {
        "": "fg:#20252b bg:#ffffff",
        "header": "bg:#ffffff",
        "header.brand": "fg:#075e59 bold",
        "header.muted": "fg:#4c5965",
        "header.workspace": "fg:#303943",
        "status": "fg:#374151 bg:#f1f3f5",
        "prompt": "fg:#075e59 bold",
        "input": "fg:#20252b",
        "input.separator": "fg:#c4cbd3",
        "transcript": "fg:#20252b",
        "transcript.diff.add": "fg:#145a32",
        "transcript.diff.remove": "fg:#9e2434",
        "transcript.diff.meta": "fg:#59636e",
        "empty.background": "bg:#ffffff",
        "empty.title": "fg:#075e59 bg:#ffffff bold",
        "empty.hint": "fg:#59636e bg:#ffffff",
        "approval": "fg:#20252b",
        "approval.heading": "fg:#075e59 bold",
        "button": "fg:#424b55 bg:#ffffff",
        "button.focused": "fg:#ffffff bg:#075e59 bold underline",
        "completion-menu": "bg:#ffffff",
        "completion-menu.completion": "fg:#20252b bg:#ffffff",
        "completion-menu.completion.current": "fg:#ffffff bg:#075e59 bold underline",
        "completion-menu.meta.completion": "fg:#59636e bg:#ffffff",
        "completion-menu.meta.completion.current": "fg:#ffffff bg:#075e59 bold underline",
    }


class _TranscriptDiffLexer(Lexer):
    """Style unified diff additions and deletions only inside patch previews."""

    def lex_document(self, document):
        styles: list[str] = [""] * document.line_count
        in_diff = False
        for index, line in enumerate(document.lines):
            if in_diff and not line:
                in_diff = False
            if line == "补丁差异":
                in_diff = False
                continue
            if line.startswith("变化（+ 新增，- 删除）："):
                in_diff = True
                continue
            if line.startswith("审批选择："):
                in_diff = False
            if not in_diff:
                continue
            if line.startswith(("--- ", "+++ ", "@@")):
                styles[index] = "class:transcript.diff.meta"
            elif line.startswith("+"):
                styles[index] = "class:transcript.diff.add"
            elif line.startswith("-"):
                styles[index] = "class:transcript.diff.remove"

        lines = document.lines

        def get_line(index: int):
            if index < 0 or index >= len(lines):
                return []
            line = lines[index]
            return [(styles[index], line)] if styles[index] else [("", line)]

        return get_line


def _truncate_cells(text: str, width: int, *, from_start: bool = False) -> str:
    if width <= 0:
        return ""
    if wcswidth(text) <= width:
        return text
    if width == 1:
        return "…"
    shown: list[str] = []
    used = 0
    characters = reversed(text) if from_start else iter(text)
    for character in characters:
        cell_width = max(0, wcswidth(character))
        if used + cell_width > width - 1:
            break
        shown.append(character)
        used += cell_width
    body = "".join(reversed(shown)) if from_start else "".join(shown)
    return f"…{body}" if from_start else f"{body}…"


def _short_workspace_label(path: str) -> str:
    parts = [part for part in re.split(r"[\\/]+", path.rstrip("\\/")) if part]
    if len(parts) > 1:
        return f"…/{parts[-2]}/{parts[-1]}"
    return parts[0] if parts else path


def _safe_text(value: str) -> str:
    """Keep model/user-controlled terminal control characters visible as text."""
    from .cli import _safe_terminal_text

    redacted, _count = redact_likely_secrets(value)
    return _safe_terminal_text(redacted)


class CommandCompleter(Completer):
    """Offer fixed commands plus bounded, safe workspace file-reference names."""

    def __init__(self, workspace: Workspace | None = None) -> None:
        self._workspace = workspace
        self._test_profiles = TestProfileStore(workspace) if workspace is not None else None

    @staticmethod
    def _path_context(document) -> tuple[str, str, bool, int] | None:
        before = document.text_before_cursor
        line_start = before.rfind("\n") + 1
        line_before = before[line_start:]
        if document.text_after_cursor.split("\n", 1)[0]:
            return None
        marker = line_before.rfind("@")
        if marker < 0:
            return None
        absolute_marker = line_start + marker
        if absolute_marker and re.match(r"\w|@", before[absolute_marker - 1]):
            return None

        reference = line_before[marker:]
        braced = reference.startswith("@{")
        if braced:
            if "}" in reference or "{" in reference[2:]:
                return None
            path_prefix = reference[2:]
        else:
            path_prefix = reference[1:]
            if any(char.isspace() for char in path_prefix) or "{" in path_prefix or "}" in path_prefix:
                return None
            if not path_prefix:
                return None
        if "\\" in path_prefix or len(path_prefix) > 4096:
            return None
        directory_prefix, separator, name_prefix = path_prefix.rpartition("/")
        directory = directory_prefix if separator else "."
        return directory, name_prefix, braced, len(name_prefix)

    def get_completions(self, document, complete_event):
        del complete_event
        before = document.text_before_cursor
        if "\n" not in before and self._workspace is not None:
            for command in ("/恢复 ", "/load "):
                if before.startswith(command):
                    prefix = before[len(command) :]
                    if " " in prefix:
                        return
                    try:
                        names = SessionStore(self._workspace).suggest_names(prefix)
                    except SessionError:
                        names = []
                    for name in names:
                        yield Completion(
                            name,
                            start_position=-len(prefix),
                            display_meta="历史会话",
                        )
                    return
            if self._test_profiles is not None:
                for action in ("运行", "移除"):
                    marker = f"/测试 {action} "
                    if not before.startswith(marker):
                        continue
                    prefix = before[len(marker) :]
                    if " " in prefix:
                        return
                    try:
                        profiles = (
                            self._test_profiles.runnable()
                            if action == "运行"
                            else self._test_profiles.load()
                        )
                    except TestProfileError:
                        profiles = []
                    for profile in profiles:
                        if profile.profile_id.casefold().startswith(prefix.casefold()):
                            meta = (
                                f"{profile.framework}，可运行"
                                if profile.python is not None
                                else "旧配置，需重新添加解释器"
                            )
                            yield Completion(
                                profile.profile_id,
                                start_position=-len(prefix),
                                display_meta=meta,
                            )
                    return
        path_context = self._path_context(document)
        if path_context is not None and self._workspace is not None:
            directory, prefix, braced, replace_chars = path_context
            try:
                entries = self._workspace.suggest_path_entries(directory, prefix)
            except (WorkspaceError, OSError):
                entries = []
            for name, is_directory in entries:
                if not braced and any(char.isspace() for char in name):
                    continue
                completion_text = name + "/" if is_directory else name + ("}" if braced else " ")
                display = name + "/" if is_directory else name
                yield Completion(
                    completion_text,
                    start_position=-replace_chars,
                    display=display,
                    display_meta="目录" if is_directory else "文件",
                )
            return
        if "\n" in before:
            return
        for command, values in sorted(
            ENUM_COMPLETIONS.items(), key=lambda item: len(item[0]), reverse=True
        ):
            marker = command + " "
            if before.startswith(marker):
                prefix = before[len(marker) :]
                if " " in prefix:
                    return
                for value in values:
                    if value.startswith(prefix):
                        yield Completion(value, start_position=-len(prefix))
                return
        if before.startswith("/") and " " not in before:
            for command, description in COMMANDS:
                if command.startswith(before):
                    yield Completion(
                        command,
                        start_position=-len(before),
                        display_meta=description,
                    )


@dataclass
class _InputRequest:
    prompt: str
    approval: bool
    completed: threading.Event
    value: str | None = None
    error: BaseException | None = None


@dataclass
class _QuestionRequest:
    request: QuestionnaireRequest
    completed: threading.Event
    result: QuestionnaireResult | None = None
    error: BaseException | None = None


class TerminalUI:
    """Full-screen prompt_toolkit shell around the existing synchronous CLI loop."""

    def __init__(
        self,
        worker: Callable[[TerminalUI], int],
        *,
        input: Any = None,
        output: Any = None,
        reference_workspace: Workspace | None = None,
        shortcuts: Mapping[str, str] | None = None,
    ) -> None:
        self._worker_callable = worker
        self._input = input
        self._output = output
        self.tui_shortcuts = validate_tui_shortcuts(shortcuts or {})
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._pending: _InputRequest | None = None
        self._queued_question: _QuestionRequest | None = None
        self._question_pending: _QuestionRequest | None = None
        self._question_index = 0
        self._question_answers: list[str] = []
        self._question_reviewing = False
        self._question_save_with_session = False
        self._worker_thread: threading.Thread | None = None
        self._worker_started = False
        self._worker_done = False
        self._worker_result = 2
        self._cancel_event = threading.Event()
        self._cancel_requested = False
        self._request_ready = threading.Event()
        self._question_ready = threading.Event()
        self._status = "启动中"
        self._notice = ""
        self._resetting_input = False
        self._prompt = ""
        self._approval_text = ""
        self._approval_choice_yes = False
        self._last_output = ""
        self._header_model = ""
        self._header_workspace = ""
        self._transcript = ""
        self._conversation_started = False
        self._follow_output = True
        self._ignore_next_scroll_inference = False
        self.input_area = TextArea(
            multiline=True,
            completer=CommandCompleter(reference_workspace),
            complete_while_typing=True,
            history=DummyHistory(),
            wrap_lines=True,
            focus_on_click=True,
            read_only=Condition(
                lambda: not self._resetting_input
                and (self._pending is None or self._pending.approval)
            ),
            height=Dimension(min=1, max=4),
            style="class:input",
        )
        self._input_buffer = self.input_area.buffer
        self._input_buffer.on_text_changed += self._on_input_text_changed
        self._input_area_container = ConditionalContainer(
            self.input_area,
            Condition(
                lambda: self._question_pending is None
                and (self._pending is None or not self._pending.approval)
            ),
        )
        self._empty_title_window = Window(
            FormattedTextControl("准备就绪"),
            height=1,
            align=WindowAlign.CENTER,
            style="class:empty.title",
        )
        self._empty_hint_window = Window(
            FormattedTextControl(lambda: self._empty_hint()),
            height=1,
            align=WindowAlign.CENTER,
            style="class:empty.hint",
        )
        self._empty_state = ConditionalContainer(
            HSplit(
                [
                    Window(
                        height=Dimension(weight=1), style="class:empty.background"
                    ),
                    self._empty_title_window,
                    self._empty_hint_window,
                    Window(
                        height=Dimension(weight=1), style="class:empty.background"
                    ),
                ]
            ),
            Condition(lambda: not self._conversation_started),
        )
        self.output_area = TextArea(
            text="",
            multiline=True,
            read_only=True,
            focusable=True,
            focus_on_click=True,
            wrap_lines=True,
            scrollbar=True,
            lexer=_TranscriptDiffLexer(),
            height=Dimension(min=1, max=8),
            style="class:transcript",
        )
        self.output_area.window.right_margins = [ScrollbarMargin(display_arrows=False)]
        self._conversation_body = HSplit(
            [
                ConditionalContainer(
                    self.output_area,
                    Condition(lambda: bool(self._transcript)),
                ),
                self._empty_state,
            ]
        )
        self._approval_area = TextArea(
            text="",
            multiline=True,
            read_only=True,
            focusable=False,
            wrap_lines=True,
            scrollbar=False,
            height=Dimension(min=1, max=4),
            style="class:approval",
        )
        self._approval_prompt_area = TextArea(
            text="",
            multiline=True,
            read_only=True,
            focusable=False,
            wrap_lines=True,
            scrollbar=False,
            height=Dimension(min=1, max=3),
            style="class:approval.heading",
        )
        self._approval_no_button = Button(
            "拒绝", handler=self._select_no, width=10
        )
        self._approval_yes_button = Button(
            "本次批准", handler=self._select_yes, width=12
        )
        self._approval_container = ConditionalContainer(
            HSplit(
                [
                    Window(
                        FormattedTextControl("待审批 · 默认拒绝"),
                        height=1,
                        style="class:approval.heading",
                    ),
                    ConditionalContainer(
                        self._approval_area,
                        Condition(lambda: bool(self._approval_text)),
                    ),
                    self._approval_prompt_area,
                    VSplit(
                        [self._approval_no_button, self._approval_yes_button],
                        padding=1,
                    ),
                ]
            ),
            Condition(
                lambda: self._pending is not None and self._pending.approval
            ),
        )
        self._question_heading = Window(
            FormattedTextControl(lambda: self._question_heading_text()),
            height=1,
            style="class:approval.heading",
        )
        self._question_prompt_area = TextArea(
            text="",
            multiline=True,
            read_only=True,
            focusable=False,
            wrap_lines=True,
            scrollbar=False,
            height=Dimension(min=2, max=4),
            style="class:approval",
        )
        self._question_answer_area = TextArea(
            text="",
            multiline=True,
            history=DummyHistory(),
            wrap_lines=True,
            focus_on_click=True,
            height=Dimension(min=2, max=4),
            style="class:input",
        )
        self._question_answer_area.buffer.on_text_changed += self._on_question_text_changed
        self._question_review_area = TextArea(
            text="",
            multiline=True,
            read_only=True,
            focusable=True,
            wrap_lines=True,
            scrollbar=True,
            height=Dimension(min=3, max=7),
            style="class:approval",
        )
        self._question_review_area.window.right_margins = [
            ScrollbarMargin(display_arrows=False)
        ]
        self._question_suggestion_buttons = [
            Button("", handler=lambda index=index: self._fill_question_suggestion(index))
            for index in range(4)
        ]
        self._question_prev_button = Button("上一题", handler=self._question_previous, width=12)
        self._question_next_button = Button("下一题", handler=self._question_next, width=12)
        self._question_review_button = Button("查看回答", handler=self._question_show_review, width=14)
        self._question_cancel_button = Button("取消提问", handler=self._question_cancel, width=14)
        self._question_edit_button = Button("返回修改", handler=self._question_edit, width=14)
        self._question_save_button = Button(
            "存档：仅本次（默认）", handler=self._question_toggle_session_save, width=26
        )
        self._question_save_unavailable = Window(
            FormattedTextControl(
                "当前对话含未存档上下文，本问卷不能随 /保存 存档；恢复后可能需要重新提供背景。"
            ),
            height=Dimension(min=1, max=2),
            wrap_lines=True,
            style="class:approval",
        )
        self._question_submit_button = Button("提交回答", handler=self._question_submit, width=14)
        self._question_input_container = ConditionalContainer(
            HSplit(
                [
                    self._question_heading,
                    self._question_prompt_area,
                    self._question_suggestion_row(),
                    self._question_answer_area,
                    VSplit(
                        [self._question_prev_button, self._question_next_button,
                         self._question_review_button, self._question_cancel_button],
                        padding=1,
                    ),
                ]
            ),
            Condition(lambda: self._question_pending is not None and not self._question_reviewing),
        )
        self._question_review_container = ConditionalContainer(
            HSplit(
                [
                    Window(FormattedTextControl("审阅本次回答 · 不会自动提交"), height=1,
                           style="class:approval.heading"),
                    self._question_review_area,
                    ConditionalContainer(
                        VSplit([self._question_save_button], padding=1),
                        Condition(
                            lambda: self._question_pending is not None
                            and self._question_pending.request.save_allowed
                        ),
                    ),
                    ConditionalContainer(
                        self._question_save_unavailable,
                        Condition(
                            lambda: self._question_pending is not None
                            and not self._question_pending.request.save_allowed
                        ),
                    ),
                    VSplit([self._question_edit_button, self._question_submit_button,
                            self._question_cancel_button], padding=1),
                ]
            ),
            Condition(lambda: self._question_pending is not None and self._question_reviewing),
        )
        self._completion_menu = CompletionsMenu(max_height=6, scroll_offset=1)
        self._completion_window = self._completion_menu.content
        self._completion_menu_row = ConditionalContainer(
            VSplit(
                [
                    self._completion_menu,
                    Window(width=Dimension(weight=1), style="class:completion-menu"),
                ]
            ),
            Condition(
                lambda: self._input_buffer.complete_state is not None
                and bool(self._input_buffer.complete_state.completions)
            ),
        )
        self._input_top_separator = Window(
            char="─", height=1, style="class:input.separator"
        )
        self._prompt_window = Window(
            FormattedTextControl(lambda: self._prompt_label()),
            height=1,
            style="class:prompt",
        )
        self._input_bottom_separator = Window(
            char="─", height=1, style="class:input.separator"
        )
        self._status_window = Window(
            FormattedTextControl(lambda: self._status_line()),
            height=1,
            style="class:status",
        )
        input_block = HSplit(
            [
                self._input_top_separator,
                self._prompt_window,
                self._input_area_container,
                self._input_bottom_separator,
            ]
        )
        self._input_block_container = ConditionalContainer(
            input_block,
            Condition(
                lambda: self._question_pending is None
                and (self._pending is None or not self._pending.approval)
            ),
        )
        content = HSplit(
            [
                Window(
                    FormattedTextControl(lambda: self._header_fragments()),
                    height=1,
                    style="class:header",
                ),
                self._conversation_body,
                self._completion_menu_row,
                self._approval_container,
                self._question_input_container,
                self._question_review_container,
                self._input_block_container,
                self._status_window,
            ]
        )
        self._bindings = self._build_key_bindings()
        self._application = Application(
            layout=Layout(content, focused_element=self.input_area),
            key_bindings=self._bindings,
            style=Style.from_dict(_style_rules_for_environment()),
            full_screen=True,
            erase_when_done=False,
            mouse_support=True,
            input=input,
            output=output,
            clipboard=DummyClipboard(),
        )

    @property
    def cancellation_event(self) -> threading.Event:
        return self._cancel_event

    def input(self, prompt: str) -> str:
        request = _InputRequest(
            prompt=prompt,
            approval=_APPROVAL_PROMPT.search(prompt) is not None,
            completed=threading.Event(),
        )
        self._events.put(("input", request))
        request.completed.wait()
        if request.error is not None:
            raise request.error
        if request.value is None:
            raise EOFError()
        return request.value

    def ask_questions(self, request: QuestionnaireRequest) -> QuestionnaireResult:
        pending = _QuestionRequest(request, threading.Event())
        self._queued_question = pending
        self._events.put(("questions", pending))
        cancel_queued = False
        while not pending.completed.wait(0.05):
            if self._cancel_event.is_set() and not cancel_queued:
                self._events.put(("question_cancel", pending))
                cancel_queued = True
        if pending.error is not None:
            raise pending.error
        return pending.result or QuestionnaireResult("cancelled")

    def output(self, text: str) -> None:
        self._events.put(("output", text))

    def stream_write(self, text: str) -> None:
        self._events.put(("stream", text))

    def run(self) -> int:
        self._worker_thread = threading.Thread(
            target=self._run_worker,
            name="cvfx-tui-cli-worker",
            daemon=True,
        )

        def start_worker() -> None:
            if self._worker_started:
                return
            self._worker_started = True
            self._worker_thread.start()
            self._application.create_background_task(self._poll_events())

        try:
            return int(self._application.run(pre_run=start_worker))
        finally:
            if self._pending is not None:
                self._resolve_pending(error=KeyboardInterrupt())
            if self._question_pending is not None:
                self._finish_questionnaire(error=KeyboardInterrupt())
            elif self._queued_question is not None:
                self._queued_question.error = KeyboardInterrupt()
                self._queued_question.completed.set()
                self._queued_question = None
            self._cancel_event.set()
            if self._worker_thread.is_alive():
                self._worker_thread.join(timeout=2)

    def _run_worker(self) -> None:
        try:
            result = self._worker_callable(self)
            result = result if type(result) is int else 2
        except BaseException:
            # Never propagate raw exception text from worker code into the terminal.
            result = 2
            self._events.put(("output", "TUI 后台任务意外结束；原始错误信息已隐藏。"))
        self._events.put(("done", result))

    async def _poll_events(self) -> None:
        while not self._worker_done:
            handled = False
            while True:
                try:
                    kind, value = self._events.get_nowait()
                except queue.Empty:
                    break
                handled = True
                if kind == "output":
                    safe = _safe_text(str(value))
                    self._last_output = safe
                    if safe.startswith("chenxiVFX Harness v"):
                        self._header_model = safe.removeprefix("chenxiVFX Harness ")
                    elif safe.startswith("工作区："):
                        self._header_workspace = safe.removeprefix("工作区：")
                    else:
                        self._append_transcript(safe + "\n")
                    if "已取消当前" in safe:
                        self._status = "已取消"
                    elif self._pending is None:
                        self._status = "运行中"
                elif kind == "stream":
                    self._append_transcript(_safe_text(str(value)))
                    if self._pending is None:
                        self._status = "运行中"
                elif kind == "input" and isinstance(value, _InputRequest):
                    self._clear_input_buffer()
                    self._prompt = _safe_text(value.prompt).strip()
                    if value.approval:
                        self._prompt = _APPROVAL_PROMPT.sub("", self._prompt).rstrip()
                    self._notice = ""
                    if value.approval:
                        self._status = "等待审批"
                        self._approval_choice_yes = False
                        self._update_approval_buttons()
                        context = self._last_output
                        patch_context = context.lstrip("\n")
                        if patch_context.startswith("补丁差异\n"):
                            # The full diff remains in the transcript above; the
                            # approval dock keeps just the target and trusted
                            # file fingerprint next to the decision buttons.
                            self._approval_text = "\n".join(patch_context.splitlines()[1:3])
                        elif patch_context.startswith(("多文件变更集完整审阅", "变更集恢复完整审阅")):
                            # Keep the decision area compact; the complete
                            # rolling diff stays in the scrollable transcript.
                            self._approval_text = "\n".join(patch_context.splitlines()[:2])
                        elif patch_context.startswith("工作区文本搜索审批范围"):
                            self._approval_text = "\n".join(patch_context.splitlines()[:2])
                        elif any(marker in context for marker in ("差异", "审批范围")):
                            self._approval_text = _safe_text(context)
                        else:
                            self._approval_text = ""
                        self._approval_area.text = self._approval_text
                        self._approval_prompt_area.text = self._prompt
                    else:
                        self._application.layout.focus(self.input_area)
                        self._cancel_event.clear()
                        self._cancel_requested = False
                        self._approval_choice_yes = False
                        self._approval_text = ""
                        self._approval_area.text = ""
                        self._approval_prompt_area.text = ""
                        self._status = "等待输入"
                    self._application.layout.focus(
                        self._approval_no_button if value.approval else self.input_area
                    )
                    self._pending = value
                    self._request_ready.set()
                elif kind == "questions" and isinstance(value, _QuestionRequest):
                    if self._queued_question is value:
                        self._queued_question = None
                    self._question_pending = value
                    self._question_index = 0
                    self._question_answers = [""] * len(value.request.questions)
                    self._question_reviewing = False
                    self._question_save_with_session = False
                    self._question_save_button.text = (
                        "存档：仅本次（默认）"
                        if value.request.save_allowed
                        else "存档：当前不可用"
                    )
                    self._question_answer_area.text = ""
                    self._question_review_area.text = ""
                    self._question_set_current()
                    self._status = "等待回答"
                    self._notice = (
                        "建议答案只会填入草稿；查看回答后还需明确提交。"
                        if value.request.save_allowed
                        else "当前对话含未存档上下文；本问卷无法存档，提交前请先审阅。"
                    )
                    self._application.layout.focus(self._question_answer_area)
                    self._question_ready.set()
                elif kind == "question_cancel" and isinstance(value, _QuestionRequest):
                    if self._question_pending is value:
                        self._finish_questionnaire(error=KeyboardInterrupt())
                elif kind == "done":
                    self._worker_result = value if type(value) is int else 2
                    self._worker_done = True
                    self._status = "会话已结束" if self._worker_result == 0 else "会话结束（错误）"
            if self._worker_done:
                self._application.exit(result=self._worker_result)
                return
            if handled:
                self._application.invalidate()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _build_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter", eager=True)
        def submit(event) -> None:
            if self._question_pending is not None:
                if event.app.layout.has_focus(self._question_answer_area):
                    self._question_answer_area.buffer.insert_text("\n")
                else:
                    self._activate_question_focused_button()
                return
            if self._pending is not None and (
                event.app.layout.has_focus(self.input_area)
                or event.app.layout.has_focus(self._approval_no_button)
                or event.app.layout.has_focus(self._approval_yes_button)
            ):
                if (
                    not self._pending.approval
                    and event.app.layout.has_focus(self.input_area)
                    and self._accept_completion_or_submit()
                ):
                    return
                self._submit_input()

        @bindings.add("tab", eager=True)
        def accept_completion(event) -> None:
            if self._question_pending is not None:
                self._focus_question_control(forward=True)
                return
            if self._pending is None:
                return
            if self._pending is not None and self._pending.approval:
                self._toggle_approval_choice()
                return
            if not event.app.layout.has_focus(self.input_area):
                return
            buffer = self._input_buffer
            if buffer.complete_state is None:
                buffer.start_completion(select_first=True)
            completion_state = buffer.complete_state
            if completion_state is not None:
                if completion_state.current_completion is None:
                    buffer.complete_next()
                    completion_state = buffer.complete_state
                if completion_state is not None and completion_state.current_completion is not None:
                    buffer.apply_completion(completion_state.current_completion)

        @bindings.add("s-tab", eager=True)
        def previous_question_control(event) -> None:
            if self._question_pending is not None:
                self._focus_question_control(forward=False)
                return

        @bindings.add("up", eager=True)
        def previous_completion(event) -> None:
            if self._question_pending is not None:
                if event.app.layout.has_focus(self._question_answer_area):
                    self._question_answer_area.buffer.cursor_up()
                elif (
                    self._question_reviewing
                    and event.app.layout.has_focus(self._question_review_area)
                ):
                    self._question_review_area.buffer.cursor_up()
                else:
                    self._focus_question_control(forward=False)
                return
            if self._pending is None:
                return
            if self._pending is not None and self._pending.approval:
                self._select_yes()
                return
            if not event.app.layout.has_focus(self.input_area):
                return
            buffer = self._input_buffer
            if buffer.complete_state is not None:
                buffer.complete_previous()
            else:
                buffer.cursor_up()

        @bindings.add("down", eager=True)
        def next_completion(event) -> None:
            if self._question_pending is not None:
                if event.app.layout.has_focus(self._question_answer_area):
                    self._question_answer_area.buffer.cursor_down()
                elif (
                    self._question_reviewing
                    and event.app.layout.has_focus(self._question_review_area)
                ):
                    self._question_review_area.buffer.cursor_down()
                else:
                    self._focus_question_control(forward=True)
                return
            if self._pending is None:
                return
            if self._pending is not None and self._pending.approval:
                self._select_no()
                return
            if not event.app.layout.has_focus(self.input_area):
                return
            buffer = self._input_buffer
            if buffer.complete_state is not None:
                buffer.complete_next()
            else:
                buffer.cursor_down()

        @bindings.add("escape", eager=True)
        def escape(event) -> None:
            if self._question_pending is not None:
                self._question_cancel()
                return
            buffer = self._input_buffer
            if buffer.complete_state is not None:
                buffer.cancel_completion()
                self._notice = "建议已关闭。"
            elif self._pending is not None:
                self._resolve_pending(error=KeyboardInterrupt())
            elif not self._worker_done:
                self._request_cancel()

        @bindings.add("c-c", eager=True)
        def clear_or_cancel(event) -> None:
            if self._question_pending is not None:
                self._cancel_requested = True
                self._cancel_event.set()
                self._finish_questionnaire(error=KeyboardInterrupt())
                return
            buffer = self._input_buffer
            if buffer.text:
                buffer.text = ""
                buffer.cancel_completion()
                self._notice = "已清空草稿；尚未提交。"
            elif self._pending is not None:
                self._resolve_pending(error=KeyboardInterrupt())
            elif not self._worker_done:
                self._request_cancel()

        @bindings.add("c-j", eager=True)
        def insert_newline(event) -> None:
            if self._question_pending is not None:
                if not self._question_reviewing:
                    self._question_answer_area.buffer.insert_text("\n")
                return
            if self._pending is None:
                self._notice = "后台生成期间已暂停输入。"
                return
            if self._pending.approval:
                self._notice = "审批回答只能单行输入。"
                return
            self._input_buffer.insert_text("\n")

        @bindings.add("c-d", eager=True)
        def end_of_input(event) -> None:
            if self._pending is not None:
                self._resolve_pending(eof=True)

        @bindings.add("pageup", eager=True)
        def scroll_output_up(event) -> None:
            if (
                self._question_pending is not None
                and self._question_reviewing
                and event.app.layout.has_focus(self._question_review_area)
            ):
                self._question_review_area.buffer.cursor_up(count=4)
                return
            if self._input_buffer.complete_state is None:
                self._move_output_cursor(-max(2, self.output_area.window.height.preferred or 10))

        @bindings.add("pagedown", eager=True)
        def scroll_output_down(event) -> None:
            if (
                self._question_pending is not None
                and self._question_reviewing
                and event.app.layout.has_focus(self._question_review_area)
            ):
                self._question_review_area.buffer.cursor_down(count=4)
                return
            if self._input_buffer.complete_state is None:
                self._move_output_cursor(max(2, self.output_area.window.height.preferred or 10))

        @bindings.add("c-end", eager=True)
        def output_bottom(event) -> None:
            if self._input_buffer.complete_state is None:
                self._follow_output = True
                self.output_area.buffer.cursor_position = len(self.output_area.text)

        shortcut_commands = {
            "help": "/帮助",
            "commands": "/命令",
            "status": "/状态",
            "config": "/配置",
        }
        for key, action in self.tui_shortcuts.items():
            command = shortcut_commands[action]

            def fill_safe_command(event, *, command=command, key=key) -> None:
                if (
                    self._question_pending is not None
                    or self._pending is None
                    or self._pending.approval
                    or not event.app.layout.has_focus(self.input_area)
                ):
                    self._notice = f"{key} 仅在普通输入提示中可用；当前状态未改变。"
                    return
                if self._input_buffer.text:
                    self._notice = f"{key} 未覆盖现有草稿；请先清空输入或自行编辑。"
                    return
                self._input_buffer.text = command
                self._input_buffer.cursor_position = len(command)
                self._notice = f"{key} 已填入 {command}；按 Enter 提交，尚未执行。"

            bindings.add(key.casefold(), eager=True)(fill_safe_command)

        return bindings

    def _on_input_text_changed(self, buffer) -> None:
        if buffer.text:
            self._notice = ""

    def _on_question_text_changed(self, buffer) -> None:
        if buffer.text and self._question_pending is not None:
            self._notice = ""

    def _accept_completion_or_submit(self) -> bool:
        """Return True when Enter filled a candidate instead of submitting."""
        buffer = self._input_buffer
        state = buffer.complete_state
        if state is None or not state.completions:
            return False

        document = buffer.document
        exact_match = any(
            document.text[: document.cursor_position + completion.start_position]
            + completion.text
            + document.text[document.cursor_position:]
            == buffer.text
            for completion in state.completions
        )
        if exact_match:
            buffer.cancel_completion()
            return False

        if state.current_completion is None:
            buffer.complete_next()
            state = buffer.complete_state
        if state is not None and state.current_completion is not None:
            buffer.apply_completion(state.current_completion)
            return True
        return False

    def _submit_input(self) -> None:
        request = self._pending
        if request is None:
            return
        if request.approval:
            value = "是" if self._approval_choice_yes else ""
            choice = "是（本次）" if self._approval_choice_yes else "否（默认拒绝）"
            self._append_transcript(f"\n审批选择：{choice}\n")
        else:
            value = self._input_buffer.text
            if value:
                self._conversation_started = True
                self.output_area.window.height = Dimension(min=1, weight=1)
                shown_value = (
                    "[疑似凭据输入已隐藏]"
                    if contains_likely_secret(value)
                    else _safe_text(value)
                )
                self._append_transcript(f"\n你> {shown_value}\n")
                # Submitting a prompt is not a manual transcript scroll. A
                # multiline or very long input can resize the transcript pane
                # before the worker produces its first response; ignore that
                # stale render position once and follow the new response.
                self._follow_output = True
                self.output_area.buffer.cursor_position = len(self._transcript)
                self._ignore_next_scroll_inference = True
        self._resolve_pending(value=value)

    def _resolve_pending(
        self,
        *,
        value: str | None = None,
        error: BaseException | None = None,
        eof: bool = False,
    ) -> None:
        request = self._pending
        if request is None:
            return
        request.value = None if eof else value
        request.error = error
        self._clear_input_buffer()
        self._pending = None
        self._prompt = ""
        self._approval_text = ""
        self._approval_area.text = ""
        self._approval_prompt_area.text = ""
        self._last_output = ""
        self._request_ready.clear()
        self._status = "运行中"
        request.completed.set()

    def _clear_input_buffer(self) -> None:
        self._resetting_input = True
        try:
            self._input_buffer.text = ""
        finally:
            self._resetting_input = False

    def _request_cancel(self) -> None:
        self._cancel_requested = True
        self._cancel_event.set()
        self._status = "正在取消"
        self._notice = "已请求取消；等待当前安全检查点。"

    def _append_transcript(self, text: str) -> None:
        info = self.output_area.window.render_info
        if info is not None and not self._ignore_next_scroll_inference:
            at_bottom = info.vertical_scroll >= max(
                0, info.content_height - info.window_height
            )
            if not at_bottom:
                self._follow_output = False
        self._ignore_next_scroll_inference = False
        old_cursor = self.output_area.buffer.cursor_position
        self._transcript += text
        if len(self._transcript) > MAX_TRANSCRIPT_CHARS:
            excess = len(self._transcript) - MAX_TRANSCRIPT_CHARS
            newline = self._transcript.find("\n", excess)
            cut = newline + 1 if newline >= 0 else excess
            self._transcript = "[较早的终端输出已折叠]\n" + self._transcript[cut:]
            old_cursor = max(0, old_cursor - cut)
        self.output_area.text = self._transcript
        if self._follow_output:
            self.output_area.buffer.cursor_position = len(self._transcript)
        else:
            self.output_area.buffer.cursor_position = min(old_cursor, len(self._transcript))

    def _move_output_cursor(self, rows: int) -> None:
        document = self.output_area.buffer.document
        current_row, current_col = document.translate_index_to_position(
            self.output_area.buffer.cursor_position
        )
        target_row = max(0, current_row + rows)
        target_row = min(target_row, document.line_count - 1)
        target_index = document.translate_row_col_to_index(target_row, 0)
        self.output_area.buffer.cursor_position = target_index
        self._follow_output = target_row >= document.line_count - 1

    def _prompt_label(self) -> str:
        if not self._pending:
            return "等待后台任务…"
        return self._prompt or "输入"

    def _empty_hint(self) -> str:
        try:
            columns = self._application.output.get_size().columns
        except (AttributeError, NotImplementedError, OSError):
            columns = 80
        if columns >= 42:
            return "输入任务开始  ·  输入 / 查看命令"
        return "输入任务  ·  /命令"

    def _header_fragments(self) -> list[tuple[str, str]]:
        brand = "chenxiVFX Harness"
        metadata = self._header_model
        if metadata:
            parts = [part.strip() for part in metadata.split("|")]
            if len(parts) >= 3:
                version = parts[0]
                provider = (
                    parts[1]
                    .removeprefix("提供商：")
                    .removeprefix("提供商:")
                    .strip()
                )
                model = (
                    parts[2]
                    .removeprefix("模型：")
                    .removeprefix("模型:")
                    .strip()
                )
                metadata = f"{version}  {provider} / {model}"
        workspace = _short_workspace_label(self._header_workspace) if self._header_workspace else ""
        try:
            columns = max(0, self._application.output.get_size().columns)
        except (AttributeError, NotImplementedError, OSError):
            columns = 80

        brand_width = max(0, wcswidth(brand))
        if columns <= brand_width:
            return [("class:header.brand", _truncate_cells(brand, columns))]

        has_metadata = bool(metadata)
        has_workspace = bool(workspace)
        separator_width = 2 * (int(has_metadata) + int(has_workspace))
        content_width = max(0, columns - brand_width - separator_width)
        if content_width == 0:
            return [("class:header.brand", brand)]
        if has_metadata and has_workspace and content_width < 12:
            metadata_budget = content_width
            workspace_budget = 0
        elif has_metadata and has_workspace:
            workspace_budget = min(
                max(0, wcswidth(workspace)),
                max(1, content_width // 3),
            )
            metadata_budget = max(0, content_width - workspace_budget)
            if wcswidth(metadata) < metadata_budget:
                workspace_budget = min(
                    max(0, wcswidth(workspace)),
                    workspace_budget + metadata_budget - wcswidth(metadata),
                )
                metadata_budget = content_width - workspace_budget
        elif has_metadata:
            metadata_budget = content_width
            workspace_budget = 0
        else:
            metadata_budget = 0
            workspace_budget = content_width

        fragments = [("class:header.brand", brand)]
        if has_metadata and metadata_budget:
            fragments.append(
                ("class:header.muted", f"  {_truncate_cells(metadata, metadata_budget)}")
            )
        if has_workspace and workspace_budget:
            fragments.append(
                (
                    "class:header.workspace",
                    f"  {_truncate_cells(workspace, workspace_budget, from_start=True)}",
                )
            )
        return fragments

    def _select_no(self) -> None:
        self._approval_choice_yes = False
        self._update_approval_buttons()
        self._application.layout.focus(self._approval_no_button)
        self._notice = "当前选择：否；按 Enter 拒绝本次操作。"

    def _select_yes(self) -> None:
        self._approval_choice_yes = True
        self._update_approval_buttons()
        self._application.layout.focus(self._approval_yes_button)
        self._notice = "当前选择：是；按 Enter 仅批准本次操作。"

    def _toggle_approval_choice(self) -> None:
        if self._approval_choice_yes:
            self._select_no()
        else:
            self._select_yes()

    def _update_approval_buttons(self) -> None:
        # Focus styling indicates the current choice; the labels stay short so
        # the review and both actions remain legible in narrow terminals.
        self._approval_no_button.text = "拒绝"
        self._approval_yes_button.text = "本次批准"

    def _question_suggestion_row(self):
        rows = []
        for start in (0, 2):
            buttons = []
            for index in range(start, start + 2):
                buttons.append(
                    ConditionalContainer(
                        self._question_suggestion_buttons[index],
                        Condition(
                            lambda index=index: self._question_has_suggestion(index)
                        ),
                    )
                )
            rows.append(VSplit(buttons, padding=1))
        return HSplit(rows)

    def _question_has_suggestion(self, index: int) -> bool:
        pending = self._question_pending
        if pending is None or not 0 <= self._question_index < len(pending.request.questions):
            return False
        return index < len(pending.request.questions[self._question_index].suggestions)

    def _question_heading_text(self) -> str:
        pending = self._question_pending
        if pending is None:
            return ""
        return f"模型提问 · {self._question_index + 1}/{len(pending.request.questions)}"

    def _focus_question_control(self, *, forward: bool) -> None:
        pending = self._question_pending
        if pending is None:
            return
        if self._question_reviewing:
            controls = [
                self._question_review_area,
                self._question_edit_button,
                self._question_submit_button,
                self._question_cancel_button,
            ]
            if pending.request.save_allowed:
                controls.insert(2, self._question_save_button)
        else:
            suggestions = pending.request.questions[self._question_index].suggestions
            controls = [self._question_answer_area]
            controls.extend(self._question_suggestion_buttons[: len(suggestions)])
            controls.extend(
                [
                    self._question_prev_button,
                    self._question_next_button,
                    self._question_review_button,
                    self._question_cancel_button,
                ]
            )
        current = next(
            (index for index, control in enumerate(controls)
             if self._application.layout.has_focus(control)),
            -1 if forward else 0,
        )
        next_index = (current + (1 if forward else -1)) % len(controls)
        self._application.layout.focus(controls[next_index])

    def _question_set_current(self) -> None:
        pending = self._question_pending
        if pending is None:
            return
        question = pending.request.questions[self._question_index]
        self._question_prompt_area.text = _safe_text(question.prompt)
        try:
            columns = self._application.output.get_size().columns
        except (AttributeError, NotImplementedError, OSError):
            columns = 80
        suggestion_width = max(8, min(30, (columns - 4) // 2 - 4))
        for index, button in enumerate(self._question_suggestion_buttons):
            if index < len(question.suggestions):
                button.text = (
                    f"{index + 1}. "
                    f"{_truncate_cells(_safe_text(question.suggestions[index]), suggestion_width)}"
                )
            else:
                button.text = ""
        self._question_answer_area.text = self._question_answers[self._question_index]
        self._question_answer_area.buffer.cursor_position = len(self._question_answer_area.text)

    def _question_save_current(self) -> None:
        if self._question_pending is not None and not self._question_reviewing:
            self._question_answers[self._question_index] = self._question_answer_area.text

    def _fill_question_suggestion(self, index: int) -> None:
        pending = self._question_pending
        if pending is None or not self._question_has_suggestion(index):
            return
        self._question_answer_area.text = pending.request.questions[
            self._question_index
        ].suggestions[index]
        self._question_answer_area.buffer.cursor_position = len(self._question_answer_area.text)
        self._question_answers[self._question_index] = self._question_answer_area.text
        self._application.layout.focus(self._question_answer_area)
        self._notice = "建议已填入草稿；检查后再审阅和提交。"

    def _question_previous(self) -> None:
        if self._question_pending is None:
            return
        self._question_save_current()
        if self._question_index > 0:
            self._question_index -= 1
            self._question_set_current()
        self._application.layout.focus(self._question_answer_area)

    def _question_next(self) -> None:
        pending = self._question_pending
        if pending is None:
            return
        self._question_save_current()
        if self._question_index + 1 < len(pending.request.questions):
            self._question_index += 1
            self._question_set_current()
            self._application.layout.focus(self._question_answer_area)
        else:
            self._question_show_review()

    def _question_show_review(self) -> None:
        if self._question_pending is None:
            return
        self._question_save_current()
        self._question_reviewing = True
        entries = []
        for question, answer in zip(
            self._question_pending.request.questions, self._question_answers
        ):
            display_answer = _safe_text(answer) if answer else "（空回答）"
            entries.append(
                f"{question.question_id} · {_safe_text(question.prompt)}\n回答：{display_answer}"
            )
        self._question_review_area.text = "\n\n".join(entries)
        self._question_review_area.text = (
            (
                "隐私：默认仅本次；切换为允许本地保存后，本问卷仅在 /保存 时写入工作区会话。\n\n"
                if self._question_pending.request.save_allowed
                else "隐私：当前对话含未存档上下文，本问卷不能随 /保存 存档；恢复后可能需要重新提供背景。\n\n"
            )
            + self._question_review_area.text
        )
        self._question_save_button.text = (
            "存档：允许本地保存" if self._question_save_with_session
            else "存档：仅本次（默认）" if self._question_pending.request.save_allowed
            else "存档：当前不可用"
        )
        self._application.layout.focus(self._question_edit_button)
        self._notice = (
            "检查全部回答；选择存档方式后，仍需按“提交回答”才会发送给本轮模型。"
            if self._question_pending.request.save_allowed
            else "检查全部回答；本问卷无法存档，按“提交回答”后才会发送给本轮模型。"
        )

    def _question_toggle_session_save(self) -> None:
        if (
            self._question_pending is None
            or not self._question_reviewing
            or not self._question_pending.request.save_allowed
        ):
            return
        self._question_save_with_session = not self._question_save_with_session
        self._question_save_button.text = (
            "存档：允许本地保存" if self._question_save_with_session
            else "存档：仅本次（默认）"
        )
        self._notice = (
            "已允许本问卷随之后的 /保存 写入工作区本地会话。"
            if self._question_save_with_session
            else "本问卷仍设为仅本次使用；受其影响的上下文不会进入会话存档。"
        )

    def _question_edit(self) -> None:
        if self._question_pending is None:
            return
        self._question_reviewing = False
        self._question_set_current()
        self._application.layout.focus(self._question_answer_area)

    def _question_submit(self) -> None:
        if self._question_pending is None:
            return
        self._question_save_current()
        try:
            answers = validate_question_answers(
                self._question_pending.request, self._question_answers
            )
        except QuestionRequestError as exc:
            if exc.code == "credential_request":
                self._notice = "检测到疑似凭据；回答未提交，请使用 /配置 或受支持的环境变量。"
                self._finish_questionnaire(
                    QuestionnaireResult("blocked_credential")
                )
            else:
                self._notice = f"回答无效或超过 {MAX_TOTAL_ANSWER_BYTES} bytes 上限；请返回修改。"
            return
        self._finish_questionnaire(
            QuestionnaireResult(
                "submitted",
                answers,
                self._question_save_with_session
                and self._question_pending.request.save_allowed,
            )
        )

    def _question_cancel(self) -> None:
        if self._question_pending is not None:
            self._finish_questionnaire(QuestionnaireResult("cancelled"))

    def _finish_questionnaire(
        self,
        result: QuestionnaireResult | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        pending = self._question_pending
        if pending is None:
            return
        pending.result = result
        pending.error = error
        self._question_pending = None
        self._question_answers = []
        self._question_index = 0
        self._question_reviewing = False
        self._question_save_with_session = False
        self._question_answer_area.text = ""
        self._question_prompt_area.text = ""
        self._question_review_area.text = ""
        self._question_ready.clear()
        self._queued_question = None
        self._question_reviewing = False
        if error is not None:
            self._notice = "提问已取消；本轮模型请求已中止。"
        elif result is not None and result.status == "cancelled":
            self._notice = "已取消提问；没有提交回答。"
        elif result is not None and result.status == "blocked_credential":
            self._notice = "检测到疑似凭据；回答未提交，请使用 /配置 或受支持的环境变量。"
        else:
            self._notice = "回答已提交给本轮模型，作为不可信上下文。"
        self._status = "运行中"
        self._application.layout.focus(self.input_area)
        pending.completed.set()

    def _activate_question_focused_button(self) -> None:
        buttons = [*self._question_suggestion_buttons,
                   self._question_prev_button, self._question_next_button,
                   self._question_review_button, self._question_cancel_button,
                   self._question_edit_button]
        if self._question_pending is not None and self._question_pending.request.save_allowed:
            buttons.append(self._question_save_button)
        buttons.append(self._question_submit_button)
        for button in buttons:
            if self._application.layout.has_focus(button):
                button.handler()
                return

    def _status_line(self) -> str:
        try:
            columns = self._application.output.get_size().columns
        except (AttributeError, NotImplementedError, OSError):
            columns = 80

        if self._question_pending is not None:
            if self._question_reviewing:
                line = "回答审阅 · ↑↓查看 · Tab切换 · Enter激活 · Esc取消"
            elif columns >= 48:
                line = "回答问题 · Enter换行 · Tab切换 · 查看回答后提交 · Esc取消"
            else:
                line = "回答 · Enter换行 · Esc取消"
            if self._notice:
                line = self._notice
        elif self._pending is not None and self._pending.approval:
            if columns >= 48:
                line = "PgUp/PgDn审阅 · Tab/↑↓选择 · Enter确认 · Esc取消"
            elif columns >= 38:
                line = "Pg↑↓审阅 · Tab选 · Enter确认 · Esc取消"
            elif columns >= 30:
                line = "Pg↑↓审阅 · Enter确认 · Esc取消"
            else:
                line = "Enter确认 · Esc取消"
        elif self._notice:
            line = f"{self._status}  ·  {self._notice}"
        elif self._pending is None and not self._worker_done:
            line = "生成中  ·  Ctrl+C 取消"
        elif self._pending is None:
            line = self._status
        else:
            if columns >= 36:
                line = "等待输入 · Tab补全 · Enter提交"
            elif columns >= 26:
                line = "等待输入 · Enter提交"
            else:
                line = "输入 · Enter"

        if wcswidth(line) <= columns:
            return line
        limit = max(0, columns - 1)
        clipped: list[str] = []
        used = 0
        for character in line:
            width = max(0, wcswidth(character))
            if used + width > limit:
                break
            clipped.append(character)
            used += width
        return "".join(clipped) + ("…" if columns else "")


def run_tui_console(args) -> int | None:
    """Run the existing CLI in a worker, returning None when TUI setup is unsupported."""
    from .cli import run_console

    def worker(ui: TerminalUI) -> int:
        return run_console(
            args,
            selected_workspace=reference_workspace,
            input_fn=ui.input,
            output=ui.output,
            stream_write=ui.stream_write,
            cancellation_event=ui.cancellation_event,
            ask_user_fn=ui.ask_questions,
        )

    try:
        reference_workspace = Workspace(args.workspace)
    except (WorkspaceError, OSError, RuntimeError):
        reference_workspace = None
    shortcuts: Mapping[str, str] = {}
    if reference_workspace is not None and not getattr(args, "ignore_config", False):
        try:
            stored, _exists = WorkspaceConfigStore(reference_workspace).load()
            shortcuts = stored.tui_shortcuts
        except ConfigError:
            # The worker reports the same safe configuration error before accepting input.
            pass
    ui = TerminalUI(worker, reference_workspace=reference_workspace, shortcuts=shortcuts)
    try:
        return ui.run()
    except (EOFError, NotImplementedError, OSError):
        if ui._worker_started:
            return 2
        return None


def terminal_supports_tui(*, is_windows: bool, is_stdin_tty: bool, is_stdout_tty: bool, term: str | None) -> bool:
    if not is_stdin_tty or not is_stdout_tty:
        return False
    if is_windows:
        return True
    return bool(term and term.casefold() != "dumb")
