from __future__ import annotations

import os
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.agent import Agent
from cvfx_harness.cli import _confirm_action
from cvfx_harness.profiles import TestProfileStore
from cvfx_harness.providers import AssistantTurn, OpenAICompatibleProvider, ToolCall
from cvfx_harness.test_runner import (
    MAX_TEST_OUTPUT_BYTES,
    _command_for,
    execute_profile,
)
from cvfx_harness.tools import ActionRequest, Workspace, WorkspaceError, run_tool


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "test_profile_runner"


class TestProcessRunner(unittest.TestCase):
    __test__ = False

    def _project(self, fixture: str) -> tuple[tempfile.TemporaryDirectory, Workspace, TestProfileStore, object]:
        tmp_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        tmp_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=tmp_root, prefix="用户测试夹具 ")
        project = Path(temporary.name)
        shutil.copytree(FIXTURES / fixture, project / "tests")
        workspace = Workspace(project)
        store = TestProfileStore(workspace)
        profile = store.add("unit", "unittest", ".", "tests")
        return temporary, workspace, store, profile

    def test_synthetic_user_profile_has_bounded_sanitized_output(self) -> None:
        temporary, workspace, _store, profile = self._project("basic")
        try:
            with patch.dict(os.environ, {"CVFX_SYNTHETIC_PARENT_MARKER": "parent-only"}):
                result = execute_profile(profile, workspace, timeout_seconds=10)
            self.assertEqual(result.status, "passed")
            self.assertIn("合成测试通过", result.output)
            self.assertIn("[已脱敏]", result.output)
            self.assertNotIn("synthetic-secret-value", result.output)
            self.assertIn("terminal=\\x1b", result.output)
            self.assertIn("parent_marker=missing", result.output)
        finally:
            temporary.cleanup()

    def test_output_limit_terminates_and_caps_retained_data(self) -> None:
        temporary, workspace, _store, profile = self._project("output_limit")
        try:
            result = execute_profile(profile, workspace, timeout_seconds=10)
            self.assertEqual(result.status, "output_limit")
            self.assertLessEqual(len(result.output.encode("utf-8")), MAX_TEST_OUTPUT_BYTES * 4)
            self.assertLessEqual(result.output_bytes, MAX_TEST_OUTPUT_BYTES + 1)
            self.assertTrue(result.output_truncated)
        finally:
            temporary.cleanup()

    def test_nonzero_test_exit_is_reported_as_failure(self) -> None:
        temporary, workspace, _store, profile = self._project("failure")
        try:
            result = execute_profile(profile, workspace, timeout_seconds=10)
            self.assertEqual(result.status, "failed")
            self.assertNotEqual(result.return_code, 0)
            self.assertIn("合成失败输出", result.output)
        finally:
            temporary.cleanup()

    def test_pytest_profile_uses_fixed_module_argv(self) -> None:
        temporary, workspace, store, _profile = self._project("basic")
        try:
            profile = store.add("pytest-profile", "pytest", ".", "tests")
            argv, cwd = _command_for(profile, workspace)
            self.assertEqual(
                argv,
                [sys.executable, "-m", "pytest", "-q", str(workspace.root / "tests")],
            )
            self.assertEqual(cwd, workspace.root)
        finally:
            temporary.cleanup()

    def test_timeout_terminates_ordinary_child_process_tree(self) -> None:
        temporary, workspace, _store, profile = self._project("child_tree")
        try:
            child_dir = Path(temporary.name) / "tests"
            result = execute_profile(profile, workspace, timeout_seconds=0.8)
            self.assertEqual(result.status, "timeout")
            marker = child_dir / "child.counter"
            self.assertTrue(marker.exists())
            before = marker.read_text(encoding="utf-8").count("tick")
            time.sleep(0.2)
            after = marker.read_text(encoding="utf-8").count("tick")
            self.assertEqual(after, before)
        finally:
            temporary.cleanup()

    def test_cancel_terminates_ordinary_child_process_tree(self) -> None:
        temporary, workspace, _store, profile = self._project("child_tree")
        try:
            event = threading.Event()
            results = []
            worker = threading.Thread(
                target=lambda: results.append(
                    execute_profile(profile, workspace, cancellation_event=event, timeout_seconds=10)
                ),
                daemon=True,
            )
            worker.start()
            child_dir = Path(temporary.name) / "tests"
            started = child_dir / "child.started"
            deadline = time.monotonic() + 4
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists(), "synthetic child never started")
            event.set()
            worker.join(timeout=4)
            self.assertFalse(worker.is_alive(), "runner did not finish after cancellation")
            self.assertEqual(results[0].status, "cancelled")
            marker = child_dir / "child.counter"
            before = marker.read_text(encoding="utf-8").count("tick")
            time.sleep(0.2)
            after = marker.read_text(encoding="utf-8").count("tick")
            self.assertEqual(after, before)
        finally:
            event.set()
            worker.join(timeout=4)
            temporary.cleanup()

    def test_windows_user_code_starts_only_after_job_assignment(self) -> None:
        temporary, workspace, _store, profile = self._project("windows_job")
        try:
            result = execute_profile(profile, workspace, timeout_seconds=10)
            self.assertEqual(result.status, "passed")
            if os.name == "nt":
                self.assertIn("OK", result.output)
        finally:
            temporary.cleanup()

    def test_tool_requires_exact_profile_approval_and_separate_output_consent(self) -> None:
        temporary, workspace, store, _profile = self._project("basic")
        try:
            approvals: list[ActionRequest] = []
            local_output: list[str] = []

            def approve(request: ActionRequest) -> bool:
                approvals.append(request)
                return request.operation == "run_test"

            call = ToolCall("t1", "run_test_profile", {"profile_id": "unit"})
            repeated_call = ToolCall("t2", "run_test_profile", {"profile_id": "unit"})

            class ScriptedProvider:
                label = "synthetic"

                def __init__(self) -> None:
                    self.requests = 0
                    self.tool_schemas = []
                    self.received_results: list[str] = []

                def complete(self, messages, tools, **kwargs):
                    del kwargs
                    self.tool_schemas = tools
                    self.requests += 1
                    if self.requests == 1:
                        return AssistantTurn("", tool_calls=[call])
                    if self.requests == 2:
                        return AssistantTurn("", tool_calls=[repeated_call])
                    self.received_results = [
                        item["content"] for item in messages if item.get("role") == "tool"
                    ]
                    return AssistantTurn("已检查。")

            scripted = ScriptedProvider()
            agent = Agent(
                scripted,
                workspace,
                approve_action=approve,
                test_profile_store=store,
                emit_output=local_output.append,
            )
            agent.ask("请运行 unit 测试。")
            test_schema = next(
                item for item in scripted.tool_schemas
                if item["function"]["name"] == "run_test_profile"
            )
            self.assertEqual(
                test_schema["function"]["parameters"]["properties"]["profile_id"]["enum"],
                ["unit"],
            )
            self.assertEqual([item.operation for item in approvals], ["run_test", "share_test_output"])
            self.assertIn("Python 解释器：", approvals[0].diff)
            self.assertIn('"-m", "unittest", "discover"', approvals[0].diff)
            self.assertIn("工作目录：", approvals[0].diff)
            self.assertIn("120 秒", approvals[0].diff)
            self.assertIn("32768 bytes", approvals[0].diff)
            self.assertIn("不是沙箱", approvals[0].diff)
            self.assertIn("16 KiB", approvals[1].diff)
            self.assertTrue(any("[已脱敏]" in item for item in local_output))
            self.assertFalse(any("synthetic-secret-value" in item for item in local_output))
            self.assertIn("用户选择仅在本地查看", scripted.received_results[0])
            self.assertNotIn("合成测试通过", scripted.received_results[0])
            self.assertNotIn("synthetic-secret-value", scripted.received_results[0])
            self.assertIn("未再次启动测试进程", scripted.received_results[1])

            displayed: list[str] = []
            prompts: list[str] = []

            def confirm_input(prompt: str) -> str:
                prompts.append(prompt)
                return "是"

            self.assertTrue(_confirm_action(approvals[0], input_fn=confirm_input, output=displayed.append))
            self.assertTrue(_confirm_action(approvals[1], input_fn=confirm_input, output=displayed.append))
            self.assertTrue(any("argv：" in item and "32768 bytes" in item for item in displayed))
            self.assertTrue(any("已脱敏输出发送给当前模型" in item for item in prompts))
            self.assertTrue(any("terminal=\\x1b" in item for item in displayed))
        finally:
            temporary.cleanup()

    def test_explicit_second_approval_shares_only_redacted_output(self) -> None:
        temporary, workspace, store, _profile = self._project("basic")
        try:
            approvals: list[ActionRequest] = []
            result = run_tool(
                workspace,
                ToolCall("t1", "run_test_profile", {"profile_id": "unit"}),
                approve_action=lambda request: approvals.append(request) or True,
                test_profile_store=store,
            )
            self.assertEqual([item.operation for item in approvals], ["run_test", "share_test_output"])
            self.assertIn("合成测试通过", result)
            self.assertIn("[已脱敏]", result)
            self.assertNotIn("synthetic-secret-value", result)
            self.assertIn("\\x1b", result)
        finally:
            temporary.cleanup()

    def test_openai_sse_tool_loop_accepts_only_dynamic_profile_id(self) -> None:
        temporary, workspace, store, _profile = self._project("basic")
        requests: list[dict[str, object]] = []
        authorization_headers: list[str | None] = []
        streamed = [
            [
                {
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "tool_calls": [{
                            "index": 0,
                            "id": "call_test",
                            "type": "function",
                            "function": {"name": "run_test_profile", "arguments": '{"profile_id":"un'},
                        }]},
                        "finish_reason": None,
                    }]
                },
                {
                    "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": [{
                            "index": 0,
                            "function": {"arguments": 'it"}'},
                        }]},
                        "finish_reason": "tool_calls",
                    }]
                },
            ],
            [
                {"choices": [{"index": 0, "delta": {"content": "合成 SSE 闭环完成"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ],
        ]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size).decode("utf-8"))
                requests.append(body)
                authorization_headers.append(self.headers.get("Authorization"))
                events = streamed.pop(0)
                wire = b"".join(
                    f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\r\n\r\n".encode("utf-8")
                    for event in events
                ) + b"data: [DONE]\r\n\r\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                for offset in range(0, len(wire), 11):
                    self.wfile.write(wire[offset : offset + 11])
                    self.wfile.flush()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        approvals: list[str] = []
        deltas: list[str] = []
        try:
            provider = OpenAICompatibleProvider(
                f"http://127.0.0.1:{server.server_port}",
                "",
                "synthetic-model",
                stream_timeout=5,
            )
            agent = Agent(
                provider,
                workspace,
                approve_action=lambda request: approvals.append(request.operation) or request.operation == "run_test",
                test_profile_store=store,
            )
            answer = agent.ask(
                "请运行 unit 测试。",
                on_text_delta=deltas.append,
            )
        finally:
            server.shutdown()
            worker.join(timeout=2)
            server.server_close()
            temporary.cleanup()

        schemas = requests[0]["tools"]
        runner_schema = next(item for item in schemas if item["function"]["name"] == "run_test_profile")
        self.assertEqual(
            runner_schema["function"]["parameters"]["properties"]["profile_id"]["enum"],
            ["unit"],
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(authorization_headers, [None, None])
        self.assertEqual(approvals, ["run_test", "share_test_output"])
        self.assertIn("合成 SSE 闭环完成", answer)
        self.assertEqual(deltas, ["合成 SSE 闭环完成"])

    def test_tool_rejects_unapproved_fields_and_plan_mode(self) -> None:
        temporary, workspace, store, _profile = self._project("basic")
        try:
            approvals: list[ActionRequest] = []
            with self.assertRaises(WorkspaceError):
                run_tool(
                    workspace,
                    ToolCall("t1", "run_test_profile", {"profile_id": "unit", "argv": ["-c", "x"]}),
                    approve_action=lambda request: approvals.append(request) or True,
                    test_profile_store=store,
                )
            self.assertEqual(approvals, [])
            result = run_tool(
                workspace,
                ToolCall("t2", "run_test_profile", {"profile_id": "unit"}),
                approve_action=lambda request: approvals.append(request) or True,
                test_profile_store=store,
                plan_only=True,
            )
            self.assertIn("计划模式", result)
            self.assertEqual(approvals, [])
        finally:
            temporary.cleanup()

    def test_declining_or_changing_profile_does_not_start_a_process(self) -> None:
        temporary, workspace, store, profile = self._project("basic")
        try:
            with patch("cvfx_harness.test_runner.execute_profile") as execute:
                denied = run_tool(
                    workspace,
                    ToolCall("t1", "run_test_profile", {"profile_id": profile.profile_id}),
                    approve_action=lambda _request: False,
                    test_profile_store=store,
                )
                changed = run_tool(
                    workspace,
                    ToolCall("t2", "run_test_profile", {"profile_id": profile.profile_id}),
                    approve_action=lambda request: store.remove(profile.profile_id) or True,
                    test_profile_store=store,
                )
            self.assertIn("拒绝", denied)
            self.assertIn("配置已更改", changed)
            execute.assert_not_called()
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
