from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from .usage import ProviderUsage, estimate_tokens


class ProviderError(RuntimeError):
    """A user-safe provider error. Never includes request headers or bodies."""


class ProviderCancelled(RuntimeError):
    """Raised when a caller cancels a provider operation at a safe boundary."""


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
    usage: ProviderUsage | None = None


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

    def complete_subagent(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout_seconds: float,
        max_output_tokens: int,
        cancellation_event: threading.Event,
    ) -> AssistantTurn:
        if cancellation_event.is_set():
            raise ProviderCancelled("subagent cancelled")
        if timeout_seconds <= 0:
            raise TimeoutError("subagent deadline elapsed")
        turn = self.complete(messages, [])
        content = turn.content
        while content and estimate_tokens(content) > max_output_tokens:
            content = content[:-1]
        if content != turn.content:
            turn.content = content
            turn.assistant_message = {"role": "assistant", "content": content}
        if cancellation_event.is_set():
            raise ProviderCancelled("subagent cancelled")
        return turn


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
            raise ProviderError("未提供 API 密钥。")
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
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        cancellation_event: threading.Event | None = None,
        max_tokens: int = 2048,
    ) -> AssistantTurn:
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider request cancelled before send")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 65536:
            raise ProviderError("max_tokens must be between 1 and 65536")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
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
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("连接模型服务失败；请检查网络和 Base URL。") from None

        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider request cancelled after response")

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
            usage=self._parse_usage(payload.get("usage")),
        )

    @staticmethod
    def _parse_usage(value: Any) -> ProviderUsage | None:
        if not isinstance(value, dict):
            return None
        prompt = value.get("prompt_tokens")
        completion = value.get("completion_tokens")
        total = value.get("total_tokens")
        if type(prompt) is not int or type(completion) is not int:
            return None
        if prompt < 0 or completion < 0:
            return None
        if type(total) is not int or total < 0:
            total = prompt + completion
        return ProviderUsage(prompt, completion, total)


def provider_from_environment(
    provider_name: str,
    model_override: str | None = None,
    *,
    api_key_override: str | None = None,
    base_url_override: str | None = None,
) -> Provider:
    model = model_override or os.environ.get("HARNESS_MODEL", "deepseek-flash")
    if provider_name == "mock":
        return MockProvider(model=model if model_override else "mock")
    if provider_name == "openai":
        base_url = base_url_override or os.environ.get("HARNESS_BASE_URL", "https://api.deepseek.com")
        # Do not silently consume a process-wide credential. A later interactive
        # hidden-input flow must pass the one-shot value explicitly.
        api_key = api_key_override or ""
        return OpenAICompatibleProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
    raise ProviderError(f"未知模型提供商：{provider_name}")
