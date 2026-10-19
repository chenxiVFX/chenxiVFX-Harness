from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.cli import build_parser, run_console
from cvfx_harness.profiles import TestProfileError, TestProfileStore
from cvfx_harness.providers import MockProvider
from cvfx_harness.tui import CommandCompleter
from cvfx_harness.tools import Workspace, WorkspaceError


class TestProfileStoreTests(unittest.TestCase):
    __test__ = False

    def _synthetic_project(self) -> tempfile.TemporaryDirectory:
        root = Path(__file__).resolve().parents[1] / ".test-tmp"
        root.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(dir=root, prefix="配置 合成项目 ")

    def test_profiles_are_user_owned_fixed_python_shapes(self) -> None:
        with self._synthetic_project() as root:
            project = Path(root)
            target = project / "tests with spaces"
            target.mkdir()
            (target / "test_sample.py").write_text("def test_sample(): pass\n", encoding="utf-8")
            workspace = Workspace(project)
            store = TestProfileStore(workspace)

            profile = store.add("unit-tests", "unittest", ".", "tests with spaces")
            self.assertEqual(profile.framework, "unittest")
            self.assertEqual(store.get("UNIT-TESTS"), profile)
            self.assertEqual(store.load(), [profile])
            config_path = project / ".harness" / "config" / "test_profiles.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"version", "profiles"})
            self.assertEqual(set(payload["profiles"][0]), {"id", "framework", "cwd", "target"})
            with self.assertRaises(WorkspaceError):
                workspace.resolve(".harness/config/test_profiles.json")

            with self.assertRaises(TestProfileError):
                store.add("UNIT-TESTS", "unittest", ".", "tests with spaces")
            with self.assertRaises(TestProfileError):
                store.add("unit -c bad", "unittest", ".", "tests with spaces")
            with self.assertRaises(TestProfileError):
                store.add("pytest", "pytest", ".", "../outside")

            from prompt_toolkit.completion import CompleteEvent
            from prompt_toolkit.document import Document

            suggestions = [
                item.text
                for item in CommandCompleter(workspace).get_completions(
                    Document("/测试 添"), CompleteEvent()
                )
            ]
            self.assertEqual(suggestions, ["添加"])

            payload["profiles"][0]["argv"] = ["-c", "synthetic arbitrary code"]
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(TestProfileError, "结构无效"):
                store.load()

    def test_cli_profile_management_handles_spaces_and_never_runs_tests(self) -> None:
        class CountingMock(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.requests = 0

            def complete(self, *args, **kwargs):
                self.requests += 1
                return super().complete(*args, **kwargs)

        with self._synthetic_project() as root:
            project = Path(root)
            (project / "tests with spaces").mkdir()
            provider = CountingMock()
            outputs: list[str] = []
            inputs = iter([
                "/测试 添加 unit",
                "unittest",
                ".",
                "tests with spaces",
                "/测试 列表",
                "/测试 移除 unit",
                "是",
                "/退出",
            ])
            args = build_parser().parse_args(
                ["--workspace", str(project), "--provider", "mock"]
            )
            with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
                result = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

            self.assertEqual(result, 0)
            self.assertEqual(provider.requests, 0)
            self.assertTrue(any("只保存配置，不会运行" in line for line in outputs))
            self.assertTrue(any("tests with spaces" in line for line in outputs))
            self.assertTrue(any("已移除测试配置" in line for line in outputs))
            self.assertEqual(TestProfileStore(Workspace(project)).load(), [])


if __name__ == "__main__":
    unittest.main()
