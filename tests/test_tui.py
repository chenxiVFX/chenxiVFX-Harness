from __future__ import annotations

import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if os.name == "posix":
    import fcntl
    import pty
    import struct
    import termios

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
from wcwidth import wcswidth

from cvfx_harness.cli import _StreamingSecretRedactor, _confirm_action, build_parser, run_console
from cvfx_harness.providers import AssistantTurn, MockProvider, ProviderCancelled, ToolCall
from cvfx_harness.tools import ActionRequest, Workspace
from cvfx_harness.tui import CommandCompleter, TerminalUI, terminal_supports_tui


class PromptToolkitTests(unittest.TestCase):
    def _run_ui(self, worker, send_keys, *, output=None) -> tuple[TerminalUI, int]:
        with create_pipe_input() as pipe:
            ui = TerminalUI(
                worker,
                input=pipe,
                output=DummyOutput() if output is None else output,
            )
            errors: list[BaseException] = []

            def sender() -> None:
                try:
                    if not ui._request_ready.wait(5):
                        raise AssertionError("worker did not request input")
                    send_keys(pipe, ui)
                except BaseException as exc:
                    errors.append(exc)
                    ui.cancellation_event.set()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not ui._worker_done:
                        if ui._pending is not None:
                            ui._resolve_pending(eof=True)
                            break
                        time.sleep(0.01)

            thread = threading.Thread(target=sender, daemon=True)
            thread.start()
            result = ui.run()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "synthetic keyboard driver must finish")
            if errors:
                raise errors[0]
            return ui, result

    def _run_cli_ui(self, workspace: str, provider, send_keys) -> tuple[TerminalUI, int]:
        args = build_parser().parse_args(
            ["--workspace", workspace, "--provider", "mock"]
        )

        def worker(ui: TerminalUI) -> int:
            return run_console(
                args,
                input_fn=ui.input,
                output=ui.output,
                stream_write=ui.stream_write,
                cancellation_event=ui.cancellation_event,
            )

        with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
            return self._run_ui(worker, send_keys)

    @staticmethod
    def _wait_until(predicate, message: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(message)

    def test_fixed_command_completer_only_offers_commands_and_enums(self) -> None:
        from prompt_toolkit.document import Document
        from prompt_toolkit.completion import CompleteEvent

        completer = CommandCompleter()
        command_items = list(completer.get_completions(Document("/帮"), CompleteEvent()))
        values = [item.text for item in completer.get_completions(Document("/模式 计"), CompleteEvent())]
        path_values = list(completer.get_completions(Document("@资料/"), CompleteEvent()))
        self.assertEqual([item.text for item in command_items], ["/帮助"])
        self.assertEqual(command_items[0].display_meta_text, "显示完整说明")
        self.assertEqual(values, ["计划"])
        self.assertEqual(path_values, [])

    def test_tab_fills_filtered_command_and_enter_submits(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            ui.output("中文界面已启动")
            received.append(ui.input("你> "))
            ui.output("已收到合成命令。")
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pipe.send_text("/帮")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = ui._input_buffer.complete_state
                if state is not None and state.completions:
                    break
                time.sleep(0.01)
            else:
                self.fail("live command completion did not appear")
            pipe.send_text("\t\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["/帮助"])
        self.assertIn("中文界面已启动", ui._transcript)
        self.assertIn("你> /帮助", ui._transcript)
        self.assertEqual(ui._status, "会话已结束")

    def test_enter_fills_command_suggestion_before_a_second_enter_submits(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你> "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pipe.send_text("/帮")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = ui._input_buffer.complete_state
                if state is not None and state.completions:
                    break
                time.sleep(0.01)
            else:
                self.fail("command suggestion did not appear before Enter")
            pipe.send_bytes(b"\r")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if ui._input_buffer.text == "/帮助":
                    break
                time.sleep(0.01)
            else:
                self.fail("first Enter did not fill the command suggestion")
            self.assertIsNotNone(ui._pending, "filling a candidate must not submit")
            pipe.send_bytes(b"\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["/帮助"])

    def test_enter_submits_exact_command_without_replacing_it_with_first_candidate(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你> "))
            return 0

        ui, result = self._run_ui(
            worker,
            lambda pipe, _ui: pipe.send_text("/命令\r"),
        )
        self.assertEqual(result, 0)
        self.assertEqual(received, ["/命令"])

    def test_arrow_keys_select_second_command_before_tab_fills(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你> "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pipe.send_text("/")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = ui._input_buffer.complete_state
                if state is not None and len(state.completions) >= 2:
                    break
                time.sleep(0.01)
            else:
                self.fail("command list did not appear")
            pipe.send_bytes(b"\x1b[B\x1b[B")
            pipe.send_text("\t\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["/命令"])
        self.assertIn("你> /命令", ui._transcript)

    def test_ctrl_j_inserts_multiline_text_and_enter_submits(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你> "))
            return 0

        ui, result = self._run_ui(
            worker,
            lambda pipe, _ui: pipe.send_text("第一行\n第二行\r"),
        )
        self.assertEqual(result, 0)
        self.assertEqual(received, ["第一行\n第二行"])
        self.assertIn("第一行\n第二行", ui._transcript)

    def test_arrow_keys_move_cursor_when_no_completion_menu_is_open(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你> "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pipe.send_text("第一行\n第二行")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if ui._input_buffer.text == "第一行\n第二行":
                    break
                time.sleep(0.01)
            else:
                self.fail("multiline draft was not inserted before arrow navigation")
            self.assertEqual(ui._input_buffer.document.cursor_position_row, 1)
            pipe.send_bytes(b"\x1b[A")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if ui._input_buffer.document.cursor_position_row == 0:
                    break
                time.sleep(0.01)
            self.assertEqual(ui._input_buffer.document.cursor_position_row, 0)
            pipe.send_bytes(b"\x1b[B")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if ui._input_buffer.document.cursor_position_row == 1:
                    break
                time.sleep(0.01)
            self.assertEqual(ui._input_buffer.document.cursor_position_row, 1)
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["第一行\n第二行"])

    def test_ctrl_c_clears_draft_before_interrupting_input(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你> "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pipe.send_text("不要提交")
            time.sleep(0.05)
            pipe.send_bytes(b"\x03")
            time.sleep(0.05)
            pipe.send_text("保留的输入\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["保留的输入"])
        self.assertNotIn("已清空草稿", ui._notice)

    def test_approval_paste_cannot_override_default_deny(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("确认写入文件？[y/N] "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pasted = "是\n不要批准".encode("utf-8")
            pipe.send_bytes(b"\x1b[200~" + pasted + b"\x1b[201~")
            time.sleep(0.05)
            self.assertIsNotNone(ui._pending)
            self.assertTrue(ui._pending.approval)
            self.assertEqual(ui._input_buffer.text, "")
            self.assertTrue(ui.input_area.buffer.read_only())
            self.assertNotIn("[y/N]", ui._prompt)
            self.assertFalse(ui._approval_choice_yes)
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, [""])
        self.assertIn("审批选择：否（默认拒绝）", ui._transcript)

    def test_approval_yes_is_only_selected_until_focused_enter(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("确认写入文件？[y/N] "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            self.assertTrue(ui._approval_choice_yes is False)
            self.assertIn("确认写入文件？", ui._prompt)
            self.assertNotIn("[y/N]", ui._prompt)
            self.assertTrue(ui._application.layout.has_focus(ui._approval_no_button))
            self.assertIn("当前", ui._approval_no_button.text)
            self.assertEqual(ui._approval_yes_button.text, "批准本次")
            pipe.send_text("\t")
            time.sleep(0.05)
            self.assertTrue(ui._approval_choice_yes)
            self.assertTrue(ui._application.layout.has_focus(ui._approval_yes_button))
            self.assertIn("当前", ui._approval_yes_button.text)
            self.assertIsNotNone(ui._pending, "selection alone must not approve")
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["是"])
        self.assertIn("当前选择：是", ui._notice)

    def test_enter_after_focus_moves_to_transcript_does_not_confirm_approval(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("确认写入文件？[y/N] "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            self.assertIsNotNone(ui._pending)
            self.assertTrue(ui._pending.approval)
            ui._application.layout.focus(ui.output_area)
            self.assertTrue(ui._application.layout.has_focus(ui.output_area))
            pipe.send_text("\r")
            time.sleep(0.05)
            self.assertIsNotNone(ui._pending, "Enter in the transcript must not confirm")
            self.assertFalse(ui._approval_choice_yes)
            ui._approval_yes_button.handler()
            self.assertIsNotNone(ui._pending, "mouse selection alone must not confirm")
            ui._application.layout.focus(ui.output_area)
            pipe.send_text("\r")
            time.sleep(0.05)
            self.assertIsNotNone(
                ui._pending, "Enter in transcript must not confirm a selection"
            )
            ui._application.layout.focus(ui._approval_yes_button)
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["是"])

    def test_rapid_repeated_enter_cannot_reapprove_next_default_deny_request(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("确认第一次操作？[y/N] "))
            received.append(ui.input("确认第二次操作？[y/N] "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            self.assertIsNotNone(ui._pending)
            self.assertTrue(ui._pending.approval)
            first_request = ui._pending
            pipe.send_text("\t")
            self._wait_until(
                lambda: ui._approval_choice_yes,
                "Tab did not select the one-time approval option",
            )
            pipe.send_bytes(b"\r\r")
            self._wait_until(
                lambda: ui._pending is not None
                and ui._pending is not first_request
                and ui._pending.approval,
                "second approval did not become pending",
            )
            self.assertFalse(ui._approval_choice_yes, "each approval must reset to default deny")
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["是", ""])
        self.assertEqual(ui._transcript.count("审批选择：是（本次）"), 1)
        self.assertEqual(ui._transcript.count("审批选择：否（默认拒绝）"), 1)

    def test_mouse_approval_handler_only_selects_and_never_submits(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("确认写入文件？[y/N] "))
            return 0

        def send(_pipe, ui: TerminalUI) -> None:
            ui._approval_yes_button.handler()
            self.assertTrue(ui._approval_choice_yes)
            self.assertIsNotNone(ui._pending, "mouse selection alone must not approve")
            ui._approval_no_button.handler()
            self.assertFalse(ui._approval_choice_yes)
            self.assertIsNotNone(ui._pending, "mouse selection alone must not reject")
            ui._submit_input()

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, [""])

    def test_mouse_scroll_focus_and_current_candidate_style_are_configured(self) -> None:
        from prompt_toolkit.layout.margins import ScrollbarMargin

        with create_pipe_input() as pipe:
            ui = TerminalUI(lambda _ui: 0, input=pipe, output=DummyOutput())
            self.assertTrue(ui._application.mouse_support())
            self.assertTrue(ui.input_area.control.focus_on_click())
            self.assertTrue(ui.output_area.control.focus_on_click())
            scrollbar_margins = [
                margin
                for margin in ui.output_area.window.right_margins
                if isinstance(margin, ScrollbarMargin)
            ]
            self.assertEqual(len(scrollbar_margins), 1)
            self.assertFalse(scrollbar_margins[0].display_arrows())
            current_style = ui._application.style.get_attrs_for_style_str(
                "class:completion-menu.completion.current"
            )
            self.assertEqual(current_style.bgcolor, "6bbdb4")
            self.assertEqual(current_style.color, "0b0d10")
            self.assertTrue(current_style.bold)
            self.assertTrue(current_style.underline)
            current_meta_style = ui._application.style.get_attrs_for_style_str(
                "class:completion-menu.meta.completion.current"
            )
            self.assertEqual(current_meta_style.bgcolor, "6bbdb4")
            self.assertEqual(current_meta_style.color, "0b0d10")
            self.assertTrue(current_meta_style.bold)

    def test_status_line_is_compact_contextual_and_fits_terminal_width(self) -> None:
        from cvfx_harness.tui import _InputRequest

        class SizedDummyOutput(DummyOutput):
            def __init__(self, columns: int) -> None:
                self.columns = columns

            def get_size(self):
                return Size(rows=24, columns=self.columns)

        for columns in (20, 24, 36, 64, 80, 100):
            with self.subTest(columns=columns), create_pipe_input() as pipe:
                ui = TerminalUI(
                    lambda _ui: 0,
                    input=pipe,
                    output=SizedDummyOutput(columns),
                )
                ui._pending = _InputRequest("输入", False, threading.Event())
                line = ui._status_line()
                self.assertLessEqual(wcswidth(line), columns)
                self.assertIn("等待输入" if columns >= 26 else "输入", line)
                self.assertLessEqual(wcswidth(ui._empty_hint()), columns)
                self.assertIn(
                    "查看命令" if columns >= 42 else "/命令", ui._empty_hint()
                )
                self.assertNotIn("Ctrl+C", line)
                self.assertNotIn("Tab填入", line)
                ui._notice = "这是一个故意很长的合成状态提示，用来检查窄终端不会裁掉状态栏布局。"
                self.assertLessEqual(wcswidth(ui._status_line()), columns)
                ui._notice = ""
                ui._pending = _InputRequest("确认写入？[y/N]", True, threading.Event())
                line = ui._status_line()
                self.assertLessEqual(wcswidth(line), columns)
                self.assertIn("Enter", line)
                if columns >= 58:
                    self.assertIn("Esc 取消", line)
                elif columns < 36:
                    self.assertNotIn("Tab/↑↓", line)

    def test_empty_state_has_a_centered_invitation_without_persistent_shortcuts(self) -> None:
        class SizedDummyOutput(DummyOutput):
            def get_size(self):
                return Size(rows=24, columns=80)

        def worker(ui: TerminalUI) -> int:
            ui.output("离线模式，启动完成。")
            ui.input("你 [执行]> ")
            return 0

        def send(_pipe, ui: TerminalUI) -> None:
            self._wait_until(
                lambda: ui._application.renderer.last_rendered_screen is not None,
                "empty state did not render",
            )
            time.sleep(0.03)
            screen = ui._application.renderer.last_rendered_screen
            self.assertIsNotNone(screen)
            positions = screen.visible_windows_to_write_positions
            title = positions.get(ui._empty_title_window)
            hint = positions.get(ui._empty_hint_window)
            self.assertIsNotNone(title)
            self.assertIsNotNone(hint)
            transcript = positions.get(ui.output_area.window)
            self.assertIsNotNone(transcript)
            self.assertIn("离线模式，启动完成。", ui._transcript)
            self.assertGreater(title.ypos, 1)
            self.assertLess(title.ypos, 20)
            self.assertGreater(title.ypos, transcript.ypos)
            self.assertEqual(hint.ypos, title.ypos + 1)
            self.assertNotIn("Ctrl+J", ui._status_line())
            ui._resolve_pending(value="")

        ui, result = self._run_ui(worker, send, output=SizedDummyOutput())
        self.assertEqual(result, 0)

    def test_completion_menu_is_clipped_above_prompt_at_varied_terminal_sizes(self) -> None:
        class SizedDummyOutput(DummyOutput):
            def __init__(self, rows: int, columns: int) -> None:
                self.rows = rows
                self.columns = columns

            def get_size(self):
                return Size(rows=self.rows, columns=self.columns)

        for rows, columns in ((9, 20), (10, 24), (12, 36), (16, 64), (24, 80), (30, 100)):
            with self.subTest(rows=rows, columns=columns):
                def send(pipe, ui: TerminalUI) -> None:
                    self._wait_until(
                        lambda: ui._pending is not None,
                        "input prompt did not become ready",
                    )
                    pipe.send_text("/")
                    self._wait_until(
                        lambda: ui._input_buffer.complete_state is not None
                        and ui._application.renderer.last_rendered_screen is not None,
                        "completion menu did not render",
                    )
                    time.sleep(0.03)
                    screen = ui._application.renderer.last_rendered_screen
                    self.assertIsNotNone(screen)
                    positions = screen.visible_windows_to_write_positions
                    menu = positions.get(ui._completion_window)
                    prompt = positions.get(ui._prompt_window)
                    top_separator = positions.get(ui._input_top_separator)
                    status = positions.get(ui._status_window)
                    self.assertIsNotNone(menu, "completion menu must remain visible")
                    self.assertIsNotNone(prompt)
                    self.assertIsNotNone(top_separator)
                    self.assertIsNotNone(status)
                    self.assertGreater(menu.height, 0)
                    self.assertLessEqual(menu.ypos + menu.height, top_separator.ypos)
                    self.assertLessEqual(menu.ypos + menu.height, prompt.ypos)
                    self.assertEqual(status.ypos, rows - 1)
                    menu_text = "\n".join(
                        "".join(screen.data_buffer[y][x].char for x in range(columns))
                        for y in range(menu.ypos, menu.ypos + menu.height)
                    )
                    prompt_text = "".join(
                        screen.data_buffer[prompt.ypos][x].char
                        for x in range(columns)
                    )
                    self.assertIn("/帮助", menu_text)
                    self.assertIn("你 [执行]>", prompt_text)
                    ui._resolve_pending(value="")

                ui, result = self._run_ui(
                    lambda current: (current.input("你 [执行]> "), 0)[1],
                    send,
                    output=SizedDummyOutput(rows, columns),
                )
                self.assertEqual(result, 0)

    def test_cli_streaming_cancel_restores_input_and_keeps_busy_draft_separate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-agent-stream-") as workspace:
            stream_started = threading.Event()

            class ResumableMockProvider(MockProvider):
                def __init__(self) -> None:
                    super().__init__()
                    self.calls = 0

                def complete_streaming(
                    self,
                    messages,
                    tools,
                    *,
                    cancellation_event=None,
                    max_tokens=2048,
                    on_text_delta,
                ):
                    del messages, tools, max_tokens
                    self.calls += 1
                    if self.calls == 1:
                        on_text_delta("这是足够长的首段合成流式输出，用于检查输入区域保持独立。")
                        stream_started.set()
                        if cancellation_event is None or not cancellation_event.wait(5):
                            raise AssertionError("cancel input did not reach the mock provider")
                        raise ProviderCancelled("synthetic fixture cancelled")
                    on_text_delta("恢复后继续生成的合成流式答复。")
                    return AssistantTurn("恢复后继续生成的合成流式答复。")

            provider = ResumableMockProvider()

            def send(pipe, ui: TerminalUI) -> None:
                self.assertIsNotNone(ui._pending)
                self.assertFalse(ui.input_area.buffer.read_only())
                pipe.send_text("开始合成对话")
                self._wait_until(
                    lambda: ui._input_buffer.text == "开始合成对话",
                    "prompt text was not accepted while the input request was pending",
                )
                pipe.send_bytes(b"\r")
                self.assertTrue(stream_started.wait(5))
                self._wait_until(
                    lambda: "首段合成流式" in ui._transcript,
                    "stream delta was not delivered to the transcript",
                )
                self.assertTrue(ui.input_area.buffer.read_only())
                pipe.send_text("不应静默丢失的输入草稿")
                time.sleep(0.05)
                self.assertEqual(ui._input_buffer.text, "")
                self.assertNotIn("不应静默丢失的输入草稿", ui._transcript)
                pipe.send_bytes(b"\x03")
                self._wait_until(
                    lambda: ui._pending is not None and not ui._pending.approval,
                    "TUI did not return to an input prompt after cancellation",
                )
                self.assertIn("已取消当前模型请求", ui._transcript)
                self.assertFalse(ui.input_area.buffer.read_only())
                pipe.send_text("继续合成对话")
                self._wait_until(
                    lambda: ui._input_buffer.text == "继续合成对话",
                    "input was not restored after cancellation",
                )
                pipe.send_bytes(b"\r")
                self._wait_until(
                    lambda: "恢复后继续生成的合成流式答复。" in ui._transcript
                    and ui._pending is not None
                    and not ui._pending.approval,
                    "conversation did not resume after cancellation",
                )
                self.assertFalse(ui.input_area.buffer.read_only())
                pipe.send_text("/退出")
                self._wait_until(
                    lambda: ui._input_buffer.text == "/退出",
                    "exit command text was not accepted after resumed stream",
                )
                pipe.send_bytes(b"\r")
                self._wait_until(
                    lambda: ui._worker_done,
                    "TUI input did not submit /退出 after resumed stream",
                )

            ui, result = self._run_cli_ui(workspace, provider, send)

        self.assertEqual(result, 0)
        self.assertEqual(provider.calls, 2)
        self.assertIn("已取消当前模型请求", ui._transcript)
        self.assertIn("恢复后继续生成的合成流式答复。", ui._transcript)

    def test_cli_write_tool_default_denial_and_one_explicit_approval(self) -> None:
        from cvfx_harness.tui import _InputRequest

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-agent-approval-") as workspace:
            target = Path(workspace) / "合成文件.txt"

            class WriteMockProvider(MockProvider):
                def __init__(self) -> None:
                    super().__init__()
                    self.calls = 0

                def complete_streaming(
                    self,
                    messages,
                    tools,
                    *,
                    cancellation_event=None,
                    max_tokens=2048,
                    on_text_delta,
                ):
                    del messages, tools, max_tokens
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise ProviderCancelled("unexpected synthetic cancellation")
                    self.calls += 1
                    if self.calls in {1, 3}:
                        return AssistantTurn(
                            "",
                            [
                                ToolCall(
                                    f"write-{self.calls}",
                                    "write_file",
                                    {"path": "合成文件.txt", "content": "获批后只写入一次"},
                                )
                            ],
                        )
                    answer = (
                        "拒绝后仍可继续的合成答复。"
                        if self.calls == 2
                        else "获批写入后的合成答复。"
                    )
                    on_text_delta(answer)
                    return AssistantTurn(answer)

            provider = WriteMockProvider()
            committed_paths: list[str] = []
            original_commit = Workspace.commit_write

            def count_commit(workspace_obj, preview, *, cancellation_event=None):
                committed_paths.append(preview.canonical_path)
                return original_commit(
                    workspace_obj,
                    preview,
                    cancellation_event=cancellation_event,
                )

            def send(pipe, ui: TerminalUI) -> None:
                def submit_prompt(text: str) -> None:
                    pipe.send_text(text)
                    self._wait_until(
                        lambda: ui._input_buffer.text == text,
                        f"input text was not accepted: {text}",
                    )
                    pipe.send_bytes(b"\r")

                def wait_for_approval() -> None:
                    self._wait_until(
                        lambda: ui._pending is not None and ui._pending.approval,
                        "Agent did not present its write approval in the TUI",
                    )
                    self.assertTrue(ui.input_area.buffer.read_only())
                    self.assertNotIn("[y/N]", ui._prompt)

                submit_prompt("请求第一次合成写入")
                wait_for_approval()
                self.assertFalse(ui._approval_choice_yes)
                pasted = "是\n不要批准".encode("utf-8")
                pipe.send_bytes(b"\x1b[200~" + pasted + b"\x1b[201~")
                time.sleep(0.05)
                self.assertIsNotNone(ui._pending, "pasted text must not submit approval")
                self.assertFalse(ui._approval_choice_yes)
                self.assertEqual(ui._input_buffer.text, "")
                self.assertEqual(committed_paths, [])
                pipe.send_bytes(b"\r")
                self._wait_until(
                    lambda: ui._pending is not None and not ui._pending.approval,
                    "denied write did not return to the conversation prompt",
                )
                self.assertFalse(target.exists())
                self.assertEqual(committed_paths, [])

                submit_prompt("请求第二次合成写入")
                wait_for_approval()
                self.assertFalse(ui._approval_choice_yes)
                ui._application.layout.focus(ui.output_area)
                pipe.send_bytes(b"\r")
                time.sleep(0.05)
                self.assertIsNotNone(
                    ui._pending, "Enter outside approval controls must not approve"
                )
                self.assertEqual(committed_paths, [])
                ui._approval_yes_button.handler()
                self.assertTrue(ui._approval_choice_yes)
                self.assertIsNotNone(ui._pending, "selection alone must not approve")
                ui._application.layout.focus(ui.output_area)
                pipe.send_bytes(b"\r")
                time.sleep(0.05)
                self.assertIsNotNone(ui._pending, "Enter outside approval controls must not confirm")
                ui._application.layout.focus(ui._approval_yes_button)
                pipe.send_bytes(b"\r\r")
                self._wait_until(
                    lambda: ui._pending is not None
                    and not ui._pending.approval
                    and target.exists(),
                    "approved write did not finish and return to input",
                )
                self.assertEqual(target.read_text(encoding="utf-8"), "获批后只写入一次")
                self.assertEqual(committed_paths, ["合成文件.txt"])
                submit_prompt("/退出")

            with patch.object(Workspace, "commit_write", new=count_commit):
                ui, result = self._run_cli_ui(workspace, provider, send)

        self.assertEqual(result, 0)
        self.assertEqual(provider.calls, 4)
        self.assertEqual(committed_paths, ["合成文件.txt"])
        self.assertIn("审批选择：否（默认拒绝）", ui._transcript)
        self.assertIn("审批选择：是（本次）", ui._transcript)
        self.assertIn("拒绝后仍可继续的合成答复。", ui._transcript)
        self.assertIn("获批写入后的合成答复。", ui._transcript)

    def test_escape_closes_completion_without_submitting_or_cancelling(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你> "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pipe.send_text("/帮")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = ui._input_buffer.complete_state
                if state is not None and state.completions:
                    break
                time.sleep(0.01)
            else:
                self.fail("completion menu did not appear before Esc")
            pipe.send_bytes(b"\x1b")
            time.sleep(0.05)
            self.assertIsNotNone(ui._pending, "Esc closes the menu first")
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["/帮"])
        self.assertIn("建议已关闭", ui._notice)

    def test_ctrl_c_cancels_a_running_worker(self) -> None:
        worker_started = threading.Event()
        cancelled: list[bool] = []
        ui_holder: dict[str, TerminalUI] = {}

        def worker(ui: TerminalUI) -> int:
            ui_holder["ui"] = ui
            worker_started.set()
            cancelled.append(ui.cancellation_event.wait(3))
            return 0

        with create_pipe_input() as pipe:
            ui = TerminalUI(worker, input=pipe, output=DummyOutput())

            def sender() -> None:
                self.assertTrue(worker_started.wait(5))
                pipe.send_bytes(b"\x03")

            thread = threading.Thread(target=sender, daemon=True)
            thread.start()
            result = ui.run()
            thread.join(timeout=5)
        self.assertEqual(result, 0)
        self.assertEqual(cancelled, [True])
        self.assertEqual(ui._status, "会话已结束")

    def test_stream_redactor_releases_chinese_before_stream_finish(self) -> None:
        redactor = _StreamingSecretRedactor()
        visible = redactor.feed("这是超过缓冲窗口的中文合成流式文本，应该在回答结束前出现")
        self.assertTrue(visible)
        self.assertTrue(visible.startswith("这是"))

    def test_tui_terminal_output_masks_controls_and_likely_credentials(self) -> None:
        def worker(ui: TerminalUI) -> int:
            ui.output("Bearer SYNTHETIC_TUI_SECRET_1234567890 \x1b[2J")
            ui.input("继续> ")
            return 0

        ui, result = self._run_ui(worker, lambda pipe, _ui: pipe.send_text("好\r"))
        self.assertEqual(result, 0)
        self.assertNotIn("SYNTHETIC_TUI_SECRET_1234567890", ui._transcript)
        self.assertNotIn("\x1b", ui._transcript)
        self.assertIn("\\x1b[2J", ui._transcript)

    def test_stream_redactor_keeps_split_sensitive_values_until_complete(self) -> None:
        redactor = _StreamingSecretRedactor()
        visible = redactor.feed("正常中文内容够长以便流动显示。api_key=合成敏感值")
        self.assertNotIn("合成敏感值", visible)
        tail = redactor.feed("后续敏感文本仍属于字段值，结束。")
        final = redactor.finish()
        self.assertNotIn("合成敏感值", visible + tail + final)
        self.assertIn("[已脱敏]", visible + tail + final)

    def test_terminal_support_falls_back_for_non_tty_and_dumb_terminal(self) -> None:
        self.assertFalse(
            terminal_supports_tui(
                is_windows=False, is_stdin_tty=False, is_stdout_tty=True, term="xterm"
            )
        )
        self.assertFalse(
            terminal_supports_tui(
                is_windows=False, is_stdin_tty=True, is_stdout_tty=True, term="dumb"
            )
        )
        self.assertTrue(
            terminal_supports_tui(
                is_windows=True, is_stdin_tty=True, is_stdout_tty=True, term=None
            )
        )

    def test_tui_cancellation_and_plain_mode_are_explicit(self) -> None:
        self.assertTrue(hasattr(build_parser().parse_args(["--plain"]), "plain"))

    @unittest.skipUnless(os.name == "posix", "POSIX pseudo-terminal test")
    def test_real_posix_pty_starts_fullscreen_tui_and_exits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-pty-") as workspace:
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "TERM": "xterm-256color",
            }
            master_fd, slave_fd = pty.openpty()
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            process = subprocess.Popen(
                [sys.executable, "-m", "cvfx_harness", "--workspace", workspace, "--provider", "mock"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=environment,
                close_fds=True,
            )
            os.close(slave_fd)
            output = bytearray()
            sent_exit = False
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master_fd], [], [], 0.1)
                    if ready:
                        try:
                            output.extend(os.read(master_fd, 8192))
                        except OSError:
                            break
                    if not sent_exit and "你[".encode("utf-8") in output:
                        os.write(master_fd, "/退出\r".encode("utf-8"))
                        sent_exit = True
                    if process.poll() is not None:
                        break
                self.assertTrue(sent_exit, "full-screen TUI did not present its interactive input area")
                self.assertEqual(process.wait(timeout=2), 0)
                self.assertIn("会话".encode("utf-8"), output)
                self.assertIn("Enter".encode("utf-8"), output)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)
                os.close(master_fd)

    @unittest.skipUnless(os.name == "posix", "PTY key handling is POSIX-specific")
    def test_real_posix_pty_shows_completion_menu_and_accepts_selected_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-menu-pty-") as workspace:
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "TERM": "xterm-256color",
            }
            master_fd, slave_fd = pty.openpty()
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            process = subprocess.Popen(
                [sys.executable, "-m", "cvfx_harness", "--workspace", workspace, "--provider", "mock"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=environment,
                close_fds=True,
            )
            os.close(slave_fd)
            transcript = bytearray()

            def read_until(marker: bytes, *, start: int = 0, timeout: float = 8.0) -> None:
                deadline = time.monotonic() + timeout
                while marker not in transcript[start:]:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.fail(f"PTY timed out waiting for {marker!r}")
                    ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.1))
                    if not ready:
                        continue
                    try:
                        transcript.extend(os.read(master_fd, 8192))
                    except OSError:
                        break

            try:
                read_until("你[执行]> ".encode("utf-8"))
                menu_start = len(transcript)
                os.write(master_fd, b"/")
                read_until("/帮助".encode("utf-8"), start=menu_start)
                read_until("显示完整说明".encode("utf-8"), start=menu_start)
                os.write(master_fd, b"\x1b[B\x1b[B\t\r")
                read_until("命令：".encode("utf-8"), start=menu_start)
                self.assertIn("/命令".encode("utf-8"), transcript[menu_start:])
                os.write(master_fd, "/退出\r".encode("utf-8"))
                self.assertEqual(process.wait(timeout=8), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)
                os.close(master_fd)


class ApprovalSafetyTests(unittest.TestCase):
    def test_multiline_affirmative_answer_is_rejected(self) -> None:
        outputs: list[str] = []
        request = ActionRequest(operation="write", path="notes.txt", diff="+合成内容")
        approved = _confirm_action(
            request,
            input_fn=lambda _prompt: "是\n",
            output=outputs.append,
        )
        self.assertFalse(approved)
        self.assertTrue(any("单行" in output for output in outputs))


if __name__ == "__main__":
    unittest.main()
