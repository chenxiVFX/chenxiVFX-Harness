from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .agent import DEFAULT_MAX_OUTPUT_TOKENS
from .providers import (
    DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_ANTHROPIC_MODELS,
    ProviderError,
    validate_deepseek_anthropic_base_url,
    validate_deepseek_base_url,
)
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
    "tui_shortcuts",
}
_SAVE_WITHOUT_REVISION_CHECK = object()

TUI_SHORTCUT_ACTIONS = {
    "help": "帮助",
    "commands": "命令",
    "status": "状态",
    "config": "配置",
}
_TUI_SHORTCUT_ACTION_ALIASES = {
    alias: action
    for action, aliases in {
        "help": ("帮助", "help"),
        "commands": ("命令", "commands"),
        "status": ("状态", "status"),
        "config": ("配置", "config"),
    }.items()
    for alias in aliases
}
_TUI_SHORTCUT_KEY = re.compile(r"^F([2-8])$", re.IGNORECASE)


def validate_tui_shortcuts(value: Any) -> dict[str, str]:
    """Accept only a small map of function keys to local read-only commands."""
    if not isinstance(value, Mapping) or len(value) > len(TUI_SHORTCUT_ACTIONS):
        raise ConfigError("快捷键配置无效；最多可设置 4 个受支持的 TUI 快捷键。")
    normalized: dict[str, str] = {}
    used_actions: set[str] = set()
    for raw_key, raw_action in value.items():
        if not isinstance(raw_key, str) or _TUI_SHORTCUT_KEY.fullmatch(raw_key) is None:
            raise ConfigError("快捷键仅支持 F2–F8；Enter、Esc、Ctrl+C、Tab 与导航键不可映射。")
        key = raw_key.upper()
        if key in normalized:
            raise ConfigError("快捷键重复；每个按键只能绑定一个动作。")
        if not isinstance(raw_action, str):
            raise ConfigError("快捷键动作无效；仅支持 帮助、命令、状态、配置。")
        action_name = raw_action.strip().removeprefix("/").casefold()
        action = _TUI_SHORTCUT_ACTION_ALIASES.get(action_name)
        if action is None:
            raise ConfigError("快捷键动作无效；仅支持 帮助、命令、状态、配置。")
        if action in used_actions:
            raise ConfigError("快捷键动作重复；帮助、命令、状态、配置各只能绑定一次。")
        normalized[key] = action
        used_actions.add(action)
    return dict(sorted(normalized.items(), key=lambda item: int(item[0][1:])))


def parse_tui_shortcuts(value: str) -> dict[str, str]:
    """Parse the reviewed editor's compact F-key=Chinese-action syntax."""
    if not isinstance(value, str):
        raise ConfigError("快捷键格式无效；示例：F2=帮助,F4=状态。")
    if not value.strip():
        return {}
    mappings: dict[str, str] = {}
    for item in re.split(r"[,，]", value):
        key, separator, action = item.partition("=")
        if not separator or not key.strip() or not action.strip():
            raise ConfigError("快捷键格式无效；请使用 F2=帮助,F4=状态 这样的逗号分隔映射。")
        normalized_key = key.strip().upper()
        if normalized_key in mappings:
            raise ConfigError("快捷键重复；每个按键只能绑定一个动作。")
        mappings[normalized_key] = action.strip()
    return validate_tui_shortcuts(mappings)


def format_tui_shortcuts(value: Any) -> str:
    shortcuts = validate_tui_shortcuts(value)
    if not shortcuts:
        return "未设置"
    return "，".join(
        f"{key}={TUI_SHORTCUT_ACTIONS[action]}"
        for key, action in shortcuts.items()
    )


class ConfigError(RuntimeError):
    """A user-safe workspace configuration error; input values are never included."""


@dataclass(frozen=True)
class WorkspaceConfig:
    provider: str = "mock"
    model: str | None = None
    base_url: str | None = None
    token_budget: int = DEFAULT_TOKEN_BUDGET
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    tui_shortcuts: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "token_budget": self.token_budget,
            "max_output_tokens": self.max_output_tokens,
            "tui_shortcuts": validate_tui_shortcuts(self.tui_shortcuts),
        }


@dataclass(frozen=True)
class ResolvedConfig:
    provider: str
    model: str
    base_url: str | None
    token_budget: int
    max_output_tokens: int
    tui_shortcuts: dict[str, str]
    sources: dict[str, str]


