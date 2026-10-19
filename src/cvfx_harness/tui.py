from __future__ import annotations

import asyncio
import os
import queue
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from prompt_toolkit import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
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
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, TextArea
from wcwidth import wcswidth

from .profiles import TestProfileError, TestProfileStore
from .sessions import SessionError, SessionStore, redact_likely_secrets
from .tools import Workspace, WorkspaceError

MAX_TRANSCRIPT_CHARS = 200_000
POLL_INTERVAL_SECONDS = 0.025
COMMANDS = (
    ("/帮助", "显示完整说明"), ("/命令", "列出中文命令"),
    ("/状态", "查看模型和工作区"), ("/模式", "切换计划或执行"),
    ("/目标", "设置会话目标"), ("/计划", "生成、审阅、修订或执行计划"),
    ("/保存", "保存安全会话"), ("/检查点", "保存手写摘要"),
    ("/恢复", "恢复安全会话"), ("/会话", "列出本地会话"),
    ("/多行", "撰写多行问题"), ("/压缩", "预览并压缩上下文"),
    ("/统计", "查看上下文用量"), ("/预算", "设置本轮预算"),
    ("/配置", "查看非敏感配置"), ("/测试", "管理固定 Python 测试配置"),
    ("/子代理", "管理隔离子任务"),
    ("/撤销", "预览并撤销写入"), ("/退出", "结束会话"),
)
ENUM_COMPLETIONS = {
    "/模式": ("计划", "执行"),
    "/测试": ("列表", "添加", "运行", "移除"),
    "/计划": ("生成", "显示", "修订", "接受", "拒绝", "添加", "执行", "完成", "阻塞", "取消", "重试", "清空"),
}
_APPROVAL_PROMPT = re.compile(r"\[y/N\]\s*$", re.IGNORECASE)


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
        for command, values in ENUM_COMPLETIONS.items():
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


