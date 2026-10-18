from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.agent import Agent, SYSTEM_PROMPT
from cvfx_harness.cli import run_console
from cvfx_harness.providers import AssistantTurn, ToolCall
from cvfx_harness.sessions import SessionError, SessionStore
from cvfx_harness.tools import Workspace
from cvfx_harness.usage import ProviderUsage, UsageStats, estimate_tokens


class ScriptedProvider:
    label = "scripted"
    model = "offline-test"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = turns
        self.requests: list[list[dict[str, object]]] = []

    def complete(self, messages, tools):
        del tools
        self.requests.append([dict(message) for message in messages])
        return self.turns.pop(0)


def local_temp() -> tempfile.TemporaryDirectory:
    base = Path(__file__).resolve().parents[1] / ".test-tmp"
    base.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base, prefix="session-中文-")


class SessionTests(unittest.TestCase):
    def test_save_and_restore_unicode_plain_dialogue_only(self) -> None:
        with local_temp() as temp:
            workspace = Workspace(temp)
            store = SessionStore(workspace)
            agent = Agent(ScriptedProvider([]), workspace, approve_action=lambda _request: True)
            agent.turns = [
                _dialogue("请分析 中文路径 C:/工作/说明.txt", "路径包含中文。"),
                _dialogue("api_key=do_not_persist_this_secret", "凭据不应保存。"),
                _tool_turn(),
                _dialogue("读取 @需求.txt", "这是引用内容。", persistable=False),
            ]
            agent.goal = "本次只处理中文文档"
            agent.usage = UsageStats(requests=2, estimated_input_tokens=80)
            saved = store.save(
                "中文_会话-1",
                agent.safe_session_turns(),
                goal=agent.goal,
                usage=agent.usage.to_dict(),
            )
            self.assertEqual(saved, 1)
            archive = workspace.root / ".harness" / "sessions" / "中文_会话-1.json"
            text = archive.read_text(encoding="utf-8")
            self.assertIn("中文路径", text)
            self.assertNotIn("do_not_persist_this_secret", text)
            self.assertNotIn("tool result sentinel", text)
            self.assertNotIn("SYSTEM_PROMPT", text)

            loaded = store.load("中文_会话-1")
            restored = Agent(ScriptedProvider([]), workspace, approve_action=lambda _request: True)
            restored.restore(turns=loaded.turns, goal=loaded.goal, usage=loaded.usage)
            self.assertEqual(restored.goal, "本次只处理中文文档")
            self.assertEqual(restored.messages[0], {"role": "system", "content": SYSTEM_PROMPT})
            self.assertEqual(restored.messages[1]["role"], "user")
            self.assertEqual(restored.messages[2]["role"], "assistant")
            self.assertEqual(restored.usage.requests, 2)

    def test_corrupt_restore_is_rejected_before_live_agent_changes(self) -> None:
        with local_temp() as temp:
            workspace = Workspace(temp)
            store = SessionStore(workspace)
            store.save("bad", [], goal="saved")
            session_file = workspace.root / ".harness" / "sessions" / "bad.json"
            session_file.write_text("{ truncated", encoding="utf-8")
            agent = Agent(ScriptedProvider([]), workspace, approve_action=lambda _request: True)
            agent.goal = "current goal"
            agent.turns = [_dialogue("当前对话", "保持不动")]
            agent._rebuild_messages()
            previous = json.dumps(agent.messages, ensure_ascii=False)
            with self.assertRaises(SessionError):
                loaded = store.load("bad")
                agent.restore(turns=loaded.turns, goal=loaded.goal, usage=loaded.usage)
            self.assertEqual(json.dumps(agent.messages, ensure_ascii=False), previous)
            self.assertEqual(agent.goal, "current goal")
            session_file.write_text(
                json.dumps({
                    "version": 1,
                    "name": "bad",
                    "saved_at": "now",
                    "goal": "",
                    "turns": [{"kind": [], "messages": []}],
                    "usage": {},
                }),
                encoding="utf-8",
            )
            with self.assertRaises(SessionError):
                store.load("bad")
            self.assertEqual(json.dumps(agent.messages, ensure_ascii=False), previous)

    def test_session_names_and_sensitive_records_are_rejected(self) -> None:
        with local_temp() as temp:
            store = SessionStore(Workspace(temp))
            for name in ("../escape", ".hidden", "a/b", "", "x" * 41):
                with self.subTest(name=name), self.assertRaises(SessionError):
                    store.save(name, [], usage={})
            with self.assertRaisesRegex(SessionError, "密钥"):
                store.save(
                    "unsafe",
                    [{
                        "kind": "dialogue",
                        "messages": [
                            {"role": "user", "content": "sk-" + "a" * 24},
                            {"role": "assistant", "content": "不要存储"},
                        ],
                    }],
                    usage={},
                )
            with self.assertRaisesRegex(SessionError, "密钥"):
                store.save(
                    "opaque-token",
                    [{
                        "kind": "dialogue",
                        "messages": [
                            {"role": "user", "content": "ordinary question"},
                            {"role": "assistant", "content": "opaque_" + "a9K2mV7p" * 6},
                        ],
                    }],
                    usage={},
                )

    def test_provider_usage_and_long_chinese_estimation(self) -> None:
        long_chinese = "中文上下文测试" * 1000
        self.assertGreaterEqual(estimate_tokens(long_chinese), 7000)
        stats = UsageStats()
        stats.record_request(8000)
        stats.record_response(120, ProviderUsage(8100, 120, 8220))
        self.assertEqual(stats.provider_usage_reports, 1)
        self.assertEqual(stats.provider_prompt_tokens, 8100)
        self.assertEqual(UsageStats.from_dict(stats.to_dict()).to_dict(), stats.to_dict())

    def test_reference_or_tool_turn_taints_later_session_saves(self) -> None:
        with local_temp() as temp:
            provider = ScriptedProvider([
                AssistantTurn("已检查文件。", assistant_message={"role": "assistant", "content": "已检查文件。"}),
                AssistantTurn("后来答复", assistant_message={"role": "assistant", "content": "后来答复"}),
            ])
            agent = Agent(provider, Workspace(temp), approve_action=lambda _request: True)
            agent.ask("请看文件", references=[("a.txt", "private file body")])
            agent.ask("后续问题")
            self.assertEqual(agent.safe_session_turns(), [])

        with local_temp() as temp:
            tool_call_message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"safe.txt"}'},
                }],
            }
            provider = ScriptedProvider([
                AssistantTurn("", [ToolCall("call-1", "read_file", {"path": "safe.txt"})], tool_call_message),
                AssistantTurn("读取失败。", assistant_message={"role": "assistant", "content": "读取失败。"}),
                AssistantTurn("后续回答", assistant_message={"role": "assistant", "content": "后续回答"}),
            ])
            agent = Agent(provider, Workspace(temp), approve_action=lambda _request: True)
            agent.ask("读取文件")
            agent.ask("后续问题")
            self.assertEqual(agent.safe_session_turns(), [])


