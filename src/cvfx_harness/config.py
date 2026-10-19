from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .agent import DEFAULT_MAX_OUTPUT_TOKENS
from .providers import ProviderError, validate_deepseek_base_url
from .sessions import contains_likely_secret
from .tools import Workspace, WorkspaceError
from .usage import DEFAULT_TOKEN_BUDGET

CONFIG_VERSION = 1
CONFIG_RELATIVE_PATH = ".harness/config/settings.json"
MAX_CONFIG_BYTES = 16 * 1024
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CONFIG_KEYS = {
    "version",
    "provider",
    "model",
    "base_url",
    "token_budget",
    "max_output_tokens",
}


class ConfigError(RuntimeError):
    """A user-safe workspace configuration error; input values are never included."""


@dataclass(frozen=True)
class WorkspaceConfig:
    provider: str = "mock"
    model: str | None = None
    base_url: str | None = None
    token_budget: int = DEFAULT_TOKEN_BUDGET
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "token_budget": self.token_budget,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class ResolvedConfig:
    provider: str
    model: str
    base_url: str | None
    token_budget: int
    max_output_tokens: int
    sources: dict[str, str]


def _validate_provider(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("提供商配置无效；当前仅支持 mock 和 DeepSeek OpenAI 兼容模式。")
    normalized = value.casefold()
    if normalized == "anthropic":
        raise ConfigError("Anthropic provider 尚未实现；当前仅支持 mock 和 DeepSeek OpenAI 兼容模式。")
    if normalized not in {"mock", "openai"}:
        raise ConfigError("提供商配置无效；当前仅支持 mock 和 DeepSeek OpenAI 兼容模式。")
    return normalized


def _validate_model(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or _MODEL_NAME.fullmatch(value) is None
        or contains_likely_secret(value)
    ):
        raise ConfigError("模型名称无效；请只填写模型标识，不要填写 URL、凭据或其他参数。")
    return value


def _validate_base_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or contains_likely_secret(value):
        raise ConfigError("Base URL 无效；当前仅支持不含凭据的 DeepSeek HTTPS 服务地址。")
    try:
        validated = validate_deepseek_base_url(value)
    except ProviderError:
        raise ConfigError("Base URL 无效；当前仅支持 HTTPS api.deepseek.com:443，且不得含凭据、查询参数或片段。") from None
    return validated


def validate_workspace_config(config: WorkspaceConfig) -> WorkspaceConfig:
    if not isinstance(config, WorkspaceConfig):
        raise ConfigError("工作区配置格式无效。")
    provider = _validate_provider(config.provider)
    model = _validate_model(config.model)
    base_url = _validate_base_url(config.base_url)
    if type(config.token_budget) is not int or not 1024 <= config.token_budget <= 1_000_000:
        raise ConfigError("Token 预算必须在 1024 到 1000000 之间。")
    if type(config.max_output_tokens) is not int or not 1 <= config.max_output_tokens <= 65536:
        raise ConfigError("输出 Token 上限必须在 1 到 65536 之间。")
    return WorkspaceConfig(provider, model, base_url, config.token_budget, config.max_output_tokens)


class WorkspaceConfigStore:
    """Persist only validated, non-secret settings inside Harness private state."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _path(self, *, create: bool) -> Path | None:
        try:
            directory = self.workspace.internal_storage_dir("config", create=create)
        except WorkspaceError:
            raise ConfigError("工作区配置目录不可用；未读取或修改配置文件。") from None
        return None if directory is None else directory / "settings.json"

    def _check_file(self, path: Path) -> bool:
        try:
            if self.workspace._is_reparse_point(path):
                raise ConfigError("配置文件不能是 symlink/junction。")
            if not path.exists():
                return False
            info = path.stat()
            if not path.is_file() or getattr(info, "st_nlink", 1) > 1:
                raise ConfigError("配置路径必须是普通文件且不能有多个硬链接。")
            if info.st_size > MAX_CONFIG_BYTES:
                raise ConfigError("工作区配置超过 16 KiB 上限。")
            return True
        except ConfigError:
            raise
        except (OSError, WorkspaceError):
            raise ConfigError("无法安全检查工作区配置文件。") from None

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError("工作区配置包含重复字段。")
            result[key] = value
        return result

    def load(self) -> tuple[WorkspaceConfig, bool]:
        path = self._path(create=False)
        if path is None or not self._check_file(path):
            return WorkspaceConfig(), False
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_CONFIG_BYTES:
                raise ConfigError("工作区配置超过 16 KiB 上限。")
            if contains_likely_secret(raw.decode("utf-8")):
                raise ConfigError("配置文件疑似含有凭据；为避免泄露已拒绝读取。请移除凭据后重试。")
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=self._unique_json_object
            )
        except ConfigError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ConfigError("工作区配置损坏或不可读取；没有使用其中的设置。") from None
        if not isinstance(payload, dict) or set(payload) - _CONFIG_KEYS:
            raise ConfigError("工作区配置包含不支持的字段；只允许非敏感运行设置。")
        if type(payload.get("version")) is not int or payload["version"] != CONFIG_VERSION:
            raise ConfigError("工作区配置版本无效；请备份后使用 --ignore-config 启动并重新保存。")
        try:
            config = WorkspaceConfig(
                provider=payload.get("provider", "mock"),
                model=payload.get("model"),
                base_url=payload.get("base_url"),
                token_budget=payload.get("token_budget", DEFAULT_TOKEN_BUDGET),
                max_output_tokens=payload.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            )
            return validate_workspace_config(config), True
        except ConfigError:
            raise
        except (TypeError, ValueError):
            raise ConfigError("工作区配置格式无效；没有使用其中的设置。") from None

    def save(self, config: WorkspaceConfig) -> None:
        validated = validate_workspace_config(config)
        path = self._path(create=True)
        assert path is not None
        self._check_file(path)
        encoded = json.dumps(validated.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_CONFIG_BYTES or contains_likely_secret(encoded.decode("utf-8")):
            raise ConfigError("配置未保存：内容超出上限或疑似含有凭据。")
        try:
            self.workspace._atomic_write_bytes(path, encoded)
        except WorkspaceError:
            raise ConfigError("配置保存失败；请检查工作区内部状态目录权限。") from None


def resolve_config(
    stored: WorkspaceConfig,
    config_exists: bool,
    cli: Any,
    *,
    environment: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Resolve defaults < workspace file < environment < CLI without reading API keys."""
    env = os.environ if environment is None else environment
    sources: dict[str, str] = {}

    def pick(name: str, default: Any, stored_value: Any, env_key: str | None = None) -> Any:
        if config_exists:
            value, source = stored_value, "工作区配置"
        else:
            value, source = default, "默认"
        if env_key is not None and env.get(env_key) not in {None, ""}:
            value, source = env[env_key], f"环境变量 {env_key}"
        cli_value = getattr(cli, name, None)
        if cli_value is not None:
            value, source = cli_value, "命令行"
        sources[name] = source
        return value

    provider = _validate_provider(
        pick("provider", "mock", stored.provider, "HARNESS_PROVIDER")
    )
    model_default = "deepseek-flash" if provider == "openai" else "mock"
    model = _validate_model(
        pick("model", model_default, stored.model or model_default, "HARNESS_MODEL")
    )
    assert model is not None
    base_default = DEFAULT_DEEPSEEK_BASE_URL if provider == "openai" else None
    base_url = _validate_base_url(
        pick("base_url", base_default, stored.base_url or base_default, "HARNESS_BASE_URL")
    )

    token_budget_value = pick(
        "token_budget", DEFAULT_TOKEN_BUDGET, stored.token_budget, "HARNESS_TOKEN_BUDGET"
    )
    if isinstance(token_budget_value, str):
        try:
            token_budget_value = int(token_budget_value)
        except ValueError:
            raise ConfigError("HARNESS_TOKEN_BUDGET/--token-budget 必须是 1024 到 1000000 的整数。") from None
    if type(token_budget_value) is not int or not 1024 <= token_budget_value <= 1_000_000:
        raise ConfigError("Token 预算必须在 1024 到 1000000 之间。")

    output_value = pick(
        "max_output_tokens",
        DEFAULT_MAX_OUTPUT_TOKENS,
        stored.max_output_tokens,
        "HARNESS_MAX_OUTPUT_TOKENS",
    )
    if isinstance(output_value, str):
        try:
            output_value = int(output_value)
        except ValueError:
            raise ConfigError("HARNESS_MAX_OUTPUT_TOKENS/--max-output-tokens 必须是 1 到 65536 的整数。") from None
    if type(output_value) is not int or not 1 <= output_value <= 65536:
        raise ConfigError("输出 Token 上限必须在 1 到 65536 之间。")

    return ResolvedConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        token_budget=token_budget_value,
        max_output_tokens=output_value,
        sources=sources,
    )