class TerminalUI:
    """Full-screen prompt_toolkit shell around the existing synchronous CLI loop."""

    def __init__(
        self,
        worker: Callable[[TerminalUI], int],
        *,
        input: Any = None,
        output: Any = None,
        reference_workspace: Workspace | None = None,
    ) -> None:
        self._worker_callable = worker
        self._input = input
        self._output = output
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._pending: _InputRequest | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_started = False
        self._worker_done = False
        self._worker_result = 2
        self._cancel_event = threading.Event()
        self._cancel_requested = False
        self._request_ready = threading.Event()
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
        self.input_area = TextArea(
            multiline=True,
            completer=CommandCompleter(reference_workspace),
            complete_while_typing=True,
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
            Condition(lambda: self._pending is None or not self._pending.approval),
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
        self._approval_no_button = Button(
            "拒绝（当前）", handler=self._select_no, width=18
        )
        self._approval_yes_button = Button(
            "批准本次", handler=self._select_yes, width=18
        )
        self._approval_container = ConditionalContainer(
            HSplit(
                [
                    Window(
                        FormattedTextControl("待审批 · 默认拒绝"),
                        height=1,
                        style="class:approval.heading",
                    ),
                    self._approval_area,
                    VSplit(
                        [self._approval_no_button, self._approval_yes_button],
                        padding=1,
                    ),
                ]
            ),
            Condition(lambda: bool(self._approval_text)),
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
                input_block,
                self._status_window,
            ]
        )
        self._bindings = self._build_key_bindings()
        self._application = Application(
            layout=Layout(content, focused_element=self.input_area),
            key_bindings=self._bindings,
            style=Style.from_dict(
                {
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
                    "empty.background": "bg:#0b0d10",
                    "empty.title": "fg:#6bbdb4 bg:#0b0d10 bold",
                    "empty.hint": "fg:#8b96a1 bg:#0b0d10",
                    "approval": "fg:#d8dee6",
                    "approval.heading": "fg:#6bbdb4 bold",
                    "button": "fg:#adb7c1 bg:#0b0d10",
                    "button.focused": "fg:#0b0d10 bg:#6bbdb4 bold underline",
                    "completion-menu": "bg:#0b0d10",
                    "completion-menu.completion": "fg:#d4dbe3 bg:#0b0d10",
                    "completion-menu.completion.current": (
                        "fg:#0b0d10 bg:#6bbdb4 bold underline"
                    ),
                    "completion-menu.meta.completion": "fg:#8c98a4 bg:#0b0d10",
                    "completion-menu.meta.completion.current": (
                        "fg:#0b0d10 bg:#6bbdb4 bold underline"
                    ),
                }
            ),
            full_screen=True,
            erase_when_done=False,
            mouse_support=True,
            input=input,
            output=output,
            refresh_interval=0.1,
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
                        if any(marker in context for marker in ("差异", "审批范围")):
                            self._approval_text = _safe_text(context)
                        else:
                            self._approval_text = self._prompt
                        self._approval_area.text = self._approval_text
                    else:
                        self._application.layout.focus(self.input_area)
                        self._cancel_event.clear()
                        self._cancel_requested = False
                        self._approval_choice_yes = False
                        self._approval_text = ""
                        self._approval_area.text = ""
                        self._status = "等待输入"
                    self._application.layout.focus(
                        self._approval_no_button if value.approval else self.input_area
                    )
                    self._pending = value
                    self._request_ready.set()
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

        @bindings.add("up", eager=True)
        def previous_completion(event) -> None:
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
            if self._input_buffer.complete_state is None:
                self._move_output_cursor(-max(2, self.output_area.window.height.preferred or 10))

        @bindings.add("pagedown", eager=True)
        def scroll_output_down(event) -> None:
            if self._input_buffer.complete_state is None:
                self._move_output_cursor(max(2, self.output_area.window.height.preferred or 10))

        @bindings.add("c-end", eager=True)
        def output_bottom(event) -> None:
            if self._input_buffer.complete_state is None:
                self._follow_output = True
                self.output_area.buffer.cursor_position = len(self.output_area.text)

        return bindings

    def _on_input_text_changed(self, buffer) -> None:
        if buffer.text:
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
                self._append_transcript(f"\n你> {_safe_text(value)}\n")
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
        if info is not None:
            at_bottom = info.vertical_scroll >= max(
                0, info.content_height - info.window_height
            )
            if not at_bottom:
                self._follow_output = False
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
        fragments = [("class:header.brand", "chenxiVFX Harness")]
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
            fragments.append(("class:header.muted", f"  {metadata}"))
        if self._header_workspace:
            fragments.append(("class:header.workspace", f"  {self._header_workspace}"))
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
        self._approval_no_button.text = (
            "拒绝（默认）" if self._approval_choice_yes else "拒绝（当前）"
        )
        self._approval_yes_button.text = (
            "批准（当前）" if self._approval_choice_yes else "批准本次"
        )

    def _status_line(self) -> str:
        try:
            columns = self._application.output.get_size().columns
        except (AttributeError, NotImplementedError, OSError):
            columns = 80

        if self._notice:
            line = f"{self._status}  ·  {self._notice}"
        elif self._pending is None and not self._worker_done:
            line = "生成中  ·  Ctrl+C 取消"
        elif self._pending is None:
            line = self._status
        elif self._pending.approval:
            if columns >= 58:
                line = "待审批  ·  Tab/↑↓ 选择  ·  Enter 确认  ·  Esc 取消"
            elif columns >= 36:
                line = "待审批 · Tab/↑↓选择 · Enter确认"
            else:
                line = "审批 · Enter确认"
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
            input_fn=ui.input,
            output=ui.output,
            stream_write=ui.stream_write,
            cancellation_event=ui.cancellation_event,
        )

    try:
        reference_workspace = Workspace(args.workspace)
    except (WorkspaceError, OSError, RuntimeError):
        reference_workspace = None
    ui = TerminalUI(worker, reference_workspace=reference_workspace)
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
