from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.cli import _plan_progress_text, run_console
from cvfx_harness.plans import PlanDraft, PlanError, PlanProgress, PlanSnapshot, PlanStore, PlanStep, parse_plan_proposal
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
            self.assertEqual([step.status for step in recovered.steps], ["blocked", "pending"])
            self.assertEqual(recovered.progress.last_result, "outcome_unknown")
            self.assertEqual(recovered.progress.interruption_cause, "legacy_state_unknown")
            self.assertEqual(
                [step.status for step in store.load().steps],
                ["blocked", "pending"],
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
            self.assertEqual(loaded.unverified_steps, (1,))
            with self.assertRaisesRegex(PlanError, "未经核验"):
                store.replace(
                    PlanDraft((PlanStep("新计划", completion_criteria="检查输出"),)),
                    expected_snapshot=loaded,
                )
            with self.assertRaisesRegex(PlanError, "需核验"):
                store.transition(1, "in-progress", expected_step=loaded.steps[0])
            recovered = store.recover_unfinished()
            self.assertEqual(recovered.steps[0].status, "blocked")
            self.assertEqual(recovered.progress.last_result, "outcome_unknown")
            resumed = store.prepare_resume(
                1, expected_snapshot=recovered, verified_unknown_outcome=True
            )
            self.assertEqual(resumed.steps[0].status, "pending")
            self.assertEqual(resumed.unverified_steps, ())
            self.assertEqual(resumed.progress.last_result, "user_verified")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 3)

    def test_legacy_v2_all_unfinished_steps_require_individual_verification(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            path = Path(temp) / ".harness" / "plans" / "current.json"
            path.parent.mkdir(parents=True)
            legacy_steps = [
                {
                    "text": f"旧步骤 {index}",
                    "status": "pending",
                    "completion_criteria": "检查输出",
                    "required_tools": [],
                }
                for index in range(1, 4)
            ]
            path.write_text(json.dumps({"version": 2, "steps": legacy_steps}), encoding="utf-8")

            recovered = store.recover_unfinished()
            self.assertEqual(recovered.unverified_steps, (1, 2, 3))
            self.assertTrue(all(step.status == "blocked" for step in recovered.steps))
            with self.assertRaisesRegex(PlanError, "旧版第 1 步"):
                store.transition(2, "pending")
            with self.assertRaisesRegex(PlanError, "第 1 步仍有未知工具结果"):
                store.prepare_resume(2, expected_snapshot=recovered, verified_unknown_outcome=True)

            first = store.prepare_resume(1, expected_snapshot=recovered, verified_unknown_outcome=True)
            self.assertEqual(first.steps[0].status, "blocked")
            self.assertEqual(first.unverified_steps, (2, 3))
            restarted = store.recover_unfinished()
            self.assertEqual(restarted.unverified_steps, (2, 3))
            self.assertEqual(restarted.progress.step_index, 2)
            self.assertEqual(restarted.progress.last_result, "outcome_unknown")
            with self.assertRaisesRegex(PlanError, "旧版第 2 步"):
                store.transition(1, "pending")

            second = store.prepare_resume(2, expected_snapshot=restarted, verified_unknown_outcome=True)
            self.assertEqual(second.unverified_steps, (3,))
            final = store.prepare_resume(3, expected_snapshot=second, verified_unknown_outcome=True)
            self.assertEqual(final.steps[2].status, "pending")
            self.assertEqual(final.unverified_steps, ())
            self.assertEqual(final.progress.last_result, "user_verified")
            self.assertTrue(all(step.status == "blocked" for step in final.steps[:2]))

    def test_unverified_legacy_steps_do_not_hide_a_separate_unknown_milestone(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            mixed = PlanSnapshot(
                (
                    PlanStep("新格式步骤", "blocked"),
                    PlanStep("遗留步骤", "blocked"),
                ),
                PlanProgress(1, "tool_execution", "write_file", "outcome_unknown", "process_restarted"),
                (2,),
            )
            store._write(mixed)

            recovered = store.recover_unfinished()
            self.assertEqual(recovered.progress.step_index, 1)
            self.assertEqual(recovered.unverified_steps, (2,))
            with self.assertRaisesRegex(PlanError, "第 1 步仍有未知工具结果"):
                store.prepare_resume(2, expected_snapshot=recovered, verified_unknown_outcome=True)

            first = store.prepare_resume(1, expected_snapshot=recovered, verified_unknown_outcome=True)
            self.assertEqual(first.steps[0].status, "blocked")
            self.assertEqual(first.unverified_steps, (2,))
            final = store.prepare_resume(2, expected_snapshot=first, verified_unknown_outcome=True)
            self.assertEqual(final.steps[1].status, "pending")
            self.assertEqual(final.unverified_steps, ())

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

    def test_structured_fields_survive_restart_and_legacy_active_state_is_blocked_unknown(self) -> None:
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
            self.assertEqual(recovered.steps[0].status, "blocked")
            self.assertEqual(recovered.progress.last_result, "outcome_unknown")
            self.assertEqual(recovered.progress.interruption_cause, "legacy_state_unknown")
            self.assertEqual(recovered.steps[0].completion_criteria, "输出模块路径")
            self.assertEqual(recovered.steps[0].required_tools, ("list_directory",))

    def test_pending_approval_restart_is_known_not_executed(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("读取合成文件")
            store.transition(1, "in-progress")
            store.record_progress(1, "tool_approval", "read_file")

            recovered = PlanStore(Workspace(temp)).recover_unfinished()
            self.assertEqual(recovered.steps[0].status, "blocked")
            self.assertEqual(recovered.progress.phase, "tool_approval")
            self.assertEqual(recovered.progress.last_result, "not_executed")
            resumed = store.prepare_resume(1, expected_snapshot=recovered)
            self.assertEqual(resumed.steps[0].status, "pending")

    def test_interrupted_questionnaire_only_records_local_interaction_not_answers(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("向用户询问合成偏好")
            store.transition(1, "in-progress")
            store.record_progress(1, "tool_interaction", "ask_user")
            path = Path(temp) / ".harness" / "plans" / "current.json"
            before_restart = path.read_text(encoding="utf-8")
            self.assertNotIn("SYNTHETIC_QUESTION_ANSWER", before_restart)

            recovered = store.recover_unfinished()
            self.assertEqual(recovered.progress.phase, "local_interaction")
            self.assertEqual(recovered.progress.last_tool, "ask_user")
            self.assertEqual(recovered.progress.last_result, "not_executed")

    def test_execution_restart_requires_explicit_verification_and_never_direct_retry(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("检查合成步骤")
            store.add("其他步骤")
            store.transition(1, "in-progress")
            store.record_progress(1, "tool_execution", "write_file")

            recovered = store.recover_unfinished()
            self.assertEqual(recovered.progress.last_result, "outcome_unknown")
            with self.assertRaisesRegex(PlanError, "结果未知"):
                store.prepare_resume(1, expected_snapshot=recovered)
            with self.assertRaisesRegex(PlanError, "结果未知"):
                store.transition(1, "pending")
            with self.assertRaisesRegex(PlanError, "第 1 步仍有未知工具结果"):
                store.transition(2, "in-progress")

            resumed = store.prepare_resume(
                1, expected_snapshot=recovered, verified_unknown_outcome=True
            )
            self.assertEqual(resumed.steps[0].status, "pending")
            self.assertEqual(resumed.progress.last_result, "user_verified")
            store.transition(1, "in-progress")
            resumed_progress = store.record_progress(1, "provider_request")
            self.assertEqual(resumed_progress.progress.interruption_cause, "none")

    def test_returned_tool_result_is_only_a_fixed_redacted_milestone(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("检查中文工具结果")
            store.transition(1, "in-progress")
            store.record_progress(1, "tool_result", "read_file")
            path = Path(temp) / ".harness" / "plans" / "current.json"
            raw = path.read_text(encoding="utf-8")
            self.assertIn('"last_result":"returned_redacted"', raw)
            for forbidden in (
                "synthetic_secret_value",
                "PRIVATE_FILE_CONTENT",
                "private/path.txt",
                "api_key=",
                "arguments",
                "tool_output",
            ):
                self.assertNotIn(forbidden, raw)

            recovered = store.recover_unfinished()
            self.assertEqual(recovered.progress.phase, "tool_result_returned")
            self.assertEqual(recovered.progress.last_result, "returned_redacted")
            self.assertEqual(recovered.progress.last_tool, "read_file")

    def test_progress_schema_rejects_arbitrary_content_and_invalid_tool_names(self) -> None:
        with self.assertRaises(PlanError):
            PlanStore._validate_snapshot({
                "version": 3,
                "steps": [{
                    "text": "检查计划",
                    "completion_criteria": "检查实际结果",
                    "required_tools": [],
                    "status": "blocked",
                }],
                "progress": {
                    "step_index": 1,
                    "phase": "tool_result_returned",
                    "last_tool": "read_file",
                    "last_result": "PRIVATE_FILE_CONTENT",
                    "interruption_cause": "process_restarted",
                },
                "unverified_steps": [],
            })
        with self.assertRaisesRegex(PlanError, "待核验步骤索引"):
            PlanStore._validate_snapshot({
                "version": 3,
                "steps": [{
                    "text": "检查计划",
                    "completion_criteria": "检查实际结果",
                    "required_tools": [],
                    "status": "blocked",
                }],
                "progress": None,
                "unverified_steps": [True],
            })
        with self.assertRaises(PlanError):
            PlanStore._validate_snapshot({
                "version": 3,
                "steps": [{
                    "text": "检查计划",
                    "completion_criteria": "检查实际结果",
                    "required_tools": [],
                    "status": "blocked",
                }],
                "progress": {
                    "step_index": 1,
                    "phase": "tool_result_returned",
                    "last_tool": "read_file /private/path",
                    "last_result": "returned_redacted",
                    "interruption_cause": "process_restarted",
                },
                "unverified_steps": [],
            })

    def test_questionnaire_progress_says_answer_was_not_archived(self) -> None:
        text = _plan_progress_text(
            PlanProgress(1, "local_interaction", "ask_user", "approval_pending", "cancelled")
        )
        self.assertIn("问卷回答未存档", text)
        self.assertIn("可能需要重新回答", text)
        self.assertNotIn("工具尚未获批", text)


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
            self.assertEqual(payload["version"], 3)
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
    @staticmethod
    def _make_restart_blocked_step(temp: str, text: str = "检查合成文件") -> PlanStore:
        store = PlanStore(Workspace(temp))
        store.add(text)
        store.transition(1, "in-progress")
        store.record_progress(1, "provider_request")
        store.recover_unfinished()
        return store

    def test_cold_resume_reviews_saved_step_and_requires_limited_context_confirmation(self) -> None:
        class CapturingProvider:
            label = "offline-cold-resume-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests: list[list[dict[str, object]]] = []

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del tools, cancellation_event, max_tokens
                self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
                return AssistantTurn("已按有限步骤上下文完成合成检查。")

        with local_temp() as temp:
            store = self._make_restart_blocked_step(temp, "检查保存的入口模块")
            provider = CapturingProvider()
            inputs = iter(["/继续 1", "", "是", "/退出"])
            prompts: list[str] = []
            outputs: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(
                    SimpleNamespace(workspace=temp, provider="mock", model=None),
                    input_fn=input_fn,
                    output=outputs.append,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(len(provider.requests), 1)
            user_text = next(
                message["content"]
                for message in provider.requests[0]
                if message["role"] == "user"
            )
            self.assertIn("检查保存的入口模块", user_text)
            self.assertIn("检查保存的入口模块", "\n".join(outputs))
            review = "\n".join(outputs)
            self.assertIn("可用上下文：仅已保存步骤说明", review)
            self.assertIn("旧目标、聊天、问卷答案和工具结果不恢复", review)
            self.assertIn("续跑将启动空白 Agent；不重放旧工具调用或审批", review)
            self.assertEqual(review.count("续跑将启动空白 Agent"), 1)
            self.assertTrue(any("补充本次续跑需要的目标/约束" in prompt for prompt in prompts))
            self.assertIn("确认继续？[y/N] ", prompts)
            self.assertEqual(store.load().steps[0].status, "in-progress")

    def test_process_restart_shows_step_details_once_during_resume_review(self) -> None:
        class CapturingProvider:
            label = "offline-restarted-plan-review-test"
            model = "synthetic"

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del messages, tools, cancellation_event, max_tokens
                return AssistantTurn("已按有限步骤完成合成检查。")

        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            step_text = "冷启动后仅复核一次的合成入口步骤"
            store.add(step_text)
            store.transition(1, "in-progress")
            store.record_progress(1, "provider_request")
            prompts: list[str] = []
            outputs: list[str] = []
            inputs = iter(["/继续 1", "", "是", "/退出"])

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            with patch(
                "cvfx_harness.cli.provider_from_environment",
                return_value=CapturingProvider(),
            ):
                rc = run_console(
                    SimpleNamespace(workspace=temp, provider="mock", model=None),
                    input_fn=input_fn,
                    output=outputs.append,
                )

            transcript = "\n".join(outputs)
            review = next(item for item in outputs if item.startswith("继续前复核："))
            self.assertEqual(rc, 0)
            self.assertEqual(review.count(step_text), 1)
            self.assertEqual(review.count("续跑将启动空白 Agent"), 1)
            self.assertIn("检测到上次进程中断，计划步骤已受阻", transcript)
            self.assertIn("可用上下文：仅已保存步骤说明", review)
            self.assertIn("旧目标、聊天、问卷答案和工具结果不恢复", review)
            self.assertNotIn("计划步骤已受阻；不会自动续跑。下面只显示", transcript)
            self.assertIn("确认继续？[y/N] ", prompts)
            self.assertEqual(store.load().steps[0].status, "in-progress")

    def test_resume_supplement_is_ephemeral_and_not_saved_in_plan_or_session(self) -> None:
        class CapturingProvider:
            label = "offline-private-resume-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests: list[list[dict[str, object]]] = []

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del tools, cancellation_event, max_tokens
                self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
                return AssistantTurn("本次临时补充只用于合成步骤。")

        with local_temp() as temp:
            store = self._make_restart_blocked_step(temp, "核对临时补充的接口")
            provider = CapturingProvider()
            supplement = "SYNTHETIC_RESUME_CONTEXT_仅本次使用"
            inputs = iter([
                "/继续 1", supplement, "是", "/保存 resume-private", "/退出",
            ])
            prompts: list[str] = []
            outputs: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(
                    SimpleNamespace(workspace=temp, provider="mock", model=None),
                    input_fn=input_fn,
                    output=outputs.append,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(len(provider.requests), 1)
            self.assertIn(supplement, json.dumps(provider.requests[0], ensure_ascii=False))
            self.assertIn("已接收本次临时目标/约束", "\n".join(outputs))
            self.assertIn("确认继续？[y/N] ", prompts)
            self.assertIn("不会写入计划存档或主会话目标", "\n".join(outputs))
            plan_path = Path(temp) / ".harness" / "plans" / "current.json"
            archive_path = Path(temp) / ".harness" / "sessions" / "resume-private.json"
            self.assertNotIn(supplement, plan_path.read_text(encoding="utf-8"))
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
            self.assertNotIn(supplement, json.dumps(archive, ensure_ascii=False))
            self.assertEqual(archive["goal"], "")
            self.assertEqual(store.load().steps[0].status, "in-progress")

    def test_existing_current_goal_is_reused_without_cold_start_prompt(self) -> None:
        class CapturingProvider:
            label = "offline-existing-goal-resume-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests: list[list[dict[str, object]]] = []

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del tools, cancellation_event, max_tokens
                self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
                return AssistantTurn("沿用当前目标完成合成检查。")

        with local_temp() as temp:
            self._make_restart_blocked_step(temp, "在现有目标内检查配置")
            provider = CapturingProvider()
            goal = "保持中文接口，只检查配置边界"
            inputs = iter([f"/目标 {goal}", "/继续 1", "是", "/退出"])
            prompts: list[str] = []
            outputs: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(
                    SimpleNamespace(workspace=temp, provider="mock", model=None),
                    input_fn=input_fn,
                    output=outputs.append,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(len(provider.requests), 1)
            self.assertIn(goal, json.dumps(provider.requests[0], ensure_ascii=False))
            self.assertFalse(any("补充本次续跑需要的目标/约束" in prompt for prompt in prompts))
            review = "\n".join(outputs)
            self.assertIn("当前进程目标作为不可信内容提供", review)
            self.assertIn("确认继续？[y/N] ", prompts)
            self.assertFalse(any("当前进程目标为空" in line for line in outputs))

    def test_cancelling_resume_context_input_keeps_blocked_step_and_sends_nothing(self) -> None:
        class NoRequestProvider:
            label = "offline-cancel-resume-context-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.calls = 0

            def complete(self, *args, **kwargs):
                del args, kwargs
                self.calls += 1
                raise AssertionError("cancelled context review must not request the provider")

        with local_temp() as temp:
            store = self._make_restart_blocked_step(temp, "取消时保持受阻")
            provider = NoRequestProvider()
            inputs = iter(["/继续 1", "/退出"])
            outputs: list[str] = []
            prompts: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                if "补充本次续跑需要的目标/约束" in prompt:
                    raise KeyboardInterrupt
                return next(inputs)

            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(
                    SimpleNamespace(workspace=temp, provider="mock", model=None),
                    input_fn=input_fn,
                    output=outputs.append,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(provider.calls, 0)
            self.assertIn("续跑上下文输入已取消", "\n".join(outputs))
            self.assertEqual(store.load().steps[0].status, "blocked")

    def test_resume_supplement_rejects_secret_control_and_oversize_without_echo_or_request(self) -> None:
        class NoRequestProvider:
            label = "offline-secret-resume-context-test"
            model = "synthetic"

            def complete(self, *args, **kwargs):
                del args, kwargs
                raise AssertionError("rejected resume context must not reach the provider")

        invalid_inputs = (
            ("api_key=synthetic-secret-value", "疑似凭据或控制字符"),
            ("first\u2028second", "疑似凭据或控制字符"),
            ("x" * 4097, "超过 4 KiB 上限"),
        )
        for value, message in invalid_inputs:
            with self.subTest(message=message), local_temp() as temp:
                store = self._make_restart_blocked_step(temp, "拒绝无效续跑补充")
                inputs = iter(["/继续 1", value, "/退出"])
                outputs: list[str] = []

                with patch(
                    "cvfx_harness.cli.provider_from_environment",
                    return_value=NoRequestProvider(),
                ):
                    rc = run_console(
                        SimpleNamespace(workspace=temp, provider="mock", model=None),
                        input_fn=lambda _prompt: next(inputs),
                        output=outputs.append,
                    )

                transcript = "\n".join(outputs)
                self.assertEqual(rc, 0)
                self.assertIn(message, transcript)
                self.assertNotIn(value, transcript)
                self.assertEqual(store.load().steps[0].status, "blocked")

    def test_legacy_migration_resume_also_requires_context_choice(self) -> None:
        class CapturingProvider:
            label = "offline-legacy-context-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests: list[list[dict[str, object]]] = []

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del tools, cancellation_event, max_tokens
                self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
                return AssistantTurn("旧版步骤已从有限上下文开始重新检查。")

        with local_temp() as temp:
            path = Path(temp) / ".harness" / "plans" / "current.json"
            path.parent.mkdir(parents=True)
            legacy = {
                "version": 2,
                "steps": [{
                    "text": "旧存档中的合成步骤",
                    "status": "pending",
                    "completion_criteria": "按当前工作区检查合成结果",
                    "required_tools": [],
                }],
            }
            path.write_bytes(json.dumps(legacy, ensure_ascii=False).encode("utf-8"))
            provider = CapturingProvider()
            inputs = iter(["/继续 核验 1", "是", "", "是", "/退出"])
            prompts: list[str] = []
            outputs: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(
                    SimpleNamespace(workspace=temp, provider="mock", model=None),
                    input_fn=input_fn,
                    output=outputs.append,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(len(provider.requests), 1)
            self.assertIn("旧版计划没有执行阶段记录", "\n".join(outputs))
            self.assertIn("可用上下文：仅已保存步骤说明", "\n".join(outputs))
            self.assertIn("确认继续？[y/N] ", prompts)
            self.assertIn("旧存档中的合成步骤", json.dumps(provider.requests[0], ensure_ascii=False))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 3)

    def test_legacy_plan_verifies_each_step_without_starting_agent_early(self) -> None:
        class FinalProvider:
            label = "offline-legacy-plan-resume-test"
            model = "synthetic"

            def __init__(self, workspace: str) -> None:
                self.workspace = workspace
                self.calls = 0

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del messages, tools, cancellation_event, max_tokens
                self.calls += 1
                plan = PlanStore(Workspace(self.workspace)).load()
                if self.calls == 1:
                    if plan.unverified_steps != ():
                        raise AssertionError("legacy steps must all be verified before any provider request")
                    return AssistantTurn("已从空白 Agent 开始核验最后一步。")
                raise AssertionError("the resume fixture must use one provider request")

        with local_temp() as temp:
            path = Path(temp) / ".harness" / "plans" / "current.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "version": 2,
                "steps": [
                    {
                        "text": f"旧版合成步骤 {index}",
                        "status": "pending",
                        "completion_criteria": "检查合成结果",
                        "required_tools": [],
                    }
                    for index in (1, 2)
                ],
            }), encoding="utf-8")
            provider = FinalProvider(temp)
            inputs = iter([
                "/继续 核验 1", "是",
                "/继续 核验 2", "是", "", "是",
                "/退出",
            ])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

            current = PlanStore(Workspace(temp)).load()
            self.assertEqual(rc, 0)
            self.assertEqual(provider.calls, 1)
            self.assertIn("全部核验前不会启动 Agent", "\n".join(outputs))
            self.assertIn("尚未启动模型", "\n".join(outputs))
            self.assertEqual(current.unverified_steps, ())
            self.assertEqual(current.steps[0].status, "blocked")
            self.assertEqual(current.steps[1].status, "in-progress")

    def test_interrupted_saved_step_blocks_and_resume_decline_makes_no_provider_request(self) -> None:
        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("只读的恢复测试步骤")
            store.transition(1, "in-progress")
            store.record_progress(1, "provider_request")

            class NoRequestProvider:
                label = "offline-recovery-test"
                model = "synthetic"

                def complete(self, *args, **kwargs):
                    del args, kwargs
                    raise AssertionError("declined resumed task must not contact provider")

            inputs = iter(["/计划", "/继续 1", "", "n", "/退出"])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=NoRequestProvider()):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

            self.assertEqual(rc, 0)
            self.assertTrue(any("不会自动续跑" in line for line in outputs))
            self.assertIn("尚未确认继续", "\n".join(outputs))
            self.assertEqual(PlanStore(Workspace(temp)).load().steps[0].status, "blocked")

    def test_unknown_tool_outcome_cannot_continue_without_explicit_verification(self) -> None:
        class NoRequestProvider:
            label = "offline-unknown-resume-test"
            model = "synthetic"

            def complete(self, *args, **kwargs):
                del args, kwargs
                raise AssertionError("unknown tool call must not be replayed before verification")

        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("检查可能已写入的合成文件")
            store.transition(1, "in-progress")
            store.record_progress(1, "tool_execution", "write_file")
            recovered = store.recover_unfinished()
            self.assertEqual(recovered.steps[0].status, "blocked")

            inputs = iter(["/继续 1", "/计划", "/继续 核验 1", "n", "/退出"])
            prompts: list[str] = []
            outputs: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=NoRequestProvider()):
                rc = run_console(args, input_fn=input_fn, output=outputs.append)

            self.assertEqual(rc, 0)
            self.assertIn("上次工具可能已执行，但结果未知", "\n".join(outputs))
            self.assertTrue(any("确认已核验" in prompt for prompt in prompts))
            self.assertEqual(store.load().steps[0].status, "blocked")
            self.assertEqual(store.load().progress.last_result, "outcome_unknown")

    def test_verified_resume_starts_fresh_agent_only_after_explicit_confirmation(self) -> None:
        class FinalProvider:
            label = "offline-verified-resume-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.requests: list[list[dict[str, object]]] = []

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del cancellation_event, max_tokens
                self.requests.append([dict(message) for message in messages])
                self.assert_no_tool_replay(tools)
                return AssistantTurn("已开始重新检查。")

            @staticmethod
            def assert_no_tool_replay(tools):
                if not tools:
                    raise AssertionError("a fresh plan-step agent must have its bounded tools")

        with local_temp() as temp:
            store = PlanStore(Workspace(temp))
            store.add("检查已核验的合成状态")
            store.transition(1, "in-progress")
            store.record_progress(1, "tool_execution", "write_file")
            store.recover_unfinished()
            provider = FinalProvider()
            inputs = iter(["/继续 核验 1", "是", "", "是", "/退出"])
            prompts: list[str] = []
            outputs: list[str] = []

            def input_fn(prompt: str) -> str:
                prompts.append(prompt)
                return next(inputs)

            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=input_fn, output=outputs.append)

            self.assertEqual(rc, 0)
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(
                [message["role"] for message in provider.requests[0]], ["system", "user"]
            )
            user_message = provider.requests[0][1]["content"]
            self.assertIn("检查已核验的合成状态", user_message)
            self.assertNotIn("PRIVATE_TOOL_RESULT", json.dumps(provider.requests, ensure_ascii=False))
            self.assertTrue(any("确认已核验" in prompt for prompt in prompts))
            self.assertFalse(any("确认开始此步骤" in prompt for prompt in prompts))
            current = store.load()
            self.assertEqual(current.steps[0].status, "in-progress")
            self.assertEqual(current.progress.last_result, "user_verified")
            self.assertIn("已开始重新检查", "\n".join(outputs))

    def test_side_effect_before_result_checkpoint_recovers_as_unknown(self) -> None:
        class WriteProvider:
            label = "offline-write-recovery-test"
            model = "synthetic"

            def __init__(self) -> None:
                self.calls = 0

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del messages, tools, cancellation_event, max_tokens
                self.calls += 1
                if self.calls == 1:
                    return AssistantTurn("", [ToolCall(
                        "synthetic-write", "write_file",
                        {"path": "written.txt", "content": "SYNTHETIC_WRITTEN_CONTENT"},
                    )])
                raise AssertionError("a failed progress checkpoint must stop before another request")

        with local_temp() as temp:
            provider = WriteProvider()
            inputs = iter(["/计划 添加 写入合成文件", "/计划 执行 1", "是", "是", "/退出"])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            original = PlanStore.record_progress

            def fail_result_checkpoint(store, index, event, tool=None):
                if event == "tool_result":
                    raise PlanError("synthetic checkpoint interruption")
                return original(store, index, event, tool)

            with (
                patch("cvfx_harness.cli.provider_from_environment", return_value=provider),
                patch.object(PlanStore, "record_progress", fail_result_checkpoint),
            ):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

            self.assertEqual(rc, 0)
            self.assertEqual(provider.calls, 1)
            written = Path(temp, "written.txt")
            self.assertEqual(written.read_text(encoding="utf-8"), "SYNTHETIC_WRITTEN_CONTENT")
            snapshot = PlanStore(Workspace(temp)).load()
            self.assertEqual(snapshot.steps[0].status, "blocked")
            self.assertEqual(snapshot.progress.phase, "tool_execution")
            self.assertEqual(snapshot.progress.last_result, "outcome_unknown")
            saved = (Path(temp) / ".harness" / "plans" / "current.json").read_text(encoding="utf-8")
            self.assertNotIn("written.txt", saved)
            self.assertNotIn("SYNTHETIC_WRITTEN_CONTENT", saved)
            self.assertIn("结果：执行结果未知", "\n".join(outputs))
            self.assertIn("/继续 核验 1", "\n".join(outputs))

    def test_cancellation_while_waiting_for_tool_approval_is_not_executed(self) -> None:
        class ReadProvider:
            label = "offline-approval-interrupt-test"
            model = "synthetic"

            def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
                del messages, tools, cancellation_event, max_tokens
                return AssistantTurn("", [ToolCall(
                    "synthetic-read", "read_file", {"path": "private/path.txt"}
                )])

        with local_temp() as temp:
            Path(temp, "private").mkdir()
            Path(temp, "private", "path.txt").write_text("SYNTHETIC_PRIVATE_CONTENT", encoding="utf-8")
            inputs = iter(["/计划 添加 读取合成文件", "/计划 执行 1", "是", "/退出"])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)

            def input_fn(prompt: str) -> str:
                if "确认读取？" in prompt:
                    raise KeyboardInterrupt
                return next(inputs)

            with patch("cvfx_harness.cli.provider_from_environment", return_value=ReadProvider()):
                rc = run_console(args, input_fn=input_fn, output=outputs.append)

            self.assertEqual(rc, 0)
            snapshot = PlanStore(Workspace(temp)).load()
            self.assertEqual(snapshot.steps[0].status, "cancelled")
            self.assertEqual(snapshot.progress.phase, "tool_approval")
            self.assertEqual(snapshot.progress.last_tool, "read_file")
            self.assertEqual(snapshot.progress.last_result, "approval_pending")
            saved = (Path(temp) / ".harness" / "plans" / "current.json").read_text(encoding="utf-8")
            self.assertNotIn("private/path.txt", saved)
            self.assertNotIn("SYNTHETIC_PRIVATE_CONTENT", saved)
            self.assertIn("工具尚未获批，未执行", "\n".join(outputs))

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
            self.assertIn("[受阻]", combined)
            self.assertIn("[待办]", combined)
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
            self.assertIn("[已取消]", "\n".join(outputs))
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
            self.assertIn("[已完成]", combined)
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
