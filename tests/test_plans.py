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
from cvfx_harness.plans import PlanDraft, PlanError, PlanStore, PlanStep, parse_plan_proposal
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
            from cvfx_harness.plans import PlanDraft, PlanSnapshot, PlanStep

            oversized = PlanDraft(tuple(
                PlanStep("😀" * 400, completion_criteria="😀" * 400)
                for _ in range(3)
            ))
            with self.assertRaisesRegex(PlanError, "8192 字节"):
                store.replace(oversized, expected_snapshot=PlanSnapshot(()))
            self.assertEqual(store.load().steps, ())

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

    def test_legacy_v1_plan_loads_with_safe_defaults_and_migrates_on_write(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            path = Path(temp) / ".harness" / "plans" / "current.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"version": 1, "steps": [{"text": "旧版步骤", "status": "pending"}]}),
                encoding="utf-8",
            )
            loaded = store.load()
            self.assertEqual(loaded.steps[0].completion_criteria, "由用户检查实际输出后确认完成。")
            self.assertEqual(loaded.steps[0].required_tools, ())
            store.transition(1, "in-progress", expected_step=loaded.steps[0])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)

    def test_accept_requires_review_snapshot_and_cannot_replace_running_step(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("既有步骤")
            reviewed = store.load()
            proposal = PlanDraft((PlanStep("新步骤", completion_criteria="输出列出结果"),))
            store.add("审阅期间外部修改")
            with self.assertRaisesRegex(PlanError, "已变化"):
                store.replace(proposal, expected_snapshot=reviewed)

            current = store.load()
            store.transition(1, "in-progress", expected_step=current.steps[0])
            active = store.load()
            with self.assertRaisesRegex(PlanError, "进行中的步骤"):
                store.replace(proposal, expected_snapshot=active)

    def test_structured_fields_survive_restart_while_in_progress_returns_to_pending(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            empty = store.load()
            proposal = PlanDraft((
                PlanStep(
                    "检查入口",
                    completion_criteria="输出模块路径",
                    required_tools=("list_directory",),
                ),
            ))
            store.replace(proposal, expected_snapshot=empty)
            saved = store.load()
            store.transition(1, "in-progress", expected_step=saved.steps[0])
            recovered = store.recover_unfinished()
            self.assertEqual(recovered.steps[0].status, "pending")
            self.assertEqual(recovered.steps[0].completion_criteria, "输出模块路径")
            self.assertEqual(recovered.steps[0].required_tools, ("list_directory",))


class PlanProposalProtocolTests(unittest.TestCase):
    VALID = json.dumps(
        {
            "steps": [
                {
                    "description": "读取中文配置",
                    "completion_criteria": "输出键名和含义，不包含密钥值",
                    "required_tools": ["read_file"],
                },
                {
                    "description": "列出测试入口",
                    "completion_criteria": "列出固定 profile ID",
                    "required_tools": ["list_directory", "run_test_profile"],
                },
            ]
        },
        ensure_ascii=False,
    )

    def test_valid_chinese_plan_has_strict_structure_and_bounded_tools(self) -> None:
        draft = parse_plan_proposal(self.VALID)
        self.assertEqual(len(draft.steps), 2)
        self.assertEqual(draft.steps[0].text, "读取中文配置")
        self.assertEqual(draft.steps[0].completion_criteria, "输出键名和含义，不包含密钥值")
        self.assertEqual(draft.steps[1].required_tools, ("list_directory", "run_test_profile"))

    def test_malformed_duplicate_unknown_control_secret_and_oversized_json_fail(self) -> None:
        invalid = [
            "{not-json}",
            '{"steps":[],"steps":[]}',
            '{"steps":[{"description":"步骤","completion_criteria":"条件","required_tools":[],"tool_call":"write_file"}]}',
            json.dumps({"steps": [{"description": "步骤\x1b[2J", "completion_criteria": "条件", "required_tools": []}]}),
            json.dumps({"steps": [{"description": "api_key=synthetic_secret_value_123", "completion_criteria": "条件", "required_tools": []}]}),
            json.dumps({"steps": [{"description": "步骤", "completion_criteria": "条件", "required_tools": ["shell"]}]}),
            json.dumps({"steps": [{"description": "步骤", "completion_criteria": "条件", "required_tools": []}]}) + " " * 8192,
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate[:32]), self.assertRaises(PlanError):
                parse_plan_proposal(candidate)


class PlanProposalCliTests(unittest.TestCase):
    class ProposalProvider:
        label = "offline-plan-protocol-fixture"
        model = "synthetic"

        def __init__(self, content: str, *, tool_calls: list[ToolCall] | None = None, callback=None) -> None:
            self.content = content
            self.tool_calls = tool_calls or []
            self.inject_message_tool_calls = False
            self.inject_native_tool_use = False
            self.callback = callback
            self.requests: list[tuple[list[dict[str, object]], list[dict[str, object]], int]] = []

        def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
            self.requests.append(([dict(message) for message in messages], list(tools), max_tokens))
            if self.callback is not None:
                callback, self.callback = self.callback, None
                callback()
            if cancellation_event is not None and cancellation_event.is_set():
                raise ProviderCancelled("cancelled")
            message = {"role": "assistant", "content": self.content}
            if self.inject_message_tool_calls:
                message["tool_calls"] = []
            if self.inject_native_tool_use:
                message["deepseek_anthropic_content"] = [
                    {"type": "tool_use", "id": "call-2", "name": "write_file", "input": {}}
                ]
            return AssistantTurn(
                self.content,
                self.tool_calls,
                message,
            )

    @staticmethod
    def _run(temp: str, provider, inputs: list[str]) -> list[str]:
        outputs: list[str] = []
        args = SimpleNamespace(workspace=temp, provider="mock", model=None)
        with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
            rc = run_console(args, input_fn=lambda _prompt: inputs.pop(0), output=outputs.append)
        if rc != 0:
            raise AssertionError(f"console returned {rc}: {outputs}")
        return outputs

    def test_generate_revise_and_accept_only_saves_reviewed_structured_steps(self) -> None:
        from cvfx_harness.plans import PlanSnapshot

        provider = self.ProposalProvider(PlanProposalProtocolTests.VALID)
        with local_temp() as temp:
            outputs = self._run(temp, provider, [
                "/计划 生成 检查中文配置",
                "/计划 修订 1 条件 输出所有非敏感键名",
                "/计划 修订 2 工具 write_file,run_test_profile",
                "/计划 接受", "/计划", "/退出",
            ])
            snapshot = PlanStore(Workspace(temp)).load()
            self.assertEqual(len(snapshot.steps), 2)
            self.assertEqual(snapshot.steps[0].completion_criteria, "输出所有非敏感键名")
            self.assertEqual(snapshot.steps[1].required_tools, ("write_file", "run_test_profile"))
            self.assertTrue(all(step.status == "pending" for step in snapshot.steps))
            self.assertTrue(all(not tools for _messages, tools, _limit in provider.requests))
            self.assertEqual(len(provider.requests), 1)
            self.assertLessEqual(provider.requests[0][2], 2048)
            self.assertTrue(any("未保存或执行" in line for line in outputs))
            self.assertTrue(any("已接受并保存" in line for line in outputs))
            saved = Path(temp) / ".harness" / "plans" / "current.json"
            payload = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(set(payload["steps"][0]), {"text", "completion_criteria", "required_tools", "status"})
            self.assertNotIn("模拟", saved.read_text(encoding="utf-8"))
            self.assertEqual(snapshot, PlanStore(Workspace(temp)).load())

    def test_reject_preserves_exact_prior_plan_bytes_and_does_not_save_candidate(self) -> None:
        provider = self.ProposalProvider(PlanProposalProtocolTests.VALID)
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("原有计划")
            path = Path(temp) / ".harness" / "plans" / "current.json"
            before = path.read_bytes()
            outputs = self._run(temp, provider, ["/计划 生成 新候选", "/计划 拒绝", "/退出"])
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("读取中文配置", path.read_text(encoding="utf-8"))
            self.assertTrue(any("计划存档未改变" in line for line in outputs))
            self.assertTrue(all(not tools for _messages, tools, _limit in provider.requests))

    def test_malformed_or_tool_bearing_provider_response_never_mutates_plan_store(self) -> None:
        contents = [PlanProposalProtocolTests.VALID + " " * 8192]
        for content in contents:
            with self.subTest(response="oversized"):
                with local_temp() as temp:
                    provider = self.ProposalProvider(content)
                    path = Path(temp) / ".harness" / "plans" / "current.json"
                    outputs = self._run(temp, provider, ["/计划 生成 合成", "/退出"])
                    self.assertFalse(path.exists())
                    self.assertTrue(any("计划存档未改变" in line for line in outputs))

        providers = [
            self.ProposalProvider(
                PlanProposalProtocolTests.VALID,
                tool_calls=[ToolCall("call-1", "write_file", {"path": "must-not-write", "content": "no"})],
            ),
            self.ProposalProvider(PlanProposalProtocolTests.VALID),
            self.ProposalProvider(PlanProposalProtocolTests.VALID),
        ]
        providers[1].inject_message_tool_calls = True
        providers[2].inject_native_tool_use = True
        for provider in providers:
            with local_temp() as temp:
                outputs = self._run(temp, provider, ["/计划 生成 合成", "/退出"])
                self.assertFalse((Path(temp) / "must-not-write").exists())
                self.assertFalse((Path(temp) / ".harness" / "plans" / "current.json").exists())
                self.assertTrue(any("包含工具调用" in line for line in outputs))
                self.assertTrue(all(not tools for _messages, tools, _limit in provider.requests))

    def test_review_snapshot_detects_plan_changed_during_model_request(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("当前计划")
            provider = self.ProposalProvider(
                PlanProposalProtocolTests.VALID,
                callback=lambda: store.add("请求期间并发添加"),
            )
            outputs = self._run(temp, provider, ["/计划 生成 新方案", "/计划 接受", "/退出"])
            self.assertEqual([step.text for step in store.load().steps], ["当前计划", "请求期间并发添加"])
            self.assertTrue(any("审阅期间当前计划已变化" in line for line in outputs))

    def test_proposal_cancellation_reserves_request_budget_and_low_budget_sends_nothing(self) -> None:
        import threading

        from cvfx_harness.agent import Agent

        class CancellingProvider:
            label = "offline-cancel-plan-fixture"
            model = "synthetic"

            def __init__(self, event: threading.Event) -> None:
                self.event = event
                self.calls = 0

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del messages, tools, cancellation_event, max_tokens
                self.calls += 1
                self.event.set()
                return AssistantTurn(PlanProposalProtocolTests.VALID)

        with local_temp() as temp:
            event = threading.Event()
            provider = CancellingProvider(event)
            agent = Agent(provider, Workspace(temp), approve_action=lambda _request: False)
            with self.assertRaises(ProviderCancelled):
                agent.propose_plan("合成需求", cancellation_event=event)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(agent.usage.requests, 1)
            self.assertEqual(agent.usage.estimated_output_tokens, 2048)
            self.assertEqual(PlanStore(Workspace(temp)).load().steps, ())

            constrained_provider = self.ProposalProvider(PlanProposalProtocolTests.VALID)
            constrained = Agent(
                constrained_provider,
                Workspace(temp),
                approve_action=lambda _request: False,
                token_budget=1024,
            )
            with self.assertRaisesRegex(PlanError, "预算不足"):
                constrained.propose_plan("超长预算测试需求" * 400)
            self.assertEqual(constrained_provider.requests, [])


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

    def test_each_step_uses_fresh_bounded_agent_and_keeps_tool_approval_per_call(self) -> None:
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
                if self.turn in {1, 3}:
                    call = ToolCall("write-1", "write_file", {"path": "target.txt", "content": "no"})
                    return AssistantTurn("", [call])
                return AssistantTurn("步骤检查完成。", assistant_message={"role": "assistant", "content": "步骤检查完成。"})

        with local_temp() as temp:
            provider = CapturingProvider()
            inputs = iter([
                "/计划 添加 检查第一步", "/计划 添加 第二步不应进入当前上下文",
                "/计划 执行 1", "是", "n", "/计划 取消 1", "/计划 重试 1",
                "/计划 执行 1", "是", "是",
                "/计划 完成 1", "/计划", "/退出",
            ])
            outputs: list[str] = []
            prompts: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=input_fn, output=outputs.append)

            self.assertEqual(rc, 0)
            target = Path(temp) / "target.txt"
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "no")
            self.assertEqual(len(provider.requests), 4)
            self.assertTrue(all(cap <= 256 for cap in provider.max_tokens))
            first_user = next(message["content"] for message in provider.requests[0] if message["role"] == "user")
            self.assertIn("检查第一步", first_user)
            self.assertNotIn("第二步不应进入", first_user)
            self.assertIn("写入", first_user)
            second_execution_user = next(message["content"] for message in provider.requests[2] if message["role"] == "user")
            self.assertIn("检查第一步", second_execution_user)
            self.assertNotIn("第二步不应进入", second_execution_user)
            combined = "\n".join(outputs)
            self.assertEqual(sum(prompt.startswith("确认写入") for prompt in prompts), 2)
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
