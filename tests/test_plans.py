from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.cli import run_console
from cvfx_harness.plans import PlanError, PlanStore
from cvfx_harness.providers import AssistantTurn, MockProvider, ProviderCancelled, ProviderError, ToolCall
from cvfx_harness.tools import Workspace


def local_temp() -> tempfile.TemporaryDirectory:
    base = Path(__file__).resolve().parents[1] / ".test-tmp"
    base.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base, prefix="plan-中文-")


class PlanStoreTests(unittest.TestCase):
    def test_user_authored_steps_are_bounded_persisted_and_recovered(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("检查中文路径，不修改文件")
            store.add("确认配置项说明")
            store.transition(1, "in-progress")

            path = Path(temp) / ".harness" / "plans" / "current.json"
            raw = path.read_text(encoding="utf-8")
            self.assertIn("检查中文路径", raw)
            for forbidden in ("tool result", "approval", "response", "output", "history"):
                self.assertNotIn(forbidden, raw)
            self.assertEqual(
                store.load().steps[0].status,
                "in-progress",
            )

            recovered = PlanStore(Workspace(temp)).recover_unfinished()
            self.assertEqual([step.status for step in recovered.steps], ["pending", "pending"])
            self.assertEqual(
                [step.status for step in store.load().steps],
                ["pending", "pending"],
            )

    def test_step_count_text_and_secret_limits_are_enforced(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            for bad in ("", "api_key=long_secret_value_123", "有\x1b隐藏控制码", "中" * 401):
                with self.subTest(bad=bad[:20]), self.assertRaises(PlanError):
                    store.add(bad)
            store.add("一")
            store.add("二")
            store.add("三")
            with self.assertRaisesRegex(PlanError, "最多包含"):
                store.add("四")

    def test_total_utf8_plan_size_is_bounded_without_partial_save(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("😀" * 400)
            store.add("😀" * 400)
            with self.assertRaisesRegex(PlanError, "4096 字节"):
                store.add("😀" * 400)
            self.assertEqual(len(store.load().steps), 2)

    def test_unknown_persisted_fields_and_invalid_transitions_are_rejected(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("只读检查")
            path = Path(temp) / ".harness" / "plans" / "current.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["steps"][0]["tool_results"] = ["MUST_NOT_BECOME_AUTHORIZATION"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(PlanError):
                store.load()

            path.write_text(json.dumps({"version": 1, "steps": [{"text": "x", "status": "pending"}]}), encoding="utf-8")
            with self.assertRaisesRegex(PlanError, "不允许"):
                store.transition(1, "completed")

    def test_confirmed_text_is_rechecked_before_transition_or_clear(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("先展示给用户确认的步骤")
            preview = store.load()
            path = Path(temp) / ".harness" / "plans" / "current.json"
            path.write_text(
                json.dumps({"version": 1, "steps": [{"text": "外部替换的步骤", "status": "pending"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PlanError, "已变化"):
                store.transition(
                    1,
                    "in-progress",
                    expected_text=preview.steps[0].text,
                    expected_status="pending",
                )
            with self.assertRaisesRegex(PlanError, "已变化"):
                store.clear(expected_snapshot=preview)
            self.assertEqual(store.load().steps[0].text, "外部替换的步骤")


class PlanCliTests(unittest.TestCase):
    def test_interrupted_saved_step_returns_to_pending_and_needs_confirmation(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("只读的恢复测试步骤")
            store.transition(1, "in-progress")

            class NoRequestProvider:
                label = "offline-recovery-test"
                model = "synthetic"

                def complete(self, *args, **kwargs):
                    del args, kwargs
                    raise AssertionError("declined resumed task must not contact provider")

            inputs = iter(["/计划", "/计划 执行 1", "n", "/退出"])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=NoRequestProvider()):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

            self.assertEqual(rc, 0)
            self.assertTrue(any("退回 pending" in line for line in outputs))
            self.assertIn("模型请求未发出", "\n".join(outputs))
            self.assertEqual(PlanStore(Workspace(temp)).load().steps[0].status, "pending")

    def test_provider_failure_is_blocked_redacted_and_retryable(self) -> None:
        class FailingProvider:
            label = "offline-failure-test"
            model = "synthetic"

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del messages, tools, cancellation_event, max_tokens
                raise ProviderError("PRIVATE_PROVIDER_ERROR_SENTINEL")

        with local_temp() as temp:
            inputs = iter([
                "/计划 添加 会失败的离线请求", "/计划 执行 1", "是",
                "/计划", "/计划 重试 1", "/计划", "/退出",
            ])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=FailingProvider()):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

            self.assertEqual(rc, 0)
            combined = "\n".join(outputs)
            self.assertIn("[blocked]", combined)
            self.assertIn("[pending]", combined)
            self.assertNotIn("PRIVATE_PROVIDER_ERROR_SENTINEL", combined)
            self.assertEqual(PlanStore(Workspace(temp)).load().steps[0].status, "pending")

    def test_provider_cancellation_persists_cancelled(self) -> None:
        class CancelProvider:
            label = "offline-cancel-test"
            model = "synthetic"

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del messages, tools, cancellation_event, max_tokens
                raise ProviderCancelled("cancelled")

        with local_temp() as temp:
            inputs = iter(["/计划 添加 可取消步骤", "/计划 执行 1", "是", "/退出"])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=CancelProvider()):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

            self.assertEqual(rc, 0)
            self.assertIn("状态为 cancelled", "\n".join(outputs))
            self.assertEqual(PlanStore(Workspace(temp)).load().steps[0].status, "cancelled")

    def test_each_step_uses_fresh_read_only_bounded_agent_and_requires_review(self) -> None:
        class CapturingProvider:
            label = "offline-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests: list[list[dict[str, object]]] = []
                self.max_tokens: list[int] = []
                self.turn = 0

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del cancellation_event
                self.requests.append([dict(message) for message in messages])
                self.max_tokens.append(max_tokens)
                self.turn += 1
                if self.turn == 1:
                    call = ToolCall("write-1", "write_file", {"path": "target.txt", "content": "no"})
                    return AssistantTurn("", [call])
                return AssistantTurn("步骤检查完成。", assistant_message={"role": "assistant", "content": "步骤检查完成。"})

        with local_temp() as temp:
            provider = CapturingProvider()
            inputs = iter([
                "/计划 添加 检查第一步", "/计划 添加 第二步不应进入当前上下文",
                "/计划 执行 1", "是", "/计划 重试 1", "/计划 执行 1", "是",
                "/计划 完成 1", "/计划", "/退出",
            ])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

            self.assertEqual(rc, 0)
            target = Path(temp) / "target.txt"
            self.assertFalse(target.exists())
            self.assertEqual(len(provider.requests), 3)
            self.assertLessEqual(len(provider.requests), 4)
            self.assertTrue(all(cap <= 256 for cap in provider.max_tokens))
            first_user = next(message["content"] for message in provider.requests[0] if message["role"] == "user")
            self.assertIn("检查第一步", first_user)
            self.assertNotIn("第二步不应进入", first_user)
            self.assertIn("计划模式：已在工具层拦截写入", provider.requests[1][-1]["content"])
            second_execution_user = next(message["content"] for message in provider.requests[2] if message["role"] == "user")
            self.assertIn("检查第一步", second_execution_user)
            self.assertNotIn("第二步不应进入", second_execution_user)
            combined = "\n".join(outputs)
            self.assertIn("Harness 已在工具层拦截", combined)
            self.assertIn("[completed]", combined)
            self.assertIn("步骤检查完成。", combined)

    def test_plan_step_retains_read_approval_and_excludes_session_tool_history(self) -> None:
        class ReadProvider:
            label = "offline-read-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests: list[list[dict[str, object]]] = []
                self.turn = 0

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del tools, cancellation_event, max_tokens
                self.requests.append([dict(message) for message in messages])
                self.turn += 1
                if self.turn in {1, 3}:
                    return AssistantTurn("", [ToolCall("read-1", "read_file", {"path": "notes.txt"})])
                return AssistantTurn("已按步骤检查。", assistant_message={"role": "assistant", "content": "已按步骤检查。"})

        with local_temp() as temp:
            Path(temp, "notes.txt").write_text("SYNTHETIC_PRIVATE_NOTE", encoding="utf-8")
            provider = ReadProvider()
            inputs = iter([
                "请先读取 notes.txt", "是",
                "/目标 只检查用户指定步骤 api_key=private_secret_value123",
                "/计划 添加 检查 notes.txt", "/计划 执行 1", "是", "是", "/计划 取消 1", "/退出",
            ])
            prompts: list[str] = []
            outputs: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=input_fn, output=outputs.append)

            self.assertEqual(rc, 0)
            self.assertTrue(any("确认开始此步骤" in prompt for prompt in prompts))
            self.assertEqual(sum("确认读取" in prompt for prompt in prompts), 2)
            self.assertIn("SYNTHETIC_PRIVATE_NOTE", provider.requests[1][-1]["content"])
            self.assertEqual(
                [message["role"] for message in provider.requests[0]],
                ["system", "user"],
            )
            self.assertEqual(
                [message["role"] for message in provider.requests[1]][:2],
                ["system", "user"],
            )
            self.assertEqual([message["role"] for message in provider.requests[2]], ["system", "user"])
            self.assertNotIn("SYNTHETIC_PRIVATE_NOTE", json.dumps(provider.requests[2], ensure_ascii=False))
            self.assertIn("只检查用户指定步骤", provider.requests[2][1]["content"])
            self.assertNotIn("private_secret_value123", json.dumps(provider.requests[2], ensure_ascii=False))
            self.assertIn("[已脱敏]", provider.requests[2][1]["content"])
            self.assertIn("SYNTHETIC_PRIVATE_NOTE", provider.requests[3][-1]["content"])
            self.assertEqual(
                [step.status for step in PlanStore(Workspace(temp)).load().steps],
                ["cancelled"],
            )

    def test_declined_step_confirmation_makes_no_provider_request(self) -> None:
        provider = MockProvider()
        with local_temp() as temp:
            inputs = iter([
                "/计划 添加 安全确认步骤", "/计划 执行 1", "n", "/计划", "/退出",
            ])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)
            self.assertEqual(rc, 0)
            self.assertIn("模型请求未发出", "\n".join(outputs))
            saved = PlanStore(Workspace(temp)).load()
            self.assertEqual(saved.steps[0].status, "pending")


if __name__ == "__main__":
    unittest.main()