class CompressionTests(unittest.TestCase):
    def test_preview_is_untrusted_and_preserves_recent_tool_call_pair(self) -> None:
        with local_temp() as temp:
            assistant_tool = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"safe.txt"}'},
                }],
            }
            provider = ScriptedProvider([
                AssistantTurn("答复甲。" * 300, assistant_message={"role": "assistant", "content": "答复甲。" * 300}),
                AssistantTurn("答复乙。" * 300, assistant_message={"role": "assistant", "content": "答复乙。" * 300}),
                AssistantTurn(
                    "",
                    [ToolCall("call-1", "read_file", {"path": "safe.txt"})],
                    assistant_tool,
                ),
                AssistantTurn("读取完成。", assistant_message={"role": "assistant", "content": "读取完成。"}),
            ])
            agent = Agent(provider, Workspace(temp), approve_action=lambda _request: True)
            agent.ask("第一轮：" + "长中文" * 300)
            agent.ask("第二轮：" + "更多中文" * 300)
            agent.ask("第三轮工具调用")
            preview = agent.preview_compression(keep_recent_turns=1)
            self.assertIsNotNone(preview)
            assert preview is not None
            self.assertEqual(preview.dropped_tool_turns, 0)
            summary = next(turn for turn in preview.replacement if turn.kind == "summary")
            self.assertEqual(summary.messages[0]["role"], "user")
            self.assertIn("不可信的用户上下文", summary.messages[0]["content"])
            self.assertEqual(agent.messages[0]["content"], SYSTEM_PROMPT)
            recent = preview.replacement[-1]
            self.assertEqual(recent.kind, "tool")
            self.assertEqual([m["role"] for m in recent.messages], ["user", "assistant", "tool", "assistant"])
            before = json.dumps(agent.messages, ensure_ascii=False)
            agent.apply_compression(preview)
            self.assertNotEqual(json.dumps(agent.messages, ensure_ascii=False), before)
            self.assertEqual(agent.messages[0]["content"], SYSTEM_PROMPT)
            self.assertIsNone(agent.preview_compression(keep_recent_turns=1))

    def test_tool_round_is_dropped_as_one_unit_and_never_summarized(self) -> None:
        with local_temp() as temp:
            provider = ScriptedProvider([
                AssistantTurn("普通答复" * 400, assistant_message={"role": "assistant", "content": "普通答复" * 400}),
                AssistantTurn("",
                    [ToolCall("call-1", "read_file", {"path": "safe.txt"})],
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
                ),
                AssistantTurn("tool result sentinel" * 30, assistant_message={"role": "assistant", "content": "tool result sentinel" * 30}),
                AssistantTurn("最近答复" * 200, assistant_message={"role": "assistant", "content": "最近答复" * 200}),
            ])
            agent = Agent(provider, Workspace(temp), approve_action=lambda _request: True)
            agent.ask("旧轮次" * 300)
            agent.ask("旧工具轮次")
            agent.ask("新轮次" * 300)
            preview = agent.preview_compression(keep_recent_turns=1)
            self.assertIsNotNone(preview)
            assert preview is not None
            self.assertEqual(preview.dropped_tool_turns, 1)
            self.assertNotIn("tool result sentinel", "".join(m.get("content", "") or "" for t in preview.replacement for m in t.messages))

    def test_preview_cancellation_and_stale_confirmation_do_not_mutate(self) -> None:
        with local_temp() as temp:
            provider = ScriptedProvider([
                AssistantTurn("回答" * 400, assistant_message={"role": "assistant", "content": "回答" * 400}),
                AssistantTurn("回答" * 400, assistant_message={"role": "assistant", "content": "回答" * 400}),
            ])
            agent = Agent(provider, Workspace(temp), approve_action=lambda _request: True)
            agent.ask("第一个问题" * 250)
            agent.ask("第二个问题" * 250)
            preview = agent.preview_compression(keep_recent_turns=1)
            self.assertIsNotNone(preview)
            assert preview is not None
            original = json.dumps(agent.messages, ensure_ascii=False)
            agent.apply_compression(preview)
            with self.assertRaisesRegex(ValueError, "重新预览"):
                agent.apply_compression(preview)
            self.assertNotEqual(json.dumps(agent.messages, ensure_ascii=False), original)

    def test_cli_compression_denial_keeps_original_context(self) -> None:
        with local_temp() as temp:
            answers = "模拟答复" * 300
            provider = ScriptedProvider([
                AssistantTurn(answers, assistant_message={"role": "assistant", "content": answers})
                for _ in range(4)
            ])
            inputs = iter(["问题甲" * 300, "问题乙" * 300, "问题丙" * 300, "/压缩 1", "n", "问题丁", "/退出"])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)
            self.assertEqual(rc, 0)
            self.assertTrue(any("已取消压缩" in line for line in outputs))
            latest = provider.requests[-1]
            users = [m["content"] for m in latest if m["role"] == "user"]
            self.assertEqual(len(users), 4)
            self.assertFalse(any("untrusted-history-summary" in str(content) for content in users))

    def test_cli_commands_show_help_stats_config_budget_and_bounded_plan(self) -> None:
        with local_temp() as temp:
            provider = ScriptedProvider([])
            inputs = iter([
                "/帮助", "/配置", "/统计", "/预算 22000",
                "/子代理 检查会话 | 检查预算", "/目标 仅供上下文", "/状态", "/退出",
            ])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)
            self.assertEqual(rc, 0)
            combined = "\n".join(outputs)
            self.assertIn("/保存", combined)
            self.assertIn("当前上下文估算", combined)
            self.assertIn("Shell：关闭", combined)
            self.assertIn("已将本地预算提示设为 22000", combined)
            self.assertIn("最大深度：0", combined)
            self.assertIn("执行：关闭", combined)

    def test_interrupted_model_turn_is_not_saved(self) -> None:
        class InterruptOnce:
            label = "interrupt-test"
            model = "offline"

            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools):
                del messages, tools
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt
                return AssistantTurn(
                    "第二轮完成",
                    assistant_message={"role": "assistant", "content": "第二轮完成"},
                )

        with local_temp() as temp:
            provider = InterruptOnce()
            inputs = iter(["第一轮会取消", "第二轮完整", "/保存 cancel-check", "/退出"])
            outputs: list[str] = []
            args = SimpleNamespace(workspace=temp, provider="mock", model=None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                rc = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)
            self.assertEqual(rc, 0)
            self.assertTrue(any("已取消当前模型请求" in line for line in outputs))
            saved = SessionStore(Workspace(temp)).load("cancel-check")
            self.assertEqual(len(saved.turns), 1)
            self.assertIn("第二轮完整", saved.turns[0]["messages"][0]["content"])
            self.assertNotIn("第一轮会取消", saved.turns[0]["messages"][0]["content"])


