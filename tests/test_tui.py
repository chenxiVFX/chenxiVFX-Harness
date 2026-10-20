from __future__ import annotations

import json
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
from cvfx_harness.plans import PlanStore
from cvfx_harness.profiles import TestProfileStore
from cvfx_harness.providers import AssistantTurn, MockProvider, ProviderCancelled, ToolCall
from cvfx_harness.questions import QuestionnaireRequest, QuestionnaireResult, UserQuestion, UserQuestionAnswer
from cvfx_harness.tools import ActionRequest, Workspace
from cvfx_harness.tui import (
    CommandCompleter,
    TerminalUI,
    run_tui_console,
    terminal_supports_tui,
)


class PromptToolkitTests(unittest.TestCase):
    def _run_ui(
        self,
        worker,
        send_keys,
        *,
        output=None,
        workspace_root: str | Path | None = None,
        shortcuts=None,
    ) -> tuple[TerminalUI, int]:
        with create_pipe_input() as pipe:
            reference_workspace = (
                Workspace(workspace_root) if workspace_root is not None else None
            )
            ui = TerminalUI(
                worker,
                input=pipe,
                output=DummyOutput() if output is None else output,
                reference_workspace=reference_workspace,
                shortcuts=shortcuts,
            )
            errors: list[BaseException] = []

            def sender() -> None:
                try:
                    self._wait_until(
                        lambda: ui._request_ready.is_set() or ui._question_ready.is_set(),
                        "worker did not request input or a questionnaire",
                    )
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
                ask_user_fn=ui.ask_questions,
            )

        with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
            return self._run_ui(worker, send_keys, workspace_root=workspace)

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
        alias_items = list(completer.get_completions(Document("/按"), CompleteEvent()))
        self.assertEqual([item.text for item in alias_items], ["/按键"])
        self.assertEqual(values, ["计划"])
        self.assertEqual(path_values, [])
        plan_values = [
            item.text
            for item in completer.get_completions(Document("/计划 接"), CompleteEvent())
        ]
        compression_values = [
            item.text
            for item in completer.get_completions(Document("/压缩 撤"), CompleteEvent())
        ]
        config_values = [
            item.text
            for item in completer.get_completions(Document("/配置 "), CompleteEvent())
        ]
        provider_values = [
            item.text
            for item in completer.get_completions(
                Document("/配置 提供商 deepseek-a"), CompleteEvent()
            )
        ]
        change_set_values = [
            item.text
            for item in completer.get_completions(Document("/变更集 "), CompleteEvent())
        ]
        self.assertEqual(plan_values, ["接受"])
        self.assertEqual(compression_values, ["撤销"])
        self.assertIn("编辑", config_values)
        self.assertEqual(provider_values, ["deepseek-anthropic"])
        self.assertEqual(change_set_values, ["列表", "证据", "恢复"])

    def test_tui_config_editor_reviews_then_persists_through_input_events(self) -> None:
        from cvfx_harness.config import WorkspaceConfigStore

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-config-edit-") as workspace:
            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "TUI did not request command input")
                pipe.send_text("/配置 编辑\r")
                self._wait_until(lambda: ui._prompt.startswith("提供商 ["), "config provider prompt missing")
                pipe.send_text("deepseek-anthropic\r")
                self._wait_until(lambda: ui._prompt.startswith("模型 ["), "config model prompt missing")
                pipe.send_text("deepseek-v4-pro\r")
                self._wait_until(lambda: ui._prompt.startswith("Base URL ["), "config endpoint prompt missing")
                pipe.send_text("\r")
                self._wait_until(lambda: ui._prompt.startswith("本地估算上下文阈值 ["), "config budget prompt missing")
                pipe.send_text("\r")
                self._wait_until(lambda: ui._prompt.startswith("输出 Token 上限 ["), "config output prompt missing")
                pipe.send_text("\r")
                self._wait_until(lambda: ui._prompt.startswith("TUI 快捷键 ["), "config shortcut prompt missing")
                pipe.send_text("F2=帮助，F4=状态\r")
                self._wait_until(lambda: ui._prompt.startswith("确认保存这些非敏感设置"), "config review prompt missing")
                self._wait_until(lambda: "待保存工作区设置（尚未写入）" in ui._transcript, "review summary missing")
                self.assertIn("TUI 快捷键：F2=帮助，F4=状态", ui._transcript)
                pipe.send_text("是\r")
                self._wait_until(lambda: "已保存到 .harness/config/settings.json" in ui._transcript, "config save confirmation missing")
                pipe.send_text("/退出\r")

            ui, rc = self._run_cli_ui(workspace, MockProvider(), drive)
            saved, exists = WorkspaceConfigStore(Workspace(workspace)).load()
            self.assertEqual(rc, 0)
            self.assertTrue(exists)
            self.assertEqual(saved.provider, "deepseek-anthropic")
            self.assertEqual(saved.model, "deepseek-v4-pro")
            self.assertIsNone(saved.base_url)
            self.assertEqual(saved.tui_shortcuts, {"F2": "help", "F4": "status"})
            self.assertIn("提供商：deepseek-anthropic", ui._transcript)
            self.assertIn("提供商 的工作区值已保存", ui._transcript)
            self.assertNotIn("provider 的工作区值已保存", ui._transcript)

    def test_custom_fkey_fills_read_only_local_command_without_submitting(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你 [执行]> "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            self._wait_until(lambda: ui._pending is not None, "TUI did not request input")
            pipe.send_text("现有草稿")
            pipe.send_bytes(b"\x1bOQ")  # xterm F2
            time.sleep(0.08)
            self.assertEqual(ui._input_buffer.text, "现有草稿")
            self.assertEqual(received, [])
            pipe.send_bytes(b"\x03")
            self._wait_until(lambda: not ui._input_buffer.text, "Ctrl+C did not clear the draft")
            pipe.send_bytes(b"\x1bOQ")
            self._wait_until(
                lambda: ui._input_buffer.text == "/帮助",
                "F2 did not fill the configured Chinese help command",
            )
            self.assertEqual(received, [], "a shortcut must never submit or execute by itself")
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send, shortcuts={"F2": "help"})
        self.assertEqual(result, 0)
        self.assertEqual(received, ["/帮助"])
        self.assertIn("按 Enter 提交，尚未执行", ui._notice)

    def test_custom_fkey_cannot_change_or_accept_an_approval(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("确认读取合成文件？ [y/N]"))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            self._wait_until(lambda: ui._pending is not None, "approval prompt did not appear")
            self.assertTrue(ui._pending.approval)
            pipe.send_bytes(b"\x1bOQ")
            time.sleep(0.08)
            self.assertTrue(ui._pending.approval)
            self.assertFalse(ui._approval_choice_yes)
            self.assertEqual(received, [])
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send, shortcuts={"F2": "help"})
        self.assertEqual(result, 0)
        self.assertEqual(received, [""])
        self.assertFalse(ui._approval_choice_yes)

    def test_tui_escape_cancels_config_draft_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-config-cancel-") as workspace:
            settings = Path(workspace) / ".harness" / "config" / "settings.json"

            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "TUI did not request command input")
                pipe.send_text("/配置 编辑\r")
                self._wait_until(lambda: ui._prompt.startswith("提供商 ["), "config prompt missing")
                pipe.send_text("\x1b")
                self._wait_until(
                    lambda: "已取消配置编辑；没有写入文件" in ui._transcript,
                    "Escape did not cancel the config draft",
                )
                self.assertFalse(settings.exists())
                pipe.send_text("/退出\r")

            ui, rc = self._run_cli_ui(workspace, MockProvider(), drive)
            self.assertEqual(rc, 0)
            self.assertFalse(settings.exists())
            self.assertIn("已取消配置编辑", ui._transcript)

    def test_secret_like_chat_input_is_hidden_and_rejected_offline(self) -> None:
        from prompt_toolkit.clipboard import DummyClipboard
        from prompt_toolkit.history import DummyHistory

        secret = "sk-" + "T" * 24
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-secret-input-") as workspace:
            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "TUI did not request chat input")
                pipe.send_text(secret + "\r")
                self._wait_until(
                    lambda: "疑似凭据输入已隐藏" in ui._transcript,
                    "sensitive input was not hidden from transcript",
                )
                self._wait_until(
                    lambda: "内容不会显示、发送给模型或保存" in ui._transcript,
                    "sensitive input was not rejected",
                )
                pipe.send_text("/退出\r")

            with patch("cvfx_harness.cli.Agent.ask") as ask:
                ui, rc = self._run_cli_ui(workspace, MockProvider(), drive)
            ask.assert_not_called()
            self.assertEqual(rc, 0)
            self.assertNotIn(secret, ui._transcript)
            self.assertIsInstance(ui._input_buffer.history, DummyHistory)
            self.assertEqual(list(ui._input_buffer.history.load_history_strings()), [])
            self.assertIsInstance(ui._application.clipboard, DummyClipboard)

    def test_model_questionnaire_review_then_explicit_submit_is_not_transcribed(self) -> None:
        class QuestionProvider:
            label = "synthetic questionnaire fixture"
            model = "offline"

            def __init__(self):
                self.calls = 0
                self.requests = []

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del tools, cancellation_event, max_tokens
                self.requests.append(list(messages))
                self.calls += 1
                if self.calls == 1:
                    return AssistantTurn("", [ToolCall("ask-1", "ask_user", {
                        "questions": [
                            {"id": "priority", "question": "本次先关注哪一方面？", "suggestions": ["稳定性", "易用性"]},
                            {"id": "scope", "question": "允许检查哪些目录？"},
                            {"id": "optional", "question": "还有补充吗？（可以明确留空）"},
                        ]
                    })])
                return AssistantTurn("已按用户本次回答继续。")

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-questionnaire-") as workspace:
            provider = QuestionProvider()

            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "initial prompt was not ready")
                pipe.send_text("请协助安排工作")
                pipe.send_bytes(b"\r")
                self._wait_until(lambda: ui._question_pending is not None, "model question was not presented")
                self.assertFalse(ui._question_reviewing)
                ui._question_suggestion_buttons[0].handler()
                self.assertEqual(ui._question_answer_area.text, "稳定性")
                self.assertIsNotNone(ui._question_pending, "a suggestion click must only fill the draft")
                ui._question_next_button.handler()
                self.assertTrue(
                    ui._application.layout.has_focus(ui._question_answer_area),
                    "moving to the next question must return keyboard focus to its answer field",
                )
                pipe.send_text("仅检查 src/")
                pipe.send_bytes(b"\r\r")
                pipe.send_bytes("\x1b[200~不要运行测试\x1b[201~".encode("utf-8"))
                self._wait_until(
                    lambda: ui._question_answer_area.text == "仅检查 src/\n\n不要运行测试",
                    "Enter or bracketed paste did not preserve the multiline answer",
                )
                self.assertIsNotNone(ui._question_pending, "Enter and pasted text must not submit the questionnaire")
                ui._question_next_button.handler()
                self.assertEqual(ui._question_heading_text(), "模型提问 · 3/3")
                self.assertEqual(ui._question_answer_area.text, "")
                ui._question_review_button.handler()
                self.assertTrue(ui._question_reviewing)
                self.assertIn("稳定性", ui._question_review_area.text)
                self.assertIn("\n\n不要运行测试", ui._question_review_area.text)
                self.assertIn("（空回答）", ui._question_review_area.text)
                self.assertNotIn("稳定性", ui._transcript)
                self.assertNotIn("不要运行测试", ui._transcript)
                self.assertNotIn("本次先关注哪一方面", ui._transcript)
                self.assertTrue(ui._application.layout.has_focus(ui._question_edit_button))
                self.assertFalse(ui._question_save_with_session)
                pipe.send_bytes(b"\t")
                self._wait_until(
                    lambda: ui._application.layout.has_focus(ui._question_save_button),
                    "Tab did not focus the default-off local-save choice",
                )
                pipe.send_bytes(b"\r")
                self._wait_until(
                    lambda: ui._question_save_with_session,
                    "explicit save choice did not toggle on",
                )
                self.assertIsNotNone(ui._question_pending, "save choice must not submit the questionnaire")
                pipe.send_bytes(b"\t")
                self._wait_until(
                    lambda: ui._application.layout.has_focus(ui._question_submit_button),
                    "Tab did not focus explicit questionnaire submit",
                )
                self.assertIsNotNone(ui._question_pending, "focus changes must not submit the questionnaire")
                pipe.send_bytes(b"\r")
                self._wait_until(
                    lambda: "已按用户本次回答继续。" in ui._transcript,
                    "provider did not continue after explicit submit",
                )
                self.assertEqual(provider.calls, 2)
                tool_result = json.loads(provider.requests[1][-1]["content"])
                self.assertEqual(tool_result["status"], "submitted")
                self.assertTrue(tool_result["save_with_session"])
                self.assertEqual(tool_result["answers"][0]["answer"], "稳定性")
                self.assertEqual(tool_result["answers"][1]["answer"], "仅检查 src/\n\n不要运行测试")
                self.assertEqual(tool_result["answers"][2]["answer"], "")
                self.assertNotIn("稳定性", ui._transcript)
                self.assertNotIn("不要运行测试", ui._transcript)
                pipe.send_text("/退出")
                pipe.send_bytes(b"\r")

            ui, rc = self._run_cli_ui(workspace, provider, drive)
            from prompt_toolkit.clipboard import DummyClipboard
            from prompt_toolkit.history import DummyHistory

            self.assertEqual(rc, 0)
            self.assertIsInstance(ui._question_answer_area.buffer.history, DummyHistory)
            self.assertIsInstance(ui._application.clipboard, DummyClipboard)
            self.assertEqual(list(ui._question_answer_area.buffer.history.load_history_strings()), [])

    def test_questionnaire_save_choice_is_locked_for_unsaved_context(self) -> None:
        request = QuestionnaireRequest(
            (UserQuestion("detail", "请补充普通背景。", ()),),
            save_allowed=False,
        )
        results: list[QuestionnaireResult] = []

        def worker(ui: TerminalUI) -> int:
            results.append(ui.ask_questions(request))
            return 0

        def drive(_pipe, ui: TerminalUI) -> None:
            self._wait_until(lambda: ui._question_ready.is_set(), "locked questionnaire was not presented")
            ui._question_answer_area.text = "普通背景"
            ui._question_show_review()
            self.assertIn("当前对话含未存档上下文", ui._question_review_area.text)
            self.assertIn("不能随 /保存 存档", ui._question_review_area.text)
            ui._question_toggle_session_save()
            self.assertFalse(ui._question_save_with_session)
            ui._application.layout.focus(ui._question_edit_button)
            ui._focus_question_control(forward=True)
            self.assertTrue(ui._application.layout.has_focus(ui._question_submit_button))
            ui._question_submit()
            self._wait_until(lambda: bool(results), "locked questionnaire did not return")

        ui, rc = self._run_ui(worker, drive)
        self.assertEqual(rc, 0)
        self.assertEqual(results[0].status, "submitted")
        self.assertFalse(results[0].save_with_session)
        self.assertFalse(ui._question_save_with_session)

    def test_questionnaire_escape_returns_cancelled_empty_answers_and_resumes_prompt(self) -> None:
        class CancelQuestionProvider:
            label = "synthetic cancellation fixture"
            model = "offline"

            def __init__(self):
                self.calls = 0
                self.requests = []

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del tools, cancellation_event, max_tokens
                self.requests.append(list(messages))
                self.calls += 1
                if self.calls == 1:
                    return AssistantTurn("", [ToolCall("ask-cancel", "ask_user", {
                        "questions": [{"id": "detail", "question": "需要澄清的合成问题是什么？"}]
                    })])
                return AssistantTurn("收到取消结果，等待后续输入。")

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-question-cancel-") as workspace:
            provider = CancelQuestionProvider()

            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "initial prompt was not ready")
                pipe.send_text("开始询问")
                pipe.send_bytes(b"\r")
                self._wait_until(lambda: ui._question_pending is not None, "question panel was not shown")
                pipe.send_text("本地草稿不要发送")
                pipe.send_bytes(b"\x1b")
                self._wait_until(
                    lambda: "收到取消结果，等待后续输入。" in ui._transcript
                    and ui._pending is not None,
                    "cancellation did not return to regular chat input",
                )
                self.assertNotIn("本地草稿不要发送", ui._transcript)
                self.assertNotIn("需要澄清的合成问题是什么", ui._transcript)
                tool_result = json.loads(provider.requests[1][-1]["content"])
                self.assertEqual(tool_result["status"], "cancelled")
                self.assertEqual(tool_result["answers"], [])
                self.assertIn("不得推断或伪造答案", tool_result["message"])
                pipe.send_text("/退出")
                pipe.send_bytes(b"\r")

            ui, rc = self._run_cli_ui(workspace, provider, drive)
            self.assertEqual(rc, 0)
            self.assertEqual(provider.calls, 2)

    def test_ctrl_c_cancels_model_round_and_returns_to_chat_input(self) -> None:
        class OneQuestionProvider:
            label = "synthetic ctrl-c fixture"
            model = "offline"
            calls = 0

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del messages, tools, cancellation_event, max_tokens
                self.calls += 1
                return AssistantTurn("", [ToolCall("ask-c-c", "ask_user", {
                    "questions": [{"id": "q", "question": "临时合成提问"}]
                })])

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-question-ctrl-c-") as workspace:
            provider = OneQuestionProvider()

            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "initial prompt was not ready")
                pipe.send_text("开始")
                pipe.send_bytes(b"\r")
                self._wait_until(lambda: ui._question_pending is not None, "question panel was not shown")
                pipe.send_bytes(b"\x03")
                self._wait_until(
                    lambda: "已取消当前模型请求" in ui._transcript and ui._pending is not None,
                    "Ctrl+C did not cancel the model round and resume chat",
                )
                self.assertNotIn("临时合成提问", ui._transcript)
                pipe.send_text("/退出")
                pipe.send_bytes(b"\r")

            ui, rc = self._run_cli_ui(workspace, provider, drive)
            self.assertEqual(rc, 0)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(ui._application.clipboard.get_data().text, "")

    def test_questionnaire_panel_and_status_fit_narrow_terminal(self) -> None:
        class SizedDummyOutput(DummyOutput):
            def get_size(self):
                return Size(rows=14, columns=46)

        request = QuestionnaireRequest(questions=(
            UserQuestion("scope", "這是一段用來檢查窄終端換行的合成問題？", ("src/", "tests/")),
        ))
        results = []

        def worker(ui: TerminalUI) -> int:
            results.append(ui.ask_questions(request))
            return 0

        def drive(pipe, ui: TerminalUI) -> None:
            self._wait_until(lambda: ui._question_pending is not None, "questionnaire panel was not shown")
            self._wait_until(
                lambda: ui._application.renderer.last_rendered_screen is not None,
                "questionnaire did not render",
            )
            time.sleep(0.03)
            screen = ui._application.renderer.last_rendered_screen
            self.assertIsNotNone(screen)
            positions = screen.visible_windows_to_write_positions
            heading = positions.get(ui._question_heading)
            prompt = positions.get(ui._question_prompt_area.window)
            answer = positions.get(ui._question_answer_area.window)
            status = positions.get(ui._status_window)
            self.assertIsNotNone(heading)
            self.assertIsNotNone(prompt)
            self.assertIsNotNone(answer)
            self.assertIsNotNone(status)
            self.assertLess(heading.ypos, prompt.ypos)
            self.assertLess(prompt.ypos, answer.ypos)
            self.assertLess(answer.ypos, status.ypos)
            self.assertEqual(status.ypos, 13)
            suggestion = ui._question_suggestion_buttons[0]
            self._wait_until(
                lambda: suggestion.window.render_info is not None,
                "suggested answer button was not rendered for mouse input",
            )
            render_info = suggestion.window.render_info
            assert render_info is not None
            click_x = render_info._x_offset + 2
            click_y = render_info._y_offset + 1
            pipe.send_bytes(f"\x1b[<0;{click_x};{click_y}M\x1b[<0;{click_x};{click_y}m".encode())
            self._wait_until(
                lambda: ui._question_answer_area.text == "src/",
                "mouse click did not fill the suggested answer",
            )
            self.assertIsNotNone(ui._question_pending, "mouse click must not submit an answer")
            ui._question_cancel()

        ui, rc = self._run_ui(worker, drive, output=SizedDummyOutput())
        self.assertEqual(rc, 0)
        self.assertEqual(results, [QuestionnaireResult("cancelled")])

    def test_tui_plan_proposal_review_revision_and_acceptance_loop(self) -> None:
        from cvfx_harness.plans import PlanStore

        class SyntheticPlanProvider:
            label = "synthetic-plan-fixture"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests = []

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del cancellation_event, max_tokens
                self.requests.append((messages, tools))
                return AssistantTurn(
                    '{"steps":[{"description":"检查中文界面","completion_criteria":"列出可见命令",'
                    '"required_tools":["list_directory"]}]}',
                    assistant_message={
                        "role": "assistant",
                        "content": '{"steps":[{"description":"检查中文界面","completion_criteria":"列出可见命令",'
                        '"required_tools":["list_directory"]}]}',
                    },
                )

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-plan-") as workspace:
            provider = SyntheticPlanProvider()

            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "TUI did not request plan input")
                pipe.send_text("/计划 生成 检查命令\r")
                self._wait_until(lambda: "待审阅候选计划" in ui._transcript, "candidate was not rendered")
                self.assertEqual(provider.requests[0][1], [])
                self.assertIn("建议工具：列出目录", ui._transcript)
                pipe.send_text("/计划 修订 1 条件 命令清单可见\r")
                self._wait_until(lambda: "已修订候选" in ui._transcript, "revision was not rendered")
                pipe.send_text("/计划 接受\r")
                self._wait_until(lambda: "已接受并保存" in ui._transcript, "proposal was not accepted")
                pipe.send_text("/计划\r")
                self._wait_until(lambda: "[待办] 检查中文界面" in ui._transcript, "accepted step was not displayed")
                pipe.send_text("/退出\r")

            _ui, rc = self._run_cli_ui(workspace, provider, drive)
            self.assertEqual(rc, 0)
            saved = PlanStore(Workspace(workspace)).load()
            self.assertEqual(saved.steps[0].completion_criteria, "命令清单可见")
            self.assertEqual(len(provider.requests), 1)

    def test_test_profile_id_completion_suggests_runnable_and_removable_profiles(self) -> None:
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        with tempfile.TemporaryDirectory(prefix="cvfx-test-complete-") as root:
            project = Path(root)
            (project / "tests").mkdir()
            workspace = Workspace(project)
            store = TestProfileStore(workspace)
            store.add("unit-main", "unittest", sys.executable, ".", "tests")
            config_path = project / ".harness" / "config" / "test_profiles.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["profiles"].append(
                {"id": "legacy-unit", "framework": "unittest", "cwd": ".", "target": "tests"}
            )
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            before = config_path.read_bytes()
            completer = CommandCompleter(workspace)
            event = CompleteEvent()

            runnable = list(
                completer.get_completions(Document("/测试 运行 "), event)
            )
            removable = list(
                completer.get_completions(Document("/测试 移除 "), event)
            )
            after = config_path.read_bytes()

        self.assertEqual([item.text for item in runnable], ["unit-main"])
        self.assertEqual(runnable[0].display_meta_text, "unittest，可运行")
        self.assertEqual([item.text for item in removable], ["legacy-unit", "unit-main"])
        self.assertEqual(removable[0].display_meta_text, "旧配置，需重新添加解释器")
        self.assertEqual(before, after)

    def test_workspace_reference_completer_handles_braces_paths_and_filters(self) -> None:
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-complete-") as root:
            folder = Path(root) / "资料"
            folder.mkdir()
            (folder / "需求 文档.md").write_text("合成正文", encoding="utf-8")
            (folder / "README.md").write_text("合成正文", encoding="utf-8")
            (folder / "api_key.txt").write_text("synthetic", encoding="utf-8")
            (folder / "图像").mkdir()
            workspace = Workspace(root)
            completer = CommandCompleter(workspace)
            event = CompleteEvent()

            braced = list(
                completer.get_completions(Document("请检查 @{资料/需"), event)
            )
            directory = list(
                completer.get_completions(Document("请检查 @{资料/图"), event)
            )
            unbraced = list(
                completer.get_completions(Document("请检查 @资料/READ"), event)
            )
            sensitive = list(
                completer.get_completions(Document("请检查 @{资料/api"), event)
            )
            email = list(
                completer.get_completions(Document("联系 user@exam"), event)
            )

        self.assertEqual([(item.text, item.start_position, item.display_meta_text) for item in braced],
                         [("需求 文档.md}", -1, "文件")])
        self.assertEqual([(item.text, item.display_meta_text) for item in directory],
                         [("图像/", "目录")])
        self.assertEqual([(item.text, item.display_meta_text) for item in unbraced],
                         [("README.md ", "文件")])
        self.assertEqual(sensitive, [])
        self.assertEqual(email, [])

    def test_session_name_completion_and_real_input_save_reopen_restore_resume_cancel(self) -> None:
        class CapturingMockProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.requests: list[list[dict[str, object]]] = []

            def complete(self, messages, tools, **kwargs):
                self.requests.append([dict(message) for message in messages])
                return super().complete(messages, tools, **kwargs)

        with tempfile.TemporaryDirectory(prefix="会话 工作区 ") as workspace:
            first_provider = CapturingMockProvider()

            def save_and_exit(pipe, ui: TerminalUI) -> None:
                self._wait_until(
                    lambda: ui._pending is not None and not ui._pending.approval,
                    "initial TUI input did not become ready",
                )
                pipe.send_text("请记住这段中文合成历史\r")
                self._wait_until(
                    lambda: len(first_provider.requests) == 1
                    and "[模拟模式] 已收到" in ui._transcript,
                    "first mock dialogue did not finish",
                )
                pipe.send_text("/保存 中文会话\r")
                self._wait_until(
                    lambda: "已保存 1 个安全对话轮次" in ui._transcript,
                    "session was not saved from the TUI",
                )
                pipe.send_text("/退出\r")

            _first_ui, first_result = self._run_cli_ui(
                workspace, first_provider, save_and_exit
            )
            self.assertEqual(first_result, 0)
            archive = Path(workspace) / ".harness" / "sessions" / "中文会话.json"
            original_archive = archive.read_bytes()

            from prompt_toolkit.completion import CompleteEvent
            from prompt_toolkit.document import Document

            names = [
                item.text
                for item in CommandCompleter(Workspace(workspace)).get_completions(
                    Document("/恢复 中"), CompleteEvent()
                )
            ]
            self.assertEqual(names, ["中文会话"])

            resumed_provider = CapturingMockProvider()

            def restore_continue_and_exit(pipe, ui: TerminalUI) -> None:
                self._wait_until(
                    lambda: ui._pending is not None and not ui._pending.approval,
                    "reopened TUI input did not become ready",
                )
                pipe.send_text("/恢复 ")
                self._wait_until(
                    lambda: ui._input_buffer.complete_state is not None
                    and any(
                        item.text == "中文会话"
                        for item in ui._input_buffer.complete_state.completions
                    ),
                    "saved session name was not offered by the live TUI",
                )
                self.assertEqual(resumed_provider.requests, [])
                pipe.send_text("\t\r")
                self._wait_until(
                    lambda: "已恢复历史会话 '中文会话'" in ui._transcript,
                    "selected session was not restored",
                )
                self.assertIn("审批与执行授权不会恢复", ui._transcript)
                self.assertIn("旧工具调用不会执行", ui._transcript)
                self.assertEqual(resumed_provider.requests, [])
                pipe.send_text("沿用历史内容继续讨论\r")
                self._wait_until(
                    lambda: len(resumed_provider.requests) == 1
                    and "[模拟模式] 已收到" in ui._transcript,
                    "mock conversation did not continue after restore",
                )
                pipe.send_text("/退出\r")

            _resumed_ui, resumed_result = self._run_cli_ui(
                workspace, resumed_provider, restore_continue_and_exit
            )
            self.assertEqual(resumed_result, 0)
            request_text = "\n".join(
                str(message.get("content", ""))
                for message in resumed_provider.requests[0]
            )
            self.assertIn("请记住这段中文合成历史", request_text)
            self.assertIn("沿用历史内容继续讨论", request_text)
            self.assertEqual(archive.read_bytes(), original_archive)

            cancelled_provider = CapturingMockProvider()

            def cancel_selection_and_exit(pipe, ui: TerminalUI) -> None:
                self._wait_until(
                    lambda: ui._pending is not None and not ui._pending.approval,
                    "cancel TUI input did not become ready",
                )
                pipe.send_text("/恢复 中")
                self._wait_until(
                    lambda: ui._input_buffer.complete_state is not None
                    and bool(ui._input_buffer.complete_state.completions),
                    "session suggestion was not available to cancel",
                )
                pipe.send_bytes(b"\x1b")
                time.sleep(0.03)
                self.assertIsNotNone(ui._pending, "Esc must only close suggestions")
                pipe.send_bytes(b"\x03")
                self._wait_until(
                    lambda: ui._input_buffer.text == "",
                    "Ctrl+C did not clear the unsubmitted restore selection",
                )
                self.assertEqual(cancelled_provider.requests, [])
                self.assertNotIn("已恢复历史会话", ui._transcript)
                pipe.send_text("/退出\r")

            _cancelled_ui, cancelled_result = self._run_cli_ui(
                workspace, cancelled_provider, cancel_selection_and_exit
            )
            self.assertEqual(cancelled_result, 0)
            self.assertEqual(cancelled_provider.requests, [])
            self.assertEqual(archive.read_bytes(), original_archive)

    def test_file_reference_completion_still_requires_read_approval_in_mock_cli(self) -> None:
        class CapturingMockProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.requests: list[list[dict[str, object]]] = []

            def complete(self, messages, tools, **kwargs):
                self.requests.append([dict(message) for message in messages])
                return super().complete(messages, tools, **kwargs)

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-reference-") as workspace:
            folder = Path(workspace) / "资料"
            folder.mkdir()
            content = "只有审批后才能发送的合成文件正文"
            (folder / "需求 文档.md").write_text(content, encoding="utf-8")
            provider = CapturingMockProvider()

            def send(pipe, ui: TerminalUI) -> None:
                self._wait_until(
                    lambda: ui._pending is not None and not ui._pending.approval,
                    "main input prompt did not become ready",
                )
                pipe.send_text("请检查 @{资料/需")
                self._wait_until(
                    lambda: ui._input_buffer.complete_state is not None
                    and any(
                        item.text == "需求 文档.md}"
                        for item in ui._input_buffer.complete_state.completions
                    ),
                    "safe file-reference suggestion did not appear",
                )
                pipe.send_text("\t\r")
                self._wait_until(
                    lambda: ui._pending is not None and ui._pending.approval,
                    "file reference did not ask for read approval",
                )
                self.assertEqual(provider.requests, [])
                self.assertNotIn(content, ui._transcript)
                self.assertIn("需求 文档.md", ui._approval_prompt_area.text)
                pipe.send_text("\t\r")
                self._wait_until(
                    lambda: ui._pending is not None
                    and not ui._pending.approval
                    and bool(provider.requests),
                    "approved file reference did not reach the offline provider",
                )
                self.assertTrue(
                    any(
                        content in message.get("content", "")
                        for message in provider.requests[0]
                        if message.get("role") == "user"
                    )
                )
                pipe.send_text("/退出\r")

            ui, result = self._run_cli_ui(workspace, provider, send)

        self.assertEqual(result, 0)
        self.assertEqual(len(provider.requests), 1)
        self.assertIn("审批选择：是（本次）", ui._transcript)
        self.assertIn("[模拟模式] 已收到", ui._transcript)

    def test_tui_search_uses_real_input_and_approves_only_bounded_scope(self) -> None:
        class SearchProvider:
            label = "offline search fixture"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests: list[list[dict[str, object]]] = []
                self.tool_schemas: list[list[dict[str, object]]] = []

            def complete(self, messages, tools, **kwargs):
                del kwargs
                self.requests.append([dict(message) for message in messages])
                self.tool_schemas.append(list(tools))
                if len(self.requests) == 1:
                    call = ToolCall(
                        "tui-search",
                        "search_text",
                        {"query": "预算阈值", "path": "src", "glob": "*.py"},
                    )
                    return AssistantTurn("", [call])
                return AssistantTurn("已找到 src/配置.py 第 2 行。")

        with tempfile.TemporaryDirectory(prefix="cvfx-tui-search-") as workspace:
            source = Path(workspace) / "src" / "配置.py"
            source.parent.mkdir()
            source.write_bytes("# 标题\r\n预算阈值=16384\r\n".encode("utf-8"))
            provider = SearchProvider()

            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(
                    lambda: ui._pending is not None and not ui._pending.approval,
                    "main input prompt did not become ready",
                )
                pipe.send_text("帮我查找预算阈值\r")
                self._wait_until(
                    lambda: ui._pending is not None and ui._pending.approval,
                    "workspace text search did not request approval",
                )
                self.assertEqual(len(provider.requests), 1)
                self.assertTrue(ui._prompt.startswith("确认搜索该范围并将脱敏匹配片段发送给模型"))
                self.assertTrue(ui._approval_prompt_area.text.startswith("确认搜索该范围并将脱敏匹配片段发送给模型"))
                self.assertIn("预算阈值", ui._approval_text)
                self.assertIn("工作区相对范围：src", ui._transcript)
                self.assertIn("*.py", ui._transcript)
                self.assertNotIn("16384", ui._transcript)
                self.assertFalse(ui._input_block_container.filter())
                pipe.send_text("\t\r")
                self._wait_until(
                    lambda: ui._pending is not None
                    and not ui._pending.approval
                    and len(provider.requests) == 2,
                    "approved search result did not reach the offline provider",
                )
                result = next(
                    message["content"]
                    for message in provider.requests[1]
                    if message.get("role") == "tool"
                )
                self.assertIn('"untrusted":true', result)
                self.assertIn('"path":"src/配置.py"', result)
                self.assertIn('"line":2', result)
                self.assertIn("search_text", [
                    schema["function"]["name"] for schema in provider.tool_schemas[0]
                ])
                pipe.send_text("/退出\r")

            ui, rc = self._run_cli_ui(workspace, provider, drive)

        self.assertEqual(rc, 0)
        self.assertIn("审批选择：是（本次）", ui._transcript)
        self.assertIn("已找到 src/配置.py 第 2 行。", ui._transcript)

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

    def test_mouse_click_fills_command_candidate_without_submitting(self) -> None:
        received: list[str] = []

        def worker(ui: TerminalUI) -> int:
            received.append(ui.input("你> "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            pipe.send_text("/")
            self._wait_until(
                lambda: ui._input_buffer.complete_state is not None
                and ui._completion_window.render_info is not None,
                "command menu did not render for mouse selection",
            )
            render_info = ui._completion_window.render_info
            assert render_info is not None
            self.assertGreaterEqual(len(ui._input_buffer.complete_state.completions), 2)
            click_x = render_info._x_offset + 1
            click_y = render_info._y_offset + 2  # terminal mouse coordinates are one-based
            pipe.send_bytes(
                f"\x1b[<0;{click_x};{click_y}M\x1b[<0;{click_x};{click_y}m".encode()
            )
            self._wait_until(
                lambda: ui._input_buffer.text == "/命令"
                and ui._input_buffer.complete_state is None,
                "mouse click did not fill the second command candidate",
            )
            self.assertIsNotNone(ui._pending, "clicking a candidate must not submit it")
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["/命令"])

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
            self.assertFalse(ui._input_block_container.filter())
            self.assertNotIn("[y/N]", ui._prompt)
            self.assertEqual(ui._approval_prompt_area.text, "确认写入文件？")
            self.assertNotIn("确认写入文件？", ui._transcript)
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
            self.assertEqual(ui._approval_prompt_area.text, "确认写入文件？")
            self.assertTrue(ui._application.layout.has_focus(ui._approval_no_button))
            self.assertFalse(ui._input_block_container.filter())
            self.assertEqual(ui._approval_no_button.text, "拒绝")
            self.assertEqual(ui._approval_yes_button.text, "本次批准")
            pipe.send_text("\t")
            time.sleep(0.05)
            self.assertTrue(ui._approval_choice_yes)
            self.assertTrue(ui._application.layout.has_focus(ui._approval_yes_button))
            self.assertEqual(ui._approval_yes_button.text, "本次批准")
            self.assertIsNotNone(ui._pending, "selection alone must not approve")
            pipe.send_text("\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["是"])
        self.assertIn("当前选择：是", ui._notice)

    def test_change_set_approval_keeps_full_diff_in_transcript_and_compacts_dock(self) -> None:
        received: list[str] = []
        review = (
            "\n多文件变更集完整审阅（编号 0123456789abcdef）：\n"
            "变更集审阅编号：0123456789abcdef\n"
            "文件：a.txt\n原文件 SHA-256：0123456789abcdef…\n"
            "精确差异\n-旧内容甲\n+新内容甲\n"
            "文件：b.txt\n原文件 SHA-256：fedcba9876543210…\n"
            "精确差异\n-旧内容乙\n+新内容乙"
        )

        def worker(ui: TerminalUI) -> int:
            ui.output(review)
            received.append(ui.input("确认应用整批变更？[y/N] "))
            return 0

        def send(pipe, ui: TerminalUI) -> None:
            self._wait_until(
                lambda: ui._pending is not None and ui._pending.approval,
                "change-set approval was not shown",
            )
            self.assertIn("0123456789abcdef", ui._approval_text)
            self.assertNotIn("新内容甲", ui._approval_text)
            self.assertNotIn("新内容乙", ui._approval_text)
            self.assertIn("新内容甲", ui._transcript)
            self.assertIn("新内容乙", ui._transcript)
            self.assertEqual(ui._approval_prompt_area.text, "确认应用整批变更？")
            self.assertNotIn("确认应用整批变更？", ui._transcript)
            pipe.send_text("\t\r")

        ui, result = self._run_ui(worker, send)
        self.assertEqual(result, 0)
        self.assertEqual(received, ["是"])
        self.assertIn("审批选择：是（本次）", ui._transcript)

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
        from prompt_toolkit.styles import Style
        from cvfx_harness.tui import _style_rules_for_environment

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
            dark_style = Style.from_dict(_style_rules_for_environment({}))
            current_style = dark_style.get_attrs_for_style_str(
                "class:completion-menu.completion.current"
            )
            self.assertEqual(current_style.bgcolor, "6bbdb4")
            self.assertEqual(current_style.color, "0b0d10")
            self.assertTrue(current_style.bold)
            self.assertTrue(current_style.underline)
            current_meta_style = dark_style.get_attrs_for_style_str(
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

        for columns in (20, 24, 30, 36, 38, 46, 48, 64, 80, 100):
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
                self.assertIn("Tab补全" if columns >= 36 else "Enter", line)
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
                self.assertIn("Esc取消", line)
                if columns >= 30:
                    self.assertIn("审阅", line)
                if columns >= 38:
                    self.assertIn("Tab", line)
                if columns >= 58:
                    self.assertIn("PgUp/PgDn审阅", line)

    def test_header_truncates_provider_and_workspace_at_terminal_width(self) -> None:
        class SizedDummyOutput(DummyOutput):
            def __init__(self, columns: int) -> None:
                self.columns = columns

            def get_size(self):
                return Size(rows=24, columns=self.columns)

        for columns in (20, 46, 64, 100):
            with self.subTest(columns=columns), create_pipe_input() as pipe:
                ui = TerminalUI(
                    lambda _ui: 0,
                    input=pipe,
                    output=SizedDummyOutput(columns),
                )
                ui._header_model = (
                    "v0.5.0 | 提供商：deepseek-anthropic | 模型：deepseek-v4-pro-very-long"
                )
                ui._header_workspace = "/tmp/private-parent/a-very-long-repository/workspace"
                fragments = ui._header_fragments()
                header = "".join(fragment for _style, fragment in fragments)
                self.assertLessEqual(wcswidth(header), columns)
                self.assertIn("chenxiVFX Harness" if columns >= 17 else "…", header)
                if columns >= 40:
                    self.assertIn("…", header)

    def test_patch_diff_palette_handles_no_color_and_light_background_contrast(self) -> None:
        from prompt_toolkit.document import Document
        from cvfx_harness.tui import _TranscriptDiffLexer, _style_rules_for_environment

        no_color = _style_rules_for_environment({"NO_COLOR": "1", "COLORFGBG": "0;15"})
        self.assertTrue(no_color)
        self.assertTrue(all(not value for value in no_color.values()))

        def contrast_ratio(foreground: str, background: str) -> float:
            def luminance(color: str) -> float:
                channels = [int(color[offset : offset + 2], 16) / 255 for offset in (1, 3, 5)]
                linear = [
                    value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                    for value in channels
                ]
                return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))

            first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
            return (first + 0.05) / (second + 0.05)

        for environment, background in (({}, "#0b0d10"), ({"COLORFGBG": "0;15"}, "#ffffff")):
            rules = _style_rules_for_environment(environment)
            for name in ("transcript.diff.add", "transcript.diff.remove"):
                foreground = rules[name].split("fg:", 1)[1].split()[0]
                self.assertGreaterEqual(contrast_ratio(foreground, background), 4.5)

        document = Document(
            "补丁差异\n目标文件：合成.txt\n指纹：abc\n变化（+ 新增，- 删除）：\n"
            "-旧值\n+新值\n\n+普通对话"
        )
        get_line = _TranscriptDiffLexer().lex_document(document)
        self.assertEqual(get_line(4)[0][0], "class:transcript.diff.remove")
        self.assertEqual(get_line(5)[0][0], "class:transcript.diff.add")
        self.assertEqual(get_line(7)[0][0], "")

        with patch.dict(os.environ, {"NO_COLOR": "1", "COLORFGBG": "0;15"}):
            with create_pipe_input() as pipe:
                no_color_ui = TerminalUI(lambda _ui: 0, input=pipe, output=DummyOutput())
                no_color_style = no_color_ui._application.style.get_attrs_for_style_str(
                    "class:transcript.diff.add"
                )
                self.assertEqual(no_color_style.color, "")
                self.assertEqual(no_color_style.bgcolor, "")

        with patch.dict(os.environ, {"NO_COLOR": "", "COLORFGBG": "0;15"}):
            with create_pipe_input() as pipe:
                light_ui = TerminalUI(lambda _ui: 0, input=pipe, output=DummyOutput())
                light_style = light_ui._application.style.get_attrs_for_style_str(
                    "class:transcript.diff.add"
                )
                self.assertEqual(light_style.color, "145a32")

    def test_status_command_exposes_full_workspace_after_header_truncation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-状态入口-") as outer:
            workspace = str(Path(outer) / "项目 中文 with spaces")
            Path(workspace).mkdir()
            provider = MockProvider()

            def send(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "TUI did not become ready")
                pipe.send_text("/状态\r")
                self._wait_until(
                    lambda: (
                        f"工作区：{Path(workspace).resolve()}" in ui._transcript
                        and "工作区根身份：有效" in ui._transcript
                        and "工作区信任与审批边界" in ui._transcript
                    ),
                    "status command did not show the full workspace path",
                )
                pipe.send_text("/退出\r")

            ui, result = self._run_cli_ui(workspace, provider, send)
            self.assertEqual(result, 0)
            self.assertIn("提供商：", ui._transcript)
            self.assertIn(f"工作区：{Path(workspace).resolve()}", ui._transcript)
            self.assertIn("工作区信任与审批边界", ui._transcript)
            self.assertIn("工作区根身份：有效", ui._transcript)
            self.assertIn("--workspace 启动新会话", ui._transcript)

    def test_tui_keeps_the_startup_root_if_its_path_is_replaced_before_worker_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-root-swap-") as temp:
            selected = Path(temp) / "selected 中文 root"
            selected.mkdir()
            args = build_parser().parse_args(
                ["--workspace", str(selected), "--provider", "mock"]
            )

            class SwappingUI:
                def __init__(self, worker, **kwargs) -> None:
                    self._worker = worker
                    self.reference_workspace = kwargs["reference_workspace"]
                    self.output_lines: list[str] = []
                    self.cancellation_event = threading.Event()

                def output(self, line: str) -> None:
                    self.output_lines.append(line)

                def stream_write(self, _fragment: str) -> None:
                    return

                def input(self, _prompt: str) -> str:
                    raise AssertionError("a replaced root must stop before requesting input")

                def ask_questions(self, _request):
                    raise AssertionError("a replaced root must stop before prompting")

                def run(self) -> int:
                    selected.rename(Path(temp) / "retired root")
                    selected.mkdir()
                    (selected / "marker.txt").write_text("new root", encoding="utf-8")
                    return self._worker(self)

            ui_instances: list[SwappingUI] = []

            def make_ui(worker, **kwargs):
                ui = SwappingUI(worker, **kwargs)
                ui_instances.append(ui)
                return ui

            with patch("cvfx_harness.tui.TerminalUI", side_effect=make_ui):
                result = run_tui_console(args)

            self.assertEqual(result, 2)
            self.assertTrue(
                any("工作区根目录在运行期间发生变化" in line for line in ui_instances[0].output_lines),
                repr(ui_instances[0].output_lines),
            )
            self.assertEqual((selected / "marker.txt").read_text(encoding="utf-8"), "new root")

    def test_long_prompt_submission_does_not_look_like_manual_transcript_scroll(self) -> None:
        from cvfx_harness.tui import _InputRequest

        with create_pipe_input() as pipe:
            ui = TerminalUI(lambda _ui: 0, input=pipe, output=DummyOutput())
            ui._transcript = "较早的合成对话\n" * 20
            ui.output_area.text = ui._transcript
            ui._follow_output = False
            ui._pending = _InputRequest("你> ", False, threading.Event())
            ui._input_buffer.text = "长中文提问" * 300

            ui._submit_input()
            self.assertTrue(ui._follow_output)
            self.assertTrue(ui._ignore_next_scroll_inference)

            ui._append_transcript("离线回答已完成。\n")
            self.assertTrue(ui._follow_output)
            self.assertFalse(ui._ignore_next_scroll_inference)
            self.assertEqual(ui.output_area.buffer.cursor_position, len(ui._transcript))

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

    def test_tui_cold_plan_resume_reviews_temporary_context_before_model_request(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-cold-plan-resume-") as workspace:
            plan = PlanStore(Workspace(workspace))
            plan.add("检查一个合成中文配置项")
            plan.transition(1, "in-progress")
            plan.record_progress(1, "provider_request")
            plan.recover_unfinished()

            class CapturingProvider(MockProvider):
                def __init__(self) -> None:
                    super().__init__()
                    self.requests: list[list[dict[str, object]]] = []
                    self.request_received = threading.Event()

                def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                    del tools, cancellation_event, max_tokens
                    self.requests.append([dict(message) for message in messages])
                    self.request_received.set()
                    return AssistantTurn("已按临时上下文完成合成续跑检查。")

            provider = CapturingProvider()
            supplement = "本次只核对合成配置的中文提示，不读取其他目录。"

            def drive(pipe, ui: TerminalUI) -> None:
                self._wait_until(lambda: ui._pending is not None, "initial CLI prompt was not ready")
                pipe.send_text("/继续 1")
                pipe.send_bytes(b"\r")
                self._wait_until(
                    lambda: ui._pending is not None
                    and "补充本次续跑需要的目标/约束" in ui._pending.prompt,
                    "cold resume context prompt was not shown in the TUI",
                )
                self.assertIn("模型上下文发送", ui._pending.prompt)
                pipe.send_text(supplement)
                pipe.send_bytes(b"\r")
                self._wait_until(
                    lambda: ui._pending is not None and ui._pending.approval,
                    "final cold resume confirmation was not shown",
                )
                self.assertEqual(ui._prompt, "确认继续？")
                self.assertFalse(ui._input_block_container.filter())
                self.assertIn("已接收本次临时目标/约束", ui._transcript)
                self.assertIn("续跑将启动空白 Agent；不重放旧工具调用或审批", ui._transcript)
                self.assertIn("PgUp/PgDn审阅", ui._status_line())
                ui._approval_yes_button.handler()
                ui._submit_input()
                self._wait_until(
                    lambda: provider.request_received.is_set()
                    and ui._pending is not None
                    and not ui._pending.approval,
                    "confirmed resume did not return to the chat prompt",
                )
                self.assertIn(supplement, json.dumps(provider.requests, ensure_ascii=False))
                self.assertIn("可用上下文：仅已保存步骤说明", ui._transcript)
                pipe.send_text("/退出")
                pipe.send_bytes(b"\r")
                self._wait_until(lambda: ui._worker_done, "TUI did not exit after the resumed step")

            ui, rc = self._run_cli_ui(workspace, provider, drive)
            self.assertEqual(plan.load().steps[0].status, "in-progress")

        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.requests), 1)
        self.assertIn("已按临时上下文完成合成续跑检查。", ui._transcript)

    def test_patch_approval_shows_compact_diff_and_escapes_terminal_control(self) -> None:
        trusted_fingerprint = "b" * 64
        forged_fingerprint = "a" * 64
        request = ActionRequest(
            operation="patch",
            path="资料/说明.txt",
            diff=(
                "--- a/资料/说明.txt\n+++ b/资料/说明.txt\n"
                "@@ -1 +1 @@\n"
                "-旧值\x1b[2J\n"
                f"+伪造可信指纹 {forged_fingerprint}\n+新值\n"
            ),
            trusted_file_sha256=trusted_fingerprint,
        )
        output: list[str] = []
        prompts: list[str] = []
        approved = _confirm_action(
            request,
            input_fn=lambda prompt: prompts.append(prompt) or "是",
            output=output.append,
        )
        self.assertTrue(approved)
        self.assertIn("补丁差异", output[0])
        self.assertIn("目标文件：资料/说明.txt", output[0])
        self.assertIn(
            "工作区原文件指纹（SHA-256 前 16 位）：" + trusted_fingerprint[:16] + "…",
            output[0],
        )
        self.assertNotIn(trusted_fingerprint, output[0])
        self.assertNotIn(forged_fingerprint, output[0])
        self.assertIn("[已脱敏]", output[0])
        self.assertNotIn("--- a/资料/说明.txt", output[0])
        self.assertIn("变化（+ 新增，- 删除）", output[0])
        self.assertIn("-旧值\\x1b[2J", output[0])
        self.assertIn("+新值", output[0])
        self.assertNotIn("\x1b[2J", output[0])
        self.assertEqual(prompts, ["确认应用这个精确补丁？[y/N] "])

    def test_patch_approval_without_structured_fingerprint_fails_closed(self) -> None:
        outputs: list[str] = []
        prompts: list[str] = []
        approved = _confirm_action(
            ActionRequest(operation="patch", path="合成.txt", diff="+内容"),
            input_fn=lambda prompt: prompts.append(prompt) or "是",
            output=outputs.append,
        )
        self.assertFalse(approved)
        self.assertEqual(prompts, [])
        self.assertIn("默认拒绝", outputs[0])

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
            ui.output(
                "Bearer SYNTHETIC_TUI_SECRET_1234567890 \x1b[2J"
                "\x1b]0;SYNTHETIC_TERMINAL_TITLE\x07"
            )
            ui.input("继续> ")
            return 0

        ui, result = self._run_ui(worker, lambda pipe, _ui: pipe.send_text("好\r"))
        self.assertEqual(result, 0)
        self.assertNotIn("SYNTHETIC_TUI_SECRET_1234567890", ui._transcript)
        self.assertNotIn("\x1b", ui._transcript)
        self.assertIn("\\x1b[2J", ui._transcript)
        self.assertIn("\\x1b]0;SYNTHETIC_TERMINAL_TITLE\\x07", ui._transcript)

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
    def test_real_posix_pty_completes_file_reference_without_skipping_approval(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-tui-menu-pty-") as workspace:
            folder = Path(workspace) / "资料"
            folder.mkdir()
            private_synthetic_content = "PTY_SYNTHETIC_BODY_REQUIRES_APPROVAL"
            (folder / "需求 文档.md").write_text(
                private_synthetic_content, encoding="utf-8"
            )
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
                # The longer command list can scroll its heading off a 30-row
                # PTY; confirm the visible tail of /命令 instead.
                read_until("Tab 补全命令名".encode("utf-8"), start=menu_start)
                self.assertIn("/命令".encode("utf-8"), transcript[menu_start:])

                path_start = len(transcript)
                os.write(master_fd, "请检查 @{资料/需".encode("utf-8"))
                read_until("需求 文档.md".encode("utf-8"), start=path_start)
                read_until("文件".encode("utf-8"), start=path_start)
                approval_start = len(transcript)
                os.write(master_fd, b"\t\r")
                read_until("确认读取？".encode("utf-8"), start=approval_start)
                self.assertNotIn(private_synthetic_content.encode("utf-8"), transcript)
                os.write(master_fd, b"\r")
                read_until("你[执行]> ".encode("utf-8"), start=approval_start)
                self.assertNotIn(private_synthetic_content.encode("utf-8"), transcript)
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
