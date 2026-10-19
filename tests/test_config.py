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

from cvfx_harness.cli import build_parser, run_console
from cvfx_harness.config import (
    CONFIG_RELATIVE_PATH,
    ConfigError,
    WorkspaceConfig,
    WorkspaceConfigStore,
    resolve_config,
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
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

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
        self.assertIn("Anthropic provider 尚未实现", "\n".join(outputs))

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

    def test_corrupt_file_can_be_bypassed_without_being_read_or_overwritten(self) -> None:
        self.path.parent.mkdir(parents=True)
        broken = "{ definitely not json"
        self.path.write_text(broken, encoding="utf-8")
        inputs = iter(["/退出"])
        outputs: list[str] = []
        args = SimpleNamespace(workspace=self.temp.name, ignore_config=True)
        with patch("cvfx_harness.cli.provider_from_environment", return_value=MockProvider()):
            result = run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append)

        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)


class ConfigParserTests(unittest.TestCase):
    def test_help_documents_supported_providers_and_persistence(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("仅 DeepSeek 兼容模式", help_text)
        self.assertIn("--ignore-config", help_text)

    def test_invalid_numeric_cli_value_is_not_echoed(self) -> None:
        secret = "sk-" + "C" * 24
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            build_parser().parse_args(["--token-budget", secret])
        self.assertNotIn(secret, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