class DelegationTests(unittest.TestCase):
    def test_delegation_plan_is_bounded_and_non_executing(self) -> None:
        from cvfx_harness.subagents import DelegationPlanError, build_delegation_plan

        plan = build_delegation_plan("读文件 | 设计测试 | 整理文档")
        self.assertEqual(len(plan.tasks), 3)
        self.assertEqual(plan.max_child_depth, 0)
        self.assertEqual(plan.max_turns_per_task, 4)
        self.assertFalse(plan.execution_enabled)
        self.assertFalse(plan.shell_enabled)
        with self.assertRaises(DelegationPlanError):
            build_delegation_plan("a | b | c | d")


def _dialogue(user: str, assistant: str, *, persistable: bool = True):
    from cvfx_harness.agent import ConversationTurn

    return ConversationTurn(
        messages=[{"role": "user", "content": user}, {"role": "assistant", "content": assistant}],
        kind="dialogue",
        persistable=persistable,
        complete=True,
    )


def _tool_turn():
    from cvfx_harness.agent import ConversationTurn

    return ConversationTurn(
        messages=[
            {"role": "user", "content": "请读文件"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
            {"role": "tool", "tool_call_id": "x", "content": "tool result sentinel"},
            {"role": "assistant", "content": "已完成"},
        ],
        kind="tool",
        persistable=False,
        complete=True,
    )


if __name__ == "__main__":
    unittest.main()
