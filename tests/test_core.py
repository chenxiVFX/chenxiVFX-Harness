from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.agent import Agent
from cvfx_harness.cli import parse_references
from cvfx_harness.providers import AssistantTurn, MockProvider, OpenAICompatibleProvider, ProviderError, ToolCall
from cvfx_harness.tools import Workspace, WorkspaceError, run_tool


def make_test_temp(prefix: str = "case-") -> tempfile.TemporaryDirectory:
    local_temp = Path(__file__).resolve().parents[1] / ".test-tmp"
    local_temp.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=local_temp, prefix=prefix)


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = make_test_temp(prefix="中文工作区-")
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unicode_path_read_write(self) -> None:
        (self.root / "资料").mkdir()
        target = self.workspace.write_text("资料/你好.txt", "模型测试：中文 ✓\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "模型测试：中文 ✓\n")
        self.assertEqual(self.workspace.read_text("资料/你好.txt"), "模型测试：中文 ✓\n")

    def test_rejects_paths_outside_workspace(self) -> None:
        for path in ("../outside.txt", "C:\\outside.txt", "\\\\server\\share\\x.txt"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                self.workspace.resolve(path)

    def test_file_reference_supports_spaces_and_chinese(self) -> None:
        (self.root / "参考 文件.txt").write_text("可引用内容", encoding="utf-8")
        text, refs = parse_references("解释 @{参考 文件.txt} 谢谢", self.workspace)
        self.assertEqual(text, "解释  谢谢")
        self.assertEqual(refs, [("参考 文件.txt", "可引用内容")])

    def test_write_requires_approval_and_plan_blocks(self) -> None:
        call = ToolCall("1", "write_file", {"path": "新文件.txt", "content": "data"})
        denied = run_tool(self.workspace, call, approve_write=lambda _path, _exists: False)
        self.assertIn("拒绝", denied)
        self.assertFalse((self.root / "新文件.txt").exists())

        approval_calls: list[tuple[str, bool]] = []
        planned = run_tool(
            self.workspace,
            call,
            approve_write=lambda path, exists: approval_calls.append((path, exists)) or True,
            plan_only=True,
        )
        self.assertIn("计划模式", planned)
        self.assertEqual(approval_calls, [])
        self.assertFalse((self.root / "新文件.txt").exists())

        approved = run_tool(
            self.workspace,
            call,
            approve_write=lambda path, exists: approval_calls.append((path, exists)) or True,
        )
        self.assertIn("已写入", approved)
        self.assertEqual((self.root / "新文件.txt").read_text(encoding="utf-8"), "data")
        self.assertEqual(approval_calls, [("新文件.txt", False)])


class ScriptedProvider:
    label = "scripted"
    model = "test"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = turns
        self.requests: list[list[dict[str, object]]] = []

    def complete(self, messages, tools):
        del tools
        self.requests.append(list(messages))
        return self.turns.pop(0)


class AgentTests(unittest.TestCase):
    def test_mock_provider_is_offline(self) -> None:
        turn = MockProvider().complete([{"role": "user", "content": "你好"}], [])
        self.assertIn("[模拟模式]", turn.content)
        self.assertIn("你好", turn.content)

    def test_tool_cycle_passes_result_and_gates_write(self) -> None:
        with make_test_temp() as temp:
            workspace = Workspace(temp)
            arguments = {"path": "你好.txt", "content": "写入成功"}
            assistant_message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps(arguments, ensure_ascii=False)},
                }],
            }
            provider = ScriptedProvider([
                AssistantTurn("", [ToolCall("call-1", "write_file", arguments)], assistant_message),
                AssistantTurn("已完成。", assistant_message={"role": "assistant", "content": "已完成。"}),
            ])
            approvals: list[tuple[str, bool]] = []
            agent = Agent(
                provider,
                workspace,
                approve_write=lambda path, exists: approvals.append((path, exists)) or True,
            )
            self.assertEqual(agent.ask("创建文件"), "已完成。")
            self.assertEqual(approvals, [("你好.txt", False)])
            self.assertEqual((Path(temp) / "你好.txt").read_text(encoding="utf-8"), "写入成功")
            self.assertEqual(provider.requests[1][-1]["role"], "tool")


class ProviderTests(unittest.TestCase):
    def test_openai_request_keeps_key_out_of_body(self) -> None:
        provider = OpenAICompatibleProvider(
            "https://api.deepseek.com", "test-secret-not-a-real-key", "deepseek-flash"
        )
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size):
                return json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": "收到"}}]}
                ).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["body"] = request.data.decode("utf-8")
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            turn = provider.complete([{"role": "user", "content": "你好"}], [])

        self.assertEqual(turn.content, "收到")
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["auth"], "Bearer test-secret-not-a-real-key")
        self.assertNotIn("test-secret-not-a-real-key", captured["body"])
        self.assertIn("deepseek-flash", captured["body"])

    def test_rejects_remote_http_and_missing_key(self) -> None:
        with self.assertRaises(ProviderError):
            OpenAICompatibleProvider("http://example.com", "key", "model")
        with self.assertRaisesRegex(ProviderError, "未配置 DEEPSEEK_API_KEY"):
            OpenAICompatibleProvider("https://api.deepseek.com", "", "deepseek-flash")


if __name__ == "__main__":
    unittest.main()
