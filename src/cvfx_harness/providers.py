from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit


class ProviderError(RuntimeError):
    """A user-safe provider error. Never includes request headers or bodies."""


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantTurn:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_message: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    label: str

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn: ...


class MockProvider:
    """Deterministic offline provider used by default and by smoke tests."""

    label = "mock"

    def __init__(self, model: str = "mock") -> None:
        self.model = model

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        del tools
        last_user = next(
            (item.get("content", "") for item in reversed(messages) if item.get("role") == "user"),
            "",
        )
        answer = f"[模拟模式] 已收到：{last_user[:400]}"
        return AssistantTurn(
            content=answer,
            assistant_message={"role": "assistant", "content": answer},
        )


class OpenAICompatibleProvider:
    """Small OpenAI Chat Completions adapter using only the Python stdlib."""

    label = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 45.0,
    ) -> None:
        if not api_key:
            raise ProviderError(
                "未配置 DEEPSEEK_API_KEY。请按 README 的本地安全录入步骤设置后重试。"
            )
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderError("Base URL 格式无效；请使用 HTTPS 服务地址。")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ProviderError("仅允许 HTTPS；HTTP 只对本机回环地址开放。")

        self.endpoint = self._chat_endpoint(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @staticmethod
    def _chat_endpoint(base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2048,
            "reasoning_effort": "high",
            "thinking": {"type": "enabled"},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            # Provider response bodies are intentionally omitted: they are untrusted
            # and might reflect sensitive request data.
            raise ProviderError(f"模型服务返回 HTTP {exc.code}。") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            reason_text = str(reason) if reason else type(exc).__name__
            raise ProviderError(f"连接模型服务失败：{reason_text}") from None

        if len(raw) > 4 * 1024 * 1024:
            raise ProviderError("模型响应超过 4 MiB 上限。")
        try:
            payload = json.loads(raw.decode("utf-8"))
            message = payload["choices"][0]["message"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise ProviderError("模型响应格式无效。") from None

        content_value = message.get("content")
        content = content_value if isinstance(content_value, str) else ""
        raw_calls = message.get("tool_calls") or []
        calls: list[ToolCall] = []
        for index, raw_call in enumerate(raw_calls):
            try:
                function = raw_call["function"]
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                calls.append(
                    ToolCall(
                        call_id=str(raw_call.get("id") or f"call-{index}"),
                        name=str(function["name"]),
                        arguments=arguments,
                    )
                )
            except (KeyError, TypeError, json.JSONDecodeError, ValueError):
                # Preserve the assistant response but surface malformed tool calls
                # as an ordinary answer; never try to execute partial arguments.
                raise ProviderError("模型返回了格式无效的工具调用。") from None

        assistant_message: dict[str, Any] = {"role": "assistant", "content": content_value}
        if message.get("reasoning_content") is not None:
            assistant_message["reasoning_content"] = message["reasoning_content"]
        if raw_calls:
            assistant_message["tool_calls"] = raw_calls
        return AssistantTurn(
            content=content,
            tool_calls=calls,
            assistant_message=assistant_message,
        )


def provider_from_environment(provider_name: str, model_override: str | None = None) -> Provider:
    model = model_override or os.environ.get("HARNESS_MODEL", "deepseek-flash")
    if provider_name == "mock":
        return MockProvider(model=model if model_override else "mock")
    if provider_name == "openai":
        base_url = os.environ.get("HARNESS_BASE_URL", "https://api.deepseek.com")
        return OpenAICompatibleProvider(
            base_url=base_url,
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            model=model,
        )
    raise ProviderError(f"未知模型提供商：{provider_name}")
