from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.cli import build_parser, run_console
from cvfx_harness.profiles import TestProfileError, TestProfileStore
from cvfx_harness.providers import MockProvider
from cvfx_harness.test_runner import TestRunResult
from cvfx_harness.tui import CommandCompleter
from cvfx_harness.tools import ToolCall, Workspace, WorkspaceError, run_tool


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

            profile = store.add("unit-tests", "unittest", sys.executable, ".", "tests with spaces")
            self.assertEqual(profile.framework, "unittest")
            self.assertEqual(profile.python, os.path.abspath(sys.executable))
            self.assertEqual(store.get("UNIT-TESTS"), profile)
            self.assertEqual(store.load(), [profile])
            config_path = project / ".harness" / "config" / "test_profiles.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"version", "profiles"})
            self.assertEqual(
                set(payload["profiles"][0]), {"id", "framework", "cwd", "target", "python"}
            )
            with self.assertRaises(WorkspaceError):
                workspace.resolve(".harness/config/test_profiles.json")
            approvals = []
            with self.assertRaises(WorkspaceError):
                run_tool(
                    workspace,
                    ToolCall(
                        "write-config",
                        "write_file",
                        {"path": ".harness/config/test_profiles.json", "content": "{}"},
                    ),
                    approve_action=lambda request: approvals.append(request) or True,
                )
            self.assertEqual(approvals, [])

            with self.assertRaises(TestProfileError):
                store.add("UNIT-TESTS", "unittest", sys.executable, ".", "tests with spaces")
            with self.assertRaises(TestProfileError):
                store.add("unit -c bad", "unittest", sys.executable, ".", "tests with spaces")
            with self.assertRaises(TestProfileError):
                store.add("pytest", "pytest", sys.executable, ".", "../outside")
            with self.assertRaises(TestProfileError):
                store.add("relative-python", "unittest", "python", ".", "tests with spaces")
            with self.assertRaises(TestProfileError):
                store.add(
                    "python-directory", "unittest", str(project), ".", "tests with spaces"
                )

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

    def test_legacy_profile_is_disabled_until_user_selects_external_python(self) -> None:
        with self._synthetic_project() as root:
            project = Path(root)
            (project / "tests").mkdir()
            workspace = Workspace(project)
            store = TestProfileStore(workspace)
            config_path = project / ".harness" / "config" / "test_profiles.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({
                    "version": 1,
                    "profiles": [{
                        "id": "old", "framework": "unittest", "cwd": ".", "target": "tests"
                    }],
                }),
                encoding="utf-8",
            )
            self.assertIsNone(store.load()[0].python)
            self.assertEqual(store.runnable(), [])
            with self.assertRaisesRegex(TestProfileError, "尚未选择外部 Python"):
                store.get("old")
            replaced = store.add("old", "unittest", sys.executable, ".", "tests")
            self.assertEqual(store.get("old"), replaced)

    def test_stale_interpreter_is_not_offered_and_user_can_remove_profile(self) -> None:
        with self._synthetic_project() as root:
            project = Path(root)
            (project / "tests").mkdir()
            workspace = Workspace(project)
            store = TestProfileStore(workspace)
            store.add("stale", "unittest", sys.executable, ".", "tests")
            config_path = project / ".harness" / "config" / "test_profiles.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            removed_interpreter = project / (
                "removed-python.exe" if os.name == "nt" else "removed-python"
            )
            payload["profiles"][0]["python"] = str(removed_interpreter)
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(store.runnable(), [])
            self.assertEqual(store.get("stale").python, str(removed_interpreter))
            store.remove("stale")
            self.assertEqual(store.load(), [])

    def test_frozen_harness_executable_cannot_be_selected_as_its_python(self) -> None:
        with self._synthetic_project() as root:
            project = Path(root)
            (project / "tests").mkdir()
            workspace = Workspace(project)
            store = TestProfileStore(workspace)
            executable = project / "cvfx-harness.exe"
            executable.write_bytes(b"synthetic harness executable marker")
            executable.chmod(0o755)
            with patch("cvfx_harness.profiles.sys.frozen", True, create=True), patch(
                "cvfx_harness.profiles.sys.executable", str(executable)
            ):
                with self.assertRaisesRegex(TestProfileError, "Harness 自身"):
                    store.add("self", "unittest", str(executable), ".", "tests")

            not_python = project / (
                "arbitrary-runner.exe" if os.name == "nt" else "arbitrary-runner"
            )
            not_python.write_bytes(b"synthetic executable marker")
            not_python.chmod(0o755)
            with self.assertRaisesRegex(TestProfileError, "不像受支持的 Python"):
                store.add("arbitrary", "unittest", str(not_python), ".", "tests")

    def test_cli_profile_management_handles_spaces_and_runs_only_after_approval(self) -> None:
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
            (project / "tests with spaces" / "test_cli.py").write_text(
                "import unittest\n"
                "class SyntheticCliProfile(unittest.TestCase):\n"
                "    def test_cli_profile(self):\n"
                "        print('synthetic CLI profile executed')\n",
                encoding="utf-8",
            )
            provider = CountingMock()
            outputs: list[str] = []
            inputs = iter([
                "/测试 列表",
                "/测试 添加 unit",
                "unittest",
                sys.executable,
                ".",
                "tests with spaces",
                "/测试 列表",
                "/测试 运行 unit",
                "是",
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
            self.assertTrue(any("当前没有测试配置" in line for line in outputs))
            self.assertTrue(any("/测试 添加 <ID>" in line for line in outputs))
            self.assertTrue(any("Harness 不会自动安装依赖" in line for line in outputs))
            self.assertTrue(any("只保存配置，不会运行" in line for line in outputs))
            self.assertTrue(any("tests with spaces" in line for line in outputs))
            self.assertTrue(any("synthetic CLI profile executed" in line for line in outputs))
            self.assertTrue(any("Python 解释器：" in line for line in outputs))
            self.assertTrue(any("输出仅在本地显示" in line for line in outputs))
            self.assertTrue(any("已移除测试配置" in line for line in outputs))
            self.assertEqual(TestProfileStore(Workspace(project)).load(), [])

    def test_missing_pytest_prints_manual_recovery_steps_without_installing(self) -> None:
        with self._synthetic_project() as root:
            project = Path(root)
            (project / "tests").mkdir()
            outputs: list[str] = []
            inputs = iter([
                "/测试 添加 pytest-suite",
                "pytest",
                sys.executable,
                ".",
                "tests",
                "/测试 运行 pytest-suite",
                "是",
                "/退出",
            ])
            args = build_parser().parse_args(
                ["--workspace", str(project), "--provider", "mock"]
            )
            missing_pytest = TestRunResult(
                "failed",
                1,
                "ModuleNotFoundError: No module named 'pytest'\n",
                44,
                False,
            )
            with (
                patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()),
                patch("cvfx_harness.test_runner.execute_profile", return_value=missing_pytest) as execute,
            ):
                result = run_console(
                    args,
                    input_fn=lambda _prompt: next(inputs),
                    output=outputs.append,
                )

        self.assertEqual(result, 0)
        execute.assert_called_once()
        self.assertTrue(any("外部 Python 环境没有 pytest" in line for line in outputs))
        self.assertTrue(any("Harness 不会自动安装依赖" in line for line in outputs))
        self.assertTrue(any("改用 unittest 配置" in line for line in outputs))

    def test_stale_interpreter_gives_reconfiguration_steps_without_approval(self) -> None:
        with self._synthetic_project() as root:
            project = Path(root)
            (project / "tests").mkdir()
            workspace = Workspace(project)
            store = TestProfileStore(workspace)
            store.add("stale-suite", "unittest", sys.executable, ".", "tests")
            config_path = project / ".harness" / "config" / "test_profiles.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            missing = project / ("removed-python.exe" if os.name == "nt" else "removed-python")
            payload["profiles"][0]["python"] = str(missing)
            config_path.write_text(json.dumps(payload), encoding="utf-8")

            outputs: list[str] = []
            inputs = iter(["/测试 运行 stale-suite", "/测试 列表", "/退出"])
            args = build_parser().parse_args(
                ["--workspace", str(project), "--provider", "mock"]
            )
            with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                result = run_console(
                    args,
                    input_fn=lambda _prompt: next(inputs),
                    output=outputs.append,
                )

        self.assertEqual(result, 0)
        self.assertTrue(any("Python 解释器不可用" in line for line in outputs))
        self.assertTrue(any("/测试 添加 <ID>" in line for line in outputs))
        self.assertFalse(any("argv：" in line for line in outputs))

    def test_cancelled_test_run_suggests_explicit_retry_without_restarting(self) -> None:
        with self._synthetic_project() as root:
            project = Path(root)
            (project / "tests").mkdir()
            outputs: list[str] = []
            inputs = iter([
                "/测试 添加 unit-suite",
                "unittest",
                sys.executable,
                ".",
                "tests",
                "/测试 运行 unit-suite",
                "是",
                "/退出",
            ])
            args = build_parser().parse_args(
                ["--workspace", str(project), "--provider", "mock"]
            )
            cancelled = TestRunResult("cancelled", None, "", 0, False)
            with (
                patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()),
                patch("cvfx_harness.test_runner.execute_profile", return_value=cancelled) as execute,
            ):
                result = run_console(
                    args,
                    input_fn=lambda _prompt: next(inputs),
                    output=outputs.append,
                )

        self.assertEqual(result, 0)
        execute.assert_called_once()
        self.assertTrue(any("/测试 运行 unit-suite 重新开始" in line for line in outputs))
        self.assertTrue(any("不会自动重试" in line for line in outputs))


if __name__ == "__main__":
    unittest.main()
