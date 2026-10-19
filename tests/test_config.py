from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvfx_harness.cli import _completion_matches, build_parser, run_console
from cvfx_harness.config import (
    CONFIG_RELATIVE_PATH,
    ConfigError,
    WorkspaceConfig,
    WorkspaceConfigStore,
    format_tui_shortcuts,
    parse_tui_shortcuts,
    resolve_config,
    validate_tui_shortcuts,
    validate_workspace_config,
)
from cvfx_harness.providers import MockProvider, ProviderError, validate_base_url
from cvfx_harness.tools import Workspace


class WorkspaceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cvfx-config-")
        self.workspace = Workspace(self.temp.name)
        self.store = WorkspaceConfigStore(self.workspace)
        self.path = Path(self.temp.name) / CONFIG_RELATIVE_PATH

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_config_round_trip_has_only_non_secret_settings(self) -> None:
        config = WorkspaceConfig(
            provider="openai",
            model="deepseek-flash",
            base_url="https://api.deepseek.com/v1",
            token_budget=32000,
            max_output_tokens=4096,
        )
        self.store.save(config)
        loaded, exists = self.store.load()

        self.assertTrue(exists)
        self.assertEqual(loaded, config)
        serialized = self.path.read_text(encoding="utf-8")
        self.assertNotIn("api_key", serialized.casefold())
        self.assertNotIn("DEEPSEEK_API_KEY", serialized)
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(loaded.tui_shortcuts, {})
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_tui_shortcut_map_is_bounded_canonical_and_round_trips(self) -> None:
        shortcuts = parse_tui_shortcuts("f4=状态，F2=/帮助,F6=commands")
        self.assertEqual(
            shortcuts,
            {"F2": "help", "F4": "status", "F6": "commands"},
        )
        self.assertEqual(
            format_tui_shortcuts(shortcuts), "F2=帮助，F4=状态，F6=命令"
        )
        saved = WorkspaceConfig(tui_shortcuts=shortcuts)
        self.store.save(saved)
        loaded, exists = self.store.load()
        self.assertTrue(exists)
        self.assertEqual(loaded.tui_shortcuts, shortcuts)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["tui_shortcuts"],
            shortcuts,
        )

    def test_tui_shortcuts_reject_duplicates_reserved_keys_actions_and_macros(self) -> None:
        invalid = (
            "F2=帮助,F2=状态",
            "Ctrl+F2=帮助",
            "F1=帮助",
            "Enter=帮助",
            "F2=退出",
            "F2=帮助,F3=/help",
            "F2=帮助,F3=命令,F4=状态,F5=配置,F6=帮助",
            "F2=帮助; /退出",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_tui_shortcuts(value)
        for value in (
            {"F2": "help", "f2": "status"},
            {"F2": "quit"},
            {"Ctrl+C": "help"},
            {"F2": "/退出"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    validate_tui_shortcuts(value)

    def test_optimistic_save_preserves_a_concurrent_configuration_change(self) -> None:
        self.store.save(WorkspaceConfig(provider="openai", model="deepseek-flash"))
        _loaded, _exists, revision = self.store.load_snapshot()
        external = WorkspaceConfig(model="external-edit")
        self.store.save(external)

        with self.assertRaisesRegex(ConfigError, "其他来源修改"):
            self.store.save(
                WorkspaceConfig(provider="openai", model="deepseek-v4-pro"),
                expected_revision=revision,
            )
        self.assertEqual(self.store.load()[0], external)

    def test_unknown_or_secret_config_fields_are_rejected_without_echo(self) -> None:
        secret = "sk-" + "A" * 24
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"version": 1, "provider": "mock", "api_key": secret}),
            encoding="utf-8",
        )

        with self.assertRaises(ConfigError) as caught:
            self.store.load()
        self.assertNotIn(secret, str(caught.exception))

    def test_config_symlink_is_rejected(self) -> None:
        self.path.parent.mkdir(parents=True)
        external = Path(self.temp.name) / "outside-settings.json"
        external.write_text('{"version":1,"provider":"mock"}', encoding="utf-8")
        try:
            os.symlink(external, self.path)
        except (OSError, NotImplementedError) as exc:
            if os.name == "nt":
                self.skipTest(f"Windows runner 未允许创建配置 symlink fixture：{type(exc).__name__}")
            raise
        with self.assertRaisesRegex(ConfigError, "symlink/junction"):
            self.store.load()

    def test_config_hardlink_is_rejected(self) -> None:
        self.path.parent.mkdir(parents=True)
        external = Path(self.temp.name) / "outside-settings.json"
        external.write_text('{"version":1,"provider":"mock"}', encoding="utf-8")
        try:
            os.link(external, self.path)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Windows runner 未允许创建配置 hardlink fixture：{type(exc).__name__}")
            raise
        with self.assertRaisesRegex(ConfigError, "硬链接"):
            self.store.load()

    def test_invalid_endpoints_are_rejected_without_echo(self) -> None:
        secret = "LOCAL_TEST_SECRET_SENTINEL"
        rejected = (
            "https://user:pass@api.deepseek.com",
            f"https://api.deepseek.com?api_key={secret}",
            f"https://api.deepseek.com#{secret}",
            "file:///tmp/model",
            "ftp://api.deepseek.com",
            "http://api.deepseek.com",
            "https://api.deepseek.com:444",
            "https://api.deepseek.com.evil.invalid",
            f"https://api.deepseek.com/{'A' * 48}",
        )
        for url in rejected:
            with self.subTest(url_kind=url.split(":", 1)[0]):
                with self.assertRaises(ConfigError) as caught:
                    validate_workspace_config(WorkspaceConfig(base_url=url))
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn("pass", str(caught.exception))

    def test_provider_url_validator_rejects_userinfo_empty_query_and_fragment(self) -> None:
        rejected = (
            "https://name:password@api.deepseek.com",
            "https://api.deepseek.com?",
            "https://api.deepseek.com#",
            "javascript:alert(1)",
            "https://api.deepseek.com\n@evil.invalid",
        )
        for url in rejected:
            with self.subTest(url=url.split(":", 1)[0]):
                with self.assertRaises(ProviderError):
                    validate_base_url(url)

    def test_settings_precedence_is_default_then_file_env_then_cli(self) -> None:
        stored = WorkspaceConfig(
            provider="openai",
            model="stored-model",
            base_url="https://api.deepseek.com/v1",
            token_budget=32000,
            max_output_tokens=4096,
        )
        environment = {
            "HARNESS_PROVIDER": "openai",
            "HARNESS_MODEL": "env-model",
            "HARNESS_BASE_URL": "https://api.deepseek.com/v2",
            "HARNESS_TOKEN_BUDGET": "64000",
            "HARNESS_MAX_OUTPUT_TOKENS": "8192",
        }
        cli = SimpleNamespace(
            provider="openai",
            model="cli-model",
            base_url=None,
            token_budget=128000,
            max_output_tokens=None,
        )
        resolved = resolve_config(stored, True, cli, environment=environment)

        self.assertEqual(resolved.provider, "openai")
        self.assertEqual(resolved.model, "cli-model")
        self.assertEqual(resolved.base_url, "https://api.deepseek.com/v2")
        self.assertEqual(resolved.token_budget, 128000)
        self.assertEqual(resolved.max_output_tokens, 8192)
        self.assertEqual(resolved.sources["model"], "命令行")
        self.assertEqual(resolved.sources["base_url"], "环境变量 HARNESS_BASE_URL")
        self.assertEqual(resolved.sources["max_output_tokens"], "环境变量 HARNESS_MAX_OUTPUT_TOKENS")

        defaults = resolve_config(
            WorkspaceConfig(),
            False,
            SimpleNamespace(provider=None, model=None, base_url=None, token_budget=None, max_output_tokens=None),
            environment={},
        )
        self.assertEqual(defaults.provider, "mock")
        self.assertEqual(defaults.model, "mock")
        self.assertIsNone(defaults.base_url)
        self.assertEqual(defaults.sources["provider"], "默认")

        openai_default = resolve_config(
            WorkspaceConfig(provider="openai"),
            True,
            SimpleNamespace(provider=None, model=None, base_url=None, token_budget=None, max_output_tokens=None),
            environment={},
        )
        self.assertEqual(openai_default.model, "deepseek-flash")
        self.assertEqual(openai_default.base_url, "https://api.deepseek.com")

        anthropic_compat_default = resolve_config(
            WorkspaceConfig(provider="deepseek-anthropic"),
            True,
            SimpleNamespace(provider=None, model=None, base_url=None, token_budget=None, max_output_tokens=None),
            environment={},
        )
        self.assertEqual(anthropic_compat_default.provider, "deepseek-anthropic")
        self.assertEqual(anthropic_compat_default.model, "deepseek-flash")
        self.assertEqual(anthropic_compat_default.base_url, "https://api.deepseek.com/anthropic")

    def test_deepseek_anthropic_config_rejects_claude_aliases_and_other_routes(self) -> None:
        with self.assertRaisesRegex(ConfigError, "不接受 Claude 模型别名"):
            validate_workspace_config(WorkspaceConfig(provider="deepseek-anthropic", model="claude-sonnet-4-5"))
        for base_url in (
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
            "https://example.invalid/anthropic",
            "https://api.deepseek.com:444/anthropic",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ConfigError):
                    validate_workspace_config(WorkspaceConfig(provider="deepseek-anthropic", base_url=base_url))

    def test_environment_and_cli_errors_do_not_echo_values(self) -> None:
        secret = "sk-" + "B" * 24
        cli = SimpleNamespace(provider=None, model=None, base_url=None, token_budget=None, max_output_tokens=None)
        with self.assertRaises(ConfigError) as caught:
            resolve_config(WorkspaceConfig(), False, cli, environment={"HARNESS_MODEL": secret})
        self.assertNotIn(secret, str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            resolve_config(WorkspaceConfig(), False, cli, environment={"HARNESS_TOKEN_BUDGET": secret})
        self.assertNotIn(secret, str(caught.exception))

    def test_cli_rejects_credentials_in_base_url_without_echo_or_provider_creation(self) -> None:
        secret = "LOCAL_URL_SECRET_SENTINEL"
        with tempfile.TemporaryDirectory(prefix="cvfx-cli-config-") as workspace:
            args = SimpleNamespace(
                workspace=workspace,
                provider=None,
                model=None,
                base_url=f"https://user:{secret}@api.deepseek.com",
                token_budget=None,
                max_output_tokens=None,
                ignore_config=False,
            )
            outputs: list[str] = []
            with patch("cvfx_harness.cli.provider_from_environment") as factory:
                result = run_console(args, output=outputs.append)
            factory.assert_not_called()
        self.assertEqual(result, 2)
        self.assertNotIn(secret, "\n".join(outputs))
        self.assertTrue(any("不得含凭据" in line for line in outputs))

    def test_anthropic_is_rejected_with_clear_unsupported_message(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cvfx-cli-config-") as workspace:
            args = SimpleNamespace(workspace=workspace, provider="anthropic", ignore_config=False)
            outputs: list[str] = []
            with patch("cvfx_harness.cli.provider_from_environment") as factory:
                result = run_console(args, output=outputs.append)
            factory.assert_not_called()
        self.assertEqual(result, 2)
        self.assertIn("未接入官方 Claude 服务", "\n".join(outputs))
        self.assertIn("deepseek-anthropic", "\n".join(outputs))

    def test_chinese_config_commands_persist_without_saving_a_key(self) -> None:
        inputs = iter(
            [
                "/配置",
                "/配置 提供商 openai",
                "/配置 模型 deepseek-flash",
                "/配置 端点 https://api.deepseek.com/v1",
                "/配置 预算 32000",
                "/配置 输出上限 4096",
                "/退出",
            ]
        )
        outputs: list[str] = []
        args = SimpleNamespace(workspace=self.temp.name, ignore_config=False)
        harness_environment_keys = (
            "HARNESS_PROVIDER",
            "HARNESS_MODEL",
            "HARNESS_BASE_URL",
            "HARNESS_TOKEN_BUDGET",
            "HARNESS_MAX_OUTPUT_TOKENS",
        )
        with patch.dict(os.environ, {}, clear=False):
            for name in harness_environment_keys:
                os.environ.pop(name, None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()) as factory:
                result = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)
                startup_inputs = iter(["/配置", "/退出"])
                startup_outputs: list[str] = []
                startup_result = run_console(
                    args,
                    input_fn=lambda _prompt: next(startup_inputs),
                    output=startup_outputs.append,
                )

        loaded, exists = self.store.load()
        self.assertEqual(result, 0)
        self.assertEqual(startup_result, 0)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(
            factory.call_args_list[1].args,
            ("openai", "deepseek-flash"),
        )
        self.assertEqual(
            factory.call_args_list[1].kwargs["base_url_override"],
            "https://api.deepseek.com/v1",
        )
        self.assertTrue(exists)
        self.assertEqual(loaded.provider, "openai")
        self.assertEqual(loaded.model, "deepseek-flash")
        self.assertEqual(loaded.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(loaded.token_budget, 32000)
        self.assertEqual(loaded.max_output_tokens, 4096)
        self.assertNotIn("DEEPSEEK_API_KEY", self.path.read_text(encoding="utf-8"))
        self.assertIn("下次启动设置", "\n".join(outputs))
        self.assertIn("密钥不会写入配置文件", "\n".join(outputs))

    def test_chinese_config_selects_deepseek_anthropic_without_persisting_credentials(self) -> None:
        inputs = iter([
            "/配置 提供商 deepseek-anthropic",
            "/配置 模型 deepseek-v4-pro",
            "/配置 端点 https://api.deepseek.com/anthropic",
            "/配置",
            "/帮助",
            "/退出",
        ])
        outputs: list[str] = []
        args = SimpleNamespace(workspace=self.temp.name, ignore_config=False)
        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "HARNESS_PROVIDER", "HARNESS_MODEL", "HARNESS_BASE_URL",
                "HARNESS_TOKEN_BUDGET", "HARNESS_MAX_OUTPUT_TOKENS",
            ):
                os.environ.pop(name, None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                result = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)
        loaded, exists = self.store.load()
        serialized = self.path.read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertTrue(exists)
        self.assertEqual(loaded.provider, "deepseek-anthropic")
        self.assertEqual(loaded.model, "deepseek-v4-pro")
        self.assertEqual(loaded.base_url, "https://api.deepseek.com/anthropic")
        self.assertNotIn("api_key", serialized.casefold())
        self.assertNotIn("ANTHROPIC_API_KEY", serialized)
        self.assertIn("DeepSeek Anthropic Messages 兼容", "\n".join(outputs))
        self.assertIn("DeepSeek Anthropic 兼容模式使用各自协议的有界 SSE", "\n".join(outputs))
        self.assertIn("Claude", "\n".join(outputs))

    def test_interactive_editor_reviews_and_switches_provider_as_one_change(self) -> None:
        self.store.save(
            WorkspaceConfig(
                provider="openai",
                model="deepseek-flash",
                base_url="https://api.deepseek.com/v1",
                token_budget=24000,
            )
        )
        answers = iter(
            ["/配置 编辑", "deepseek-anthropic", "", "", "", "", "F2=帮助，F4=状态", "是", "/退出"]
        )
        outputs: list[str] = []
        args = build_parser().parse_args(["--workspace", self.temp.name])
        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "HARNESS_PROVIDER", "HARNESS_MODEL", "HARNESS_BASE_URL",
                "HARNESS_TOKEN_BUDGET", "HARNESS_MAX_OUTPUT_TOKENS",
            ):
                os.environ.pop(name, None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                result = run_console(
                    args,
                    input_fn=lambda _prompt: next(answers),
                    output=outputs.append,
                )

        saved, exists = self.store.load()
        joined = "\n".join(outputs)
        self.assertEqual(result, 0)
        self.assertTrue(exists)
        self.assertEqual(saved.provider, "deepseek-anthropic")
        self.assertIsNone(saved.model)
        self.assertIsNone(saved.base_url)
        self.assertEqual(saved.token_budget, 24000)
        self.assertEqual(saved.tui_shortcuts, {"F2": "help", "F4": "status"})
        effective = resolve_config(
            saved,
            exists,
            SimpleNamespace(provider=None, model=None, base_url=None, token_budget=None, max_output_tokens=None),
            environment={},
        )
        self.assertEqual(effective.model, "deepseek-flash")
        self.assertEqual(effective.base_url, "https://api.deepseek.com/anthropic")
        self.assertIn("待保存工作区设置（尚未写入）", joined)
        self.assertIn("确认保存后的生效值", joined)
        self.assertIn("下次启动", joined)

    def test_single_field_provider_switch_resets_incompatible_protocol_defaults(self) -> None:
        self.store.save(
            WorkspaceConfig(
                provider="openai",
                model="custom-model",
                base_url="https://api.deepseek.com/v1",
            )
        )
        answers = iter(["/配置 提供商 deepseek-anthropic", "/退出"])
        outputs: list[str] = []
        args = build_parser().parse_args(["--workspace", self.temp.name])
        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "HARNESS_PROVIDER", "HARNESS_MODEL", "HARNESS_BASE_URL",
                "HARNESS_TOKEN_BUDGET", "HARNESS_MAX_OUTPUT_TOKENS",
            ):
                os.environ.pop(name, None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                result = run_console(args, input_fn=lambda _prompt: next(answers), output=outputs.append)

        loaded, exists = self.store.load()
        self.assertEqual(result, 0)
        self.assertTrue(exists)
        self.assertEqual(loaded.provider, "deepseek-anthropic")
        self.assertIsNone(loaded.model)
        self.assertIsNone(loaded.base_url)
        self.assertIn("模型和 Base URL 已重置", "\n".join(outputs))

    def test_interactive_editor_cancellation_and_protocol_error_leave_file_unchanged(self) -> None:
        original = WorkspaceConfig(
            provider="openai",
            model="deepseek-flash",
            base_url="https://api.deepseek.com/v1",
            token_budget=22000,
        )
        self.store.save(original)
        original_bytes = self.path.read_bytes()
        args = build_parser().parse_args(["--workspace", self.temp.name])

        for answers in (
            ["/配置 编辑", "/取消", "/退出"],
            ["/配置 编辑", "deepseek-anthropic", "", "https://api.deepseek.com/v1", "", "", "", "/退出"],
        ):
            outputs: list[str] = []
            answer_iter = iter(answers)
            with patch.dict(os.environ, {}, clear=False):
                for name in (
                    "HARNESS_PROVIDER", "HARNESS_MODEL", "HARNESS_BASE_URL",
                    "HARNESS_TOKEN_BUDGET", "HARNESS_MAX_OUTPUT_TOKENS",
                ):
                    os.environ.pop(name, None)
                with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                    result = run_console(
                        args,
                        input_fn=lambda _prompt: next(answer_iter),
                        output=outputs.append,
                    )
            self.assertEqual(result, 0)
            self.assertEqual(self.path.read_bytes(), original_bytes)
            self.assertEqual(self.store.load()[0], original)

        self.assertTrue(any("仅允许 HTTPS api.deepseek.com:443/anthropic" in line for line in outputs))

    def test_shortcut_editor_conflict_and_late_cancel_leave_existing_file_unchanged(self) -> None:
        original = WorkspaceConfig(tui_shortcuts={"F2": "help"})
        self.store.save(original)
        original_bytes = self.path.read_bytes()
        args = build_parser().parse_args(["--workspace", self.temp.name])

        for answers in (
            ["/配置 编辑", "", "", "", "", "", "F2=帮助,F2=状态", "/退出"],
            ["/配置 编辑", "", "", "", "", "", "/取消", "/退出"],
        ):
            answer_iter = iter(answers)
            outputs: list[str] = []
            with patch.dict(os.environ, {}, clear=False):
                for name in (
                    "HARNESS_PROVIDER", "HARNESS_MODEL", "HARNESS_BASE_URL",
                    "HARNESS_TOKEN_BUDGET", "HARNESS_MAX_OUTPUT_TOKENS",
                ):
                    os.environ.pop(name, None)
                with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                    result = run_console(
                        args,
                        input_fn=lambda _prompt: next(answer_iter),
                        output=outputs.append,
                    )
            self.assertEqual(result, 0)
            self.assertEqual(self.path.read_bytes(), original_bytes)
            self.assertEqual(self.store.load()[0], original)

    def test_interactive_editor_does_not_overwrite_changes_made_while_prompting(self) -> None:
        args = build_parser().parse_args(["--workspace", self.temp.name])
        external = WorkspaceConfig(model="external-edit")
        answers = iter(["/配置 编辑", "openai", "", "", "", "", "", "/退出"])
        outputs: list[str] = []

        def input_with_competing_edit(prompt: str) -> str:
            if prompt.startswith("确认保存"):
                self.store.save(external)
                return "是"
            return next(answers)

        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "HARNESS_PROVIDER", "HARNESS_MODEL", "HARNESS_BASE_URL",
                "HARNESS_TOKEN_BUDGET", "HARNESS_MAX_OUTPUT_TOKENS",
            ):
                os.environ.pop(name, None)
            with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
                result = run_console(args, input_fn=input_with_competing_edit, output=outputs.append)

        self.assertEqual(result, 0)
        self.assertEqual(self.store.load()[0], external)
        self.assertIn("配置文件已被其他来源修改", "\n".join(outputs))

    def test_plain_chat_key_is_discarded_before_provider_call(self) -> None:
        secret = "sk-" + "D" * 24
        answers = iter([secret, "/退出"])
        outputs: list[str] = []
        args = build_parser().parse_args(["--workspace", self.temp.name])
        with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
            with patch("cvfx_harness.cli.Agent.ask") as ask:
                result = run_console(
                    args,
                    input_fn=lambda _prompt: next(answers),
                    output=outputs.append,
                )
        ask.assert_not_called()
        self.assertEqual(result, 0)
        self.assertNotIn(secret, "\n".join(outputs))
        self.assertIn("不会显示、发送给模型或保存", "\n".join(outputs))

    def test_interactive_config_never_accepts_or_echoes_a_key(self) -> None:
        secret = "sk-" + "E" * 24
        answers = iter(["/配置 编辑", "openai", secret, "/退出"])
        outputs: list[str] = []
        args = build_parser().parse_args(["--workspace", self.temp.name])
        with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
            result = run_console(
                args,
                input_fn=lambda _prompt: next(answers),
                output=outputs.append,
            )

        self.assertEqual(result, 0)
        self.assertFalse(self.path.exists())
        self.assertNotIn(secret, "\n".join(outputs))
        self.assertIn("拒绝疑似凭据输入", "\n".join(outputs))

    def test_corrupt_file_can_be_bypassed_without_being_read_or_overwritten(self) -> None:
        self.path.parent.mkdir(parents=True)
        broken = '{ "api_key": "sk-' + "F" * 24 + '"'
        self.path.write_text(broken, encoding="utf-8")
        inputs = iter(["/退出"])
        outputs: list[str] = []
        args = SimpleNamespace(workspace=self.temp.name, ignore_config=True)
        with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
            with patch.object(Path, "read_bytes", side_effect=AssertionError("ignored config must not be read")) as read_bytes:
                result = run_console(
                    args, input_fn=lambda _prompt: next(inputs), output=outputs.append
                )
                read_bytes.assert_not_called()

        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)


class ConfigParserTests(unittest.TestCase):
    def test_config_completer_offers_editor_and_only_supported_providers(self) -> None:
        self.assertIn("编辑", _completion_matches("/配置 编辑", "编辑"))
        self.assertEqual(_completion_matches("/快", "/快"), ("/快捷键",))
        self.assertEqual(_completion_matches("/按", "/按"), ("/按键",))
        self.assertEqual(
            _completion_matches("/配置 提供商 deepseek-a", "deepseek-a"),
            ("deepseek-anthropic",),
        )

    def test_help_documents_supported_providers_and_persistence(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("deepseek-anthropic", help_text)
        self.assertIn("仅连接", help_text)
        self.assertIn("--ignore-config", help_text)

    def test_invalid_numeric_cli_value_is_not_echoed(self) -> None:
        secret = "sk-" + "C" * 24
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            build_parser().parse_args(["--token-budget", secret])
        self.assertNotIn(secret, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