def _validate_provider(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("提供商配置无效；请选择 mock、openai 或 deepseek-anthropic。")
    normalized = value.casefold()
    if normalized == "anthropic":
        raise ConfigError(
            "未接入官方 Claude 服务；如使用 DeepSeek Anthropic 兼容入口，请选择 deepseek-anthropic。"
        )
    if normalized not in {"mock", "openai", "deepseek-anthropic"}:
        raise ConfigError("提供商配置无效；请选择 mock、openai 或 deepseek-anthropic。")
    return normalized


def _validate_model(value: Any, *, provider: str | None = None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or _MODEL_NAME.fullmatch(value) is None
        or contains_likely_secret(value)
    ):
        raise ConfigError("模型名称无效；请只填写模型标识，不要填写 URL、凭据或其他参数。")
    if provider == "deepseek-anthropic" and value not in DEEPSEEK_ANTHROPIC_MODELS:
        raise ConfigError("DeepSeek Anthropic 兼容模式只接受 deepseek-flash 或 deepseek-v4-pro；不接受 Claude 模型别名。")
    return value


def _validate_base_url(value: Any, *, provider: str | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or contains_likely_secret(value):
        raise ConfigError("Base URL 无效；当前仅支持不含凭据的 DeepSeek HTTPS 服务地址。")
    try:
        if provider == "deepseek-anthropic":
            validated = validate_deepseek_anthropic_base_url(value)
        else:
            validated = validate_deepseek_base_url(value)
    except ProviderError:
        if provider == "deepseek-anthropic":
            raise ConfigError("Base URL 无效；DeepSeek Anthropic 兼容模式仅允许 HTTPS api.deepseek.com:443/anthropic。") from None
        raise ConfigError("Base URL 无效；当前仅支持 HTTPS api.deepseek.com:443，且不得含凭据、查询参数或片段。") from None
    return validated


def validate_workspace_config(config: WorkspaceConfig) -> WorkspaceConfig:
    if not isinstance(config, WorkspaceConfig):
        raise ConfigError("工作区配置格式无效。")
    provider = _validate_provider(config.provider)
    model = _validate_model(config.model, provider=provider)
    base_url = _validate_base_url(config.base_url, provider=provider)
    if type(config.token_budget) is not int or not 1024 <= config.token_budget <= 1_000_000:
        raise ConfigError("Token 预算必须在 1024 到 1000000 之间。")
    if type(config.max_output_tokens) is not int or not 1 <= config.max_output_tokens <= 65536:
        raise ConfigError("输出 Token 上限必须在 1 到 65536 之间。")
    return WorkspaceConfig(
        provider,
        model,
        base_url,
        config.token_budget,
        config.max_output_tokens,
        validate_tui_shortcuts(config.tui_shortcuts),
    )


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

    def _revision_at_path(
        self, path: Path, *, metadata_only: bool = False
    ) -> str | tuple[str, ...] | None:
        if not self._check_file(path):
            return None
        try:
            if metadata_only:
                info = path.stat()
                return (
                    "stat",
                    str(info.st_dev),
                    str(info.st_ino),
                    str(info.st_size),
                    str(info.st_mtime_ns),
                    str(info.st_ctime_ns),
                    str(getattr(info, "st_nlink", 1)),
                )
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise ConfigError("无法安全检查工作区配置文件。") from None

    def revision(self) -> tuple[str, ...] | None:
        """Return metadata-only revision for files deliberately ignored at startup."""
        path = self._path(create=False)
        return None if path is None else self._revision_at_path(path, metadata_only=True)

    def load_snapshot(
        self,
    ) -> tuple[WorkspaceConfig, bool, str | tuple[str, ...] | None]:
        """Load validated settings together with a revision for optimistic updates."""
        path = self._path(create=False)
        if path is None or not self._check_file(path):
            return WorkspaceConfig(), False, None
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_CONFIG_BYTES:
                raise ConfigError("工作区配置超过 16 KiB 上限。")
            revision = hashlib.sha256(raw).hexdigest()
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
                tui_shortcuts=payload.get("tui_shortcuts", {}),
            )
            return validate_workspace_config(config), True, revision
        except ConfigError:
            raise
        except (TypeError, ValueError):
            raise ConfigError("工作区配置格式无效；没有使用其中的设置。") from None

    def load(self) -> tuple[WorkspaceConfig, bool]:
        config, exists, _revision = self.load_snapshot()
        return config, exists

    def save(
        self,
        config: WorkspaceConfig,
        *,
        expected_revision: str | tuple[str, ...] | None | object = _SAVE_WITHOUT_REVISION_CHECK,
    ) -> None:
        validated = validate_workspace_config(config)
        path = self._path(create=True)
        assert path is not None
        self._check_file(path)
        if expected_revision is not _SAVE_WITHOUT_REVISION_CHECK:
            metadata_only = (
                isinstance(expected_revision, tuple)
                and bool(expected_revision)
                and expected_revision[0] == "stat"
            )
            if self._revision_at_path(path, metadata_only=metadata_only) != expected_revision:
                raise ConfigError("配置文件已被其他来源修改；没有覆盖新配置，请重新查看后重试。")
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
    model_default = "deepseek-flash" if provider in {"openai", "deepseek-anthropic"} else "mock"
    model = _validate_model(
        pick("model", model_default, stored.model or model_default, "HARNESS_MODEL"),
        provider=provider,
    )
    assert model is not None
    base_default = (
        DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL
        if provider == "deepseek-anthropic"
        else DEFAULT_DEEPSEEK_BASE_URL if provider == "openai" else None
    )
    base_url = _validate_base_url(
        pick("base_url", base_default, stored.base_url or base_default, "HARNESS_BASE_URL"),
        provider=provider,
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

    shortcuts = validate_tui_shortcuts(stored.tui_shortcuts)
    sources["tui_shortcuts"] = "工作区配置" if config_exists else "默认"

    return ResolvedConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        token_budget=token_budget_value,
        max_output_tokens=output_value,
        tui_shortcuts=shortcuts,
        sources=sources,
    )
