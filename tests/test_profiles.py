from __future__ import annotations

import json
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.cli import build_parser, run_console
from cvfx_harness.profiles import TestProfileError, TestProfileStore
from cvfx_harness.providers import MockProvider
from cvfx_harness.test_runner import TestRunResult, run_test_profile_tool
from cvfx_harness.tui import CommandCompleter
from cvfx_harness.tools import ToolCall, Workspace, WorkspaceError, build_tool_schemas, run_tool


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
            self.assertEqual(suggestions, ["添加", "添加命令"])

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

    def test_version_two_python_profiles_survive_command_profile_migration(self) -> None:
        with self._synthetic_project() as root:
            project = Path(root)
            (project / "tests").mkdir()
            store = TestProfileStore(Workspace(project))
            config_path = project / ".harness" / "config" / "test_profiles.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({
                    "version": 2,
                    "profiles": [{
                        "id": "python-old",
                        "framework": "unittest",
                        "cwd": ".",
                        "target": "tests",
                        "python": os.path.abspath(sys.executable),
                    }],
                }),
                encoding="utf-8",
            )
            original = store.get("python-old")
            command = store.add_command("direct", sys.executable, '["--version"]', ".")
            migrated = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(migrated["version"], 3)
            self.assertEqual(store.get("python-old"), original)
            self.assertEqual(store.get("direct"), command)
            self.assertEqual(
                [item["id"] for item in migrated["profiles"]],
                ["python-old", "direct"],
            )

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

    def test_node_command_profile_fails_then_passes_after_reviewed_patch(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is installed by CI; local host has no Node executable")
        with self._synthetic_project() as root:
            project = Path(root)
            shutil.copy2(
                Path(__file__).parent / "fixtures" / "test_profile_runner" / "node" / "repair.test.cjs",
                project / "repair.test.cjs",
            )
            first_outputs: list[str] = []
            inputs = iter([
                "/测试 添加命令 node-smoke",
                node,
                '["--test"]',
                ".",
                "/测试 列表",
                "/测试 运行 node-smoke",
                "是",
                "/退出",
            ])
            args = build_parser().parse_args(
                ["--workspace", str(project), "--provider", "mock"]
            )
            with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                result = run_console(
                    args,
                    input_fn=lambda _prompt: next(inputs),
                    output=first_outputs.append,
                )

            store = TestProfileStore(Workspace(project))
            profile = store.get("NODE-SMOKE")
            config_path = project / ".harness" / "config" / "test_profiles.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(result, 0)
            self.assertEqual(profile.kind, "command")
            self.assertEqual(profile.args, ("--test",))
            self.assertEqual(store.runnable(), [profile])
            self.assertEqual(payload["version"], 3)
            self.assertEqual(
                payload["profiles"][0],
                {
                    "id": "node-smoke",
                    "kind": "command",
                    "executable": os.path.abspath(node),
                    "args": ["--test"],
                    "cwd": ".",
                },
            )
            self.assertTrue(any("只保存配置，不会运行" in line for line in first_outputs))
            self.assertTrue(any("固定命令" in line and "node-smoke" in line for line in first_outputs))
            self.assertTrue(any("固定命令可执行文件：" in line for line in first_outputs))
            self.assertTrue(any('"--test"' in line for line in first_outputs))
            self.assertTrue(any("工作目录" in line for line in first_outputs))
            self.assertTrue(any("shell=False" in line for line in first_outputs))
            self.assertTrue(any("不是沙箱" in line for line in first_outputs))
            self.assertTrue(any("120 秒" in line for line in first_outputs))
            self.assertTrue(any("32768 bytes" in line for line in first_outputs))
            self.assertTrue(any("Node 合成项目需要一次精确修补" in line for line in first_outputs))
            self.assertTrue(any("退出码为 1" in line for line in first_outputs))
            self.assertTrue(any("AssertionError" in line for line in first_outputs))

            before = (project / "repair.test.cjs").read_bytes()
            patch_result = run_tool(
                Workspace(project),
                ToolCall("node-fix", "patch_file", {
                    "path": "repair.test.cjs",
                    "expected_sha256": hashlib.sha256(before).hexdigest(),
                    "old_text": "2 + 2, 5",
                    "new_text": "2 + 2, 4",
                }),
                approve_action=lambda request: request.operation == "patch",
            )
            self.assertIn("已应用精确补丁", patch_result)

            second_outputs: list[str] = []
            second_inputs = iter(["/测试 运行 node-smoke", "是", "/退出"])
            with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                second_result = run_console(
                    args,
                    input_fn=lambda _prompt: next(second_inputs),
                    output=second_outputs.append,
                )
            self.assertEqual(second_result, 0)
            self.assertTrue(any("Node 修补后复测通过" in line for line in second_outputs))
            self.assertTrue(any("退出码为 0" in line for line in second_outputs))

    def test_command_profile_requires_new_approval_if_args_or_cwd_change(self) -> None:
        executable = shutil.which("node")
        if executable is None:
            self.skipTest("Node is installed by CI; local host has no Node executable")
        with self._synthetic_project() as root:
            project = Path(root)
            (project / "alternate").mkdir()
            (project / "marker.cjs").write_text(
                "require('node:fs').writeFileSync('executed.flag', 'yes');\n",
                encoding="utf-8",
            )
            workspace = Workspace(project)
            store = TestProfileStore(workspace)
            profile = store.add_command("mutable", executable, '["marker.cjs"]', ".")
            approval_requests = []
            with patch("cvfx_harness.test_runner.execute_profile") as execute:
                denied = run_test_profile_tool(
                    workspace,
                    store,
                    profile.profile_id,
                    approve_action=lambda request: approval_requests.append(request) or False,
                    share_output=False,
                )
                self.assertIn("拒绝", denied)

                def change_args(_request):
                    store.remove(profile.profile_id)
                    store.add_command(profile.profile_id, executable, '["--version"]', ".")
                    return True

                changed_args = run_test_profile_tool(
                    workspace,
                    store,
                    profile.profile_id,
                    approve_action=change_args,
                    share_output=False,
                )
                self.assertIn("配置已更改", changed_args)

                def change_cwd(_request):
                    store.remove(profile.profile_id)
                    store.add_command(profile.profile_id, executable, '["--version"]', "alternate")
                    return True

                changed_cwd = run_test_profile_tool(
                    workspace,
                    store,
                    profile.profile_id,
                    approve_action=change_cwd,
                    share_output=False,
                )
                self.assertIn("配置已更改", changed_cwd)

            self.assertEqual(len(approval_requests), 1)
            execute.assert_not_called()
            self.assertFalse((project / "executed.flag").exists())

    def test_fixed_command_output_sharing_has_a_separate_approval(self) -> None:
        executable = shutil.which("node")
        if executable is None:
            self.skipTest("Node is installed by CI; local host has no Node executable")
        with self._synthetic_project() as root:
            project = Path(root)
            shutil.copy2(
                Path(__file__).parent / "fixtures" / "test_profile_runner" / "node" / "smoke.test.cjs",
                project / "smoke.test.cjs",
            )
            workspace = Workspace(project)
            store = TestProfileStore(workspace)
            profile = store.add_command("share-node", executable, '["--test"]', ".")
            approvals = []
            local_output = []

            def approve(request):
                approvals.append(request)
                return request.operation == "run_test"

            result = run_test_profile_tool(
                workspace,
                store,
                profile.profile_id,
                approve_action=approve,
                emit_output=local_output.append,
                share_output=True,
            )

            self.assertEqual([item.operation for item in approvals], ["run_test", "share_test_output"])
            self.assertTrue(any("Node 固定命令 profile 合成测试通过" in item for item in local_output))
            self.assertIn("用户选择仅在本地查看", result)
            self.assertNotIn("用户另行批准的脱敏输出", result)

    def test_command_profile_rejects_shells_bad_json_and_model_supplied_fields(self) -> None:
        executable = shutil.which("node")
        if executable is None:
            self.skipTest("Node is installed by CI; local host has no Node executable")
        with self._synthetic_project() as root:
            project = Path(root)
            workspace = Workspace(project)
            store = TestProfileStore(workspace)
            shell = shutil.which("cmd.exe") or shutil.which("sh") or shutil.which("bash")
            if shell is not None:
                with self.assertRaisesRegex(TestProfileError, "shell 或命令解释器"):
                    store.add_command("shell", shell, '["-c", "echo unsafe"]', ".")
            with self.assertRaisesRegex(TestProfileError, "合法 JSON"):
                store.add_command("bad-json", executable, "--test", ".")
            profile = store.add_command("fixed", executable, '["--test"]', ".")
            runner_schema = next(
                item for item in build_tool_schemas([profile.profile_id])
                if item["function"]["name"] == "run_test_profile"
            )
            self.assertEqual(
                runner_schema["function"]["parameters"]["properties"]["profile_id"]["enum"],
                [profile.profile_id],
            )
            self.assertEqual(
                set(runner_schema["function"]["parameters"]["properties"]),
                {"profile_id"},
            )
            approvals: list[object] = []
            with self.assertRaisesRegex(WorkspaceError, "缺失或包含未授权字段"):
                run_tool(
                    workspace,
                    ToolCall(
                        "invalid-command-args",
                        "run_test_profile",
                        {"profile_id": profile.profile_id, "args": ["--eval"]},
                    ),
                    approve_action=lambda request: approvals.append(request) or True,
                    test_profile_store=store,
                )
            self.assertEqual(approvals, [])


if __name__ == "__main__":
    unittest.main()
