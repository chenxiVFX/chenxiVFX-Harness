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

if os.name == "posix":
    import fcntl
    import pty
    import struct
    import termios

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cvfx_harness.cli import _StreamingSecretRedactor, _confirm_action, build_parser
from cvfx_harness.tools import ActionRequest
from cvfx_harness.tui import CommandCompleter, TerminalUI, terminal_supports_tui


class PromptToolkitTests(unittest.TestCase):
    def _run_ui(self, worker, send_keys) -> tuple[TerminalUI, int]:
        with create_pipe_input() as pipe:
            ui = TerminalUI(worker, input=pipe, output=DummyOutput())
            errors: list[BaseException] = []

            def sender() -> None:
                try:
                    if not ui._request_ready.wait(5):
                        raise AssertionError("worker did not request input")
                    send_keys(pipe, ui)
                except BaseException as exc:
                    errors.append(exc)
                    try:
                        pipe.send_bytes(b"\x03")
                    except Exception:
                        pass

            thread = threading.Thread(target=sender, daemon=True)
            thread.start()
            result = ui.run()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "synthetic keyboard driver must finish")
            if errors:
                raise errors[0]
            return ui, result

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
        self.assertIn("已清空草稿", ui._notice)

    def test_approval_input_rejects_pasted_newline_and_stays_default_deny(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("确认写入文件？[y/N] "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pasted = "是\n不要批准".encode("utf-8")
            pipe.send_bytes(b"\x1b[200~" + pasted + b"\x1b[201~\r")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if "单行" in ui._notice:
                    break
                time.sleep(0.01)
            else:
                self.fail("multiline approval paste was not rejected")
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, [""])
        self.assertIn("审批回答必须是单行", ui._notice)

    def test_approval_yes_is_only_selected_until_explicit_enter(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("确认写入文件？[y/N] "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            self.assertTrue(ui._approval_choice_yes is False)
            pipe.send_text("\t")
            time.sleep(0.05)
            self.assertTrue(ui._approval_choice_yes)
            self.assertIsNotNone(ui._pending, "selection alone must not approve")
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["是"])
        self.assertIn("当前选择：是", ui._notice)

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
            ui._input_buffer.text = ""
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
            self.assertTrue(
                any(
                    isinstance(margin, ScrollbarMargin)
                    for margin in ui.output_area.window.right_margins
                )
            )
            current_style = ui._application.style.get_attrs_for_style_str(
                "class:completion-menu.completion.current"
            )
            self.assertEqual(current_style.bgcolor, "c6a0f6")
            self.assertEqual(current_style.color, "1b1722")
            self.assertTrue(current_style.bold)

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
