from __future__ import annotations

import errno
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from .usage import ProviderUsage, estimate_tokens

DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
MAX_PROVIDER_TOOL_CALLS = 16
MAX_PROVIDER_TOOL_ARGUMENT_BYTES = 128 * 1024
MAX_STREAM_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_STREAM_LINE_BYTES = 512 * 1024
MAX_STREAM_EVENT_BYTES = 512 * 1024
MAX_STREAM_EVENTS = 16384
MAX_STREAM_ASSISTANT_BYTES = 2 * 1024 * 1024
MAX_STREAM_DURATION_SECONDS = 120.0
STREAM_READ_POLL_SECONDS = 0.25
MAX_SUBAGENT_TASK_CHARS = 240
MAX_SUBAGENT_TURNS = 2
MAX_SUBAGENT_TOOL_CALLS_PER_TURN = 2
MAX_SUBAGENT_INPUT_TOKENS = 512
MAX_SUBAGENT_OUTPUT_TOKENS = 256
MIN_SUBAGENT_TIMEOUT_SECONDS = 5.0
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 60.0
MAX_SUBAGENT_TIMEOUT_SECONDS = 120.0
MIN_MODEL_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS = 45.0
MAX_MODEL_REQUEST_TIMEOUT_SECONDS = MAX_STREAM_DURATION_SECONDS
_URL_CREDENTIAL_MARKER = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|authorization)(?:=|%3d)"
)


class ProviderError(RuntimeError):
    """A user-safe provider error. Never includes request headers or bodies."""


class ProviderCancelled(RuntimeError):
    """Raised when a caller cancels a provider operation at a safe boundary."""


class ProviderStreamError(ProviderError):
    """A bounded SSE transport or protocol error safe to show in the CLI."""

    def __init__(
        self,
        message: str,
        *,
        category: str | None = None,
        diagnostics: StreamDiagnostics | None = None,
    ) -> None:
        self.safe_message = message
        self.category = category
        self.diagnostics = diagnostics
        if diagnostics is not None:
            http_status = diagnostics.http_status if diagnostics.http_status is not None else "无"
            message = (
                f"{message}（分类={diagnostics.category}；阶段={diagnostics.stage}；"
                f"HTTP={http_status}；已收={diagnostics.received_bytes} bytes；"
                f"耗时={diagnostics.elapsed_ms} ms）"
            )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StreamDiagnostics:
    category: str
    stage: str
    http_status: int | None
    received_bytes: int
    elapsed_ms: int


def _stream_transport_category(error: BaseException, stage: str) -> str:
    """Map network exceptions to a fixed label without exposing exception text."""
    reason: BaseException = error
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        reason = error.reason
    timeout_codes = {errno.ETIMEDOUT, 10060}  # POSIX ETIMEDOUT and Winsock WSAETIMEDOUT.
    would_block_codes = {errno.EAGAIN, errno.EWOULDBLOCK, 10035}  # Winsock WSAEWOULDBLOCK.
    if (
        isinstance(reason, TimeoutError)
        or getattr(reason, "errno", None) in timeout_codes
        or getattr(reason, "winerror", None) == 10060
    ):
        return "connect_timeout" if stage in {"connect", "response_headers"} else "read_timeout"
    if (
        isinstance(reason, BlockingIOError)
        or getattr(reason, "errno", None) in would_block_codes
        or getattr(reason, "winerror", None) == 10035
    ):
        return "connect_timeout" if stage in {"connect", "response_headers"} else "read_timeout"
    if isinstance(reason, ssl.SSLError):
        return "tls_failure"
    if isinstance(reason, http.client.RemoteDisconnected):
        return "remote_disconnect"
    if isinstance(reason, ConnectionResetError):
        return "connection_reset"
    if isinstance(reason, socket.gaierror):
        return "name_resolution_failure"
    return "transport_error"


def _stream_error_category(error: ProviderStreamError, stage: str) -> str:
    if error.category is not None:
        return error.category
    if "超过" in error.safe_message and "上限" in error.safe_message:
        return "size_limit"
    if stage == "protocol" or stage == "response_headers":
        return "protocol_rejection"
    if stage == "completion":
        return "missing_end_marker"
    if stage == "sse_parse":
        return "parse_error"
    if "总时限" in error.safe_message:
        return "connect_timeout" if stage == "connect" else "read_timeout"
    if "完成标记前结束" in error.safe_message:
        return "missing_end_marker"
    return "transport_error"


class _SSEParser:
    """Incremental UTF-8/SSE parser with limits on lines, events, and total bytes."""

    def __init__(self, on_event: Callable[[str, str], bool]) -> None:
        self._on_event = on_event
        self._buffer = bytearray()
        self._event_name = ""
        self._data: list[str] = []
        self._event_bytes = 0
        self.total_bytes = 0
        self.events = 0
        self.done = False

    def feed(self, chunk: bytes) -> bool:
        if self.done:
            if chunk:
                raise ProviderStreamError("SSE 结束标记后收到额外数据。")
            return True
        self.total_bytes += len(chunk)
        if self.total_bytes > MAX_STREAM_RESPONSE_BYTES:
            raise ProviderStreamError("SSE 响应超过 4 MiB 上限。")
        self._buffer.extend(chunk)
        return self._drain(final=False)

    def finish(self) -> bool:
        if self.done:
            return True
        try:
            bytes(self._buffer).decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderStreamError("SSE 响应在 UTF-8 字符中途结束。") from None
        self._drain(final=True)
        if self._buffer or self._data or self._event_name:
            raise ProviderStreamError("SSE 响应包含未完成事件。")
        return False

    def _drain(self, *, final: bool) -> bool:
        buffer = self._buffer
        cursor = 0
        while cursor < len(buffer):
            lf = buffer.find(b"\n", cursor)
            cr = buffer.find(b"\r", cursor)
            delimiters = [index for index in (lf, cr) if index >= 0]
            if not delimiters:
                if len(buffer) - cursor > MAX_STREAM_LINE_BYTES:
                    raise ProviderStreamError("SSE 单行超过大小上限。")
                break
            index = min(delimiters)
            char = buffer[index]
            if char == 13 and index + 1 == len(buffer) and not final:
                if index - cursor > MAX_STREAM_LINE_BYTES:
                    raise ProviderStreamError("SSE 单行超过大小上限。")
                break
            width = 2 if char == 13 and buffer[index : index + 2] == b"\r\n" else 1
            line_bytes = bytes(buffer[cursor:index])
            cursor = index + width
            if len(line_bytes) > MAX_STREAM_LINE_BYTES:
                raise ProviderStreamError("SSE 单行超过大小上限。")
            self._event_bytes += len(line_bytes) + width
            if self._event_bytes > MAX_STREAM_EVENT_BYTES:
                raise ProviderStreamError("SSE 事件超过大小上限。")
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise ProviderStreamError("SSE 响应不是有效的 UTF-8 文本。") from None
            if not line:
                if self._data:
                    self.events += 1
                    if self.events > MAX_STREAM_EVENTS:
                        raise ProviderStreamError("SSE 事件数量超过上限。")
                    name = self._event_name or "message"
                    payload = "\n".join(self._data)
                    self._event_name = ""
                    self._data = []
                    self._event_bytes = 0
                    self.done = self._on_event(name, payload)
                    if self.done:
                        if cursor < len(buffer):
                            raise ProviderStreamError("SSE 结束标记后收到额外数据。")
                        del buffer[:cursor]
                        return True
                else:
                    self._event_name = ""
                    self._event_bytes = 0
                continue
            if line.startswith(":"):
                continue
            field_name, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field_name == "data":
                self._data.append(value)
            elif field_name == "event":
                self._event_name = value
        if cursor:
            del buffer[:cursor]
        return False


def validate_base_url(base_url: str) -> str:
    """Validate an endpoint without echoing user-supplied URL text in errors."""
    if (
        not isinstance(base_url, str)
        or not base_url
        or len(base_url) > 2048
        or base_url != base_url.strip()
        or "\\" in base_url
        or "?" in base_url
        or "#" in base_url
        or _URL_CREDENTIAL_MARKER.search(base_url) is not None
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in base_url)
    ):
        raise ProviderError("Base URL 格式无效；不得包含凭据、查询参数、片段或控制字符。")
    from .sessions import contains_likely_secret

    if contains_likely_secret(base_url):
        raise ProviderError("Base URL 疑似包含凭据；请只填写服务地址，不要填写任何密钥。")
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port  # Reject malformed ports before any request is constructed.
        hostname = parsed.hostname
    except ValueError:
        raise ProviderError("Base URL 格式无效；请使用安全的 HTTPS 服务地址。") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError("Base URL 格式无效；不得包含凭据、查询参数或片段。")
    if parsed.scheme != "https" and hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}:
        raise ProviderError("仅允许 HTTPS；HTTP 只对本机回环地址开放。")
    return base_url


def validate_deepseek_base_url(base_url: str) -> str:
    """Restrict the configured DeepSeek key target to HTTPS on api.deepseek.com:443."""
    if not isinstance(base_url, str) or len(base_url) > 2048:
        raise ProviderError("Base URL 格式无效。")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise ProviderError("Base URL 格式无效。") from None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != "api.deepseek.com"
        or port not in {None, 443}
    ):
        raise ProviderError(
            f"使用 {DEEPSEEK_API_KEY_ENV} 时仅允许 HTTPS api.deepseek.com 的 443 端口。"
        )
    return validate_base_url(base_url)


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
    finish_reason: str | None = None


class Provider(Protocol):
    label: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        cancellation_event: threading.Event | None = None,
        max_tokens: int = 2048,
    ) -> AssistantTurn: ...


class MockProvider:
    """Deterministic offline provider used by default and by smoke tests."""

    label = "mock"

    def __init__(self, model: str = "mock") -> None:
        self.model = model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        cancellation_event: threading.Event | None = None,
        max_tokens: int = 2048,
    ) -> AssistantTurn:
        del tools
        if type(max_tokens) is not int or not 1 <= max_tokens <= 65536:
            raise ProviderError("max_tokens must be between 1 and 65536")
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider request cancelled before response")
        last_user = next(
            (item.get("content", "") for item in reversed(messages) if item.get("role") == "user"),
            "",
        )
        answer = f"[模拟模式] 已收到：{last_user[:400]}"
        while answer and estimate_tokens(answer) > max_tokens:
            answer = answer[:-1]
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider request cancelled after response")
        return AssistantTurn(
            content=answer,
            assistant_message={"role": "assistant", "content": answer},
        )

    def complete_subagent(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        timeout_seconds: float,
        max_output_tokens: int,
        cancellation_event: threading.Event,
    ) -> AssistantTurn:
        if cancellation_event.is_set():
            raise ProviderCancelled("subagent cancelled")
        if timeout_seconds <= 0:
            raise TimeoutError("subagent deadline elapsed")
        turn = self.complete(messages, tools or [], cancellation_event=cancellation_event, max_tokens=max_output_tokens)
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
        stream_timeout: float = MAX_STREAM_DURATION_SECONDS,
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_MODEL_REQUEST_TIMEOUT_SECONDS
        ):
            raise ProviderError("模型请求总时限必须在 0 到 120 秒之间。")
        if (
            isinstance(stream_timeout, bool)
            or not isinstance(stream_timeout, (int, float))
            or not 0 < stream_timeout <= MAX_STREAM_DURATION_SECONDS
        ):
            raise ProviderError("SSE 总时限必须在 0 到 120 秒之间。")
        base_url = validate_base_url(base_url)
        parsed_base = urlsplit(base_url)
        is_loopback = (parsed_base.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
        if not api_key and not is_loopback:
            raise ProviderError("未提供 API 密钥。")
        self.endpoint = self._chat_endpoint(base_url)
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.stream_timeout = float(stream_timeout)

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

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
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
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ProviderError("模型响应格式无效。") from None
        return self._turn_from_payload(payload)

    def complete_streaming(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        cancellation_event: threading.Event | None = None,
        max_tokens: int = 2048,
        on_text_delta: Callable[[str], None],
        timeout_seconds: float | None = None,
    ) -> AssistantTurn:
        """Receive bounded SSE deltas; never return partial tool calls to Agent."""
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider stream cancelled before send")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 65536:
            raise ProviderError("max_tokens must be between 1 and 65536")
        if not callable(on_text_delta):
            raise ProviderStreamError("SSE 文本回调无效。")
        stream_timeout = self.stream_timeout
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= MAX_STREAM_DURATION_SECONDS
            ):
                raise ProviderStreamError("SSE 单次请求时限无效。")
            stream_timeout = min(stream_timeout, float(timeout_seconds))

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning_effort": "high",
            "thinking": {"type": "enabled"},
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/event-stream",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, list[str]]] = {}
        tool_sizes: dict[int, dict[str, int]] = {}
        usage_value: Any = None
        finish_reason: str | None = None
        assistant_bytes = 0

        def append_text(target: list[str], value: Any, *, display: bool) -> None:
            nonlocal assistant_bytes
            if value is None:
                return
            if not isinstance(value, str):
                raise ProviderStreamError("SSE 文本片段格式无效。")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeEncodeError:
                raise ProviderStreamError("SSE 文本片段格式无效。") from None
            assistant_bytes += size
            if assistant_bytes > MAX_STREAM_ASSISTANT_BYTES:
                raise ProviderStreamError("SSE 助手内容超过 2 MiB 上限。")
            target.append(value)
            if display and value:
                try:
                    on_text_delta(value)
                except Exception:
                    raise ProviderStreamError("无法显示 SSE 流式文本。", category="local_output_error") from None

        def collect_event(event_name: str, data: str) -> bool:
            nonlocal usage_value, finish_reason
            if event_name == "error":
                raise ProviderStreamError("模型服务返回 SSE 错误事件。")
            if data == "[DONE]":
                if finish_reason is None:
                    raise ProviderStreamError("SSE 流在结束前缺少完成状态。")
                if tool_parts and finish_reason != "tool_calls":
                    raise ProviderStreamError("SSE 工具调用与完成状态不一致。")
                if finish_reason == "tool_calls" and not tool_parts:
                    raise ProviderStreamError("SSE 完成状态要求工具调用，但未收到调用内容。")
                return True
            try:
                chunk = json.loads(data)
            except (json.JSONDecodeError, RecursionError):
                raise ProviderStreamError("SSE 事件 JSON 格式无效。", category="parse_error") from None
            if not isinstance(chunk, dict) or "error" in chunk:
                raise ProviderStreamError("模型服务返回格式无效的 SSE 事件。")
            if "usage" in chunk:
                usage_value = chunk["usage"]
            choices = chunk.get("choices")
            if choices is None:
                if "usage" in chunk:
                    return False
                raise ProviderStreamError("SSE 事件缺少 choices。")
            if not isinstance(choices, list) or len(choices) > 1:
                raise ProviderStreamError("SSE 仅支持单个模型候选项。")
            if not choices:
                if "usage" in chunk:
                    return False
                raise ProviderStreamError("SSE choices 为空。")
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ProviderStreamError("SSE 候选项格式无效。")
            if choice.get("logprobs") is not None:
                raise ProviderStreamError("SSE logprobs 模式尚未支持。")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ProviderStreamError("SSE delta 格式无效。")
            if set(delta) - {"role", "content", "reasoning_content", "tool_calls"}:
                raise ProviderStreamError("SSE 含有尚未支持的响应类型。")
            if delta.get("role") not in (None, "assistant"):
                raise ProviderStreamError("SSE 消息角色无效。")
            append_text(content_parts, delta.get("content"), display=True)
            append_text(reasoning_parts, delta.get("reasoning_content"), display=False)
            tool_deltas = delta.get("tool_calls")
            if tool_deltas is not None:
                if not isinstance(tool_deltas, list) or len(tool_deltas) > MAX_PROVIDER_TOOL_CALLS:
                    raise ProviderStreamError("SSE 工具调用片段格式无效。")
                for tool_delta in tool_deltas:
                    if not isinstance(tool_delta, dict):
                        raise ProviderStreamError("SSE 工具调用片段格式无效。")
                    if set(tool_delta) - {"index", "type", "id", "function"}:
                        raise ProviderStreamError("SSE 工具调用包含尚未支持的字段。")
                    index = tool_delta.get("index")
                    if type(index) is not int or not 0 <= index < MAX_PROVIDER_TOOL_CALLS:
                        raise ProviderStreamError("SSE 工具调用索引无效。")
                    if tool_delta.get("type") not in (None, "function"):
                        raise ProviderStreamError("SSE 仅支持函数工具调用。")
                    slot = tool_parts.setdefault(index, {"id": [], "name": [], "arguments": []})
                    sizes = tool_sizes.setdefault(index, {"id": 0, "name": 0, "arguments": 0})
                    call_id_fragment = tool_delta.get("id")
                    if call_id_fragment is not None:
                        if not isinstance(call_id_fragment, str):
                            raise ProviderStreamError("SSE 工具调用片段格式无效。")
                        try:
                            sizes["id"] += len(call_id_fragment.encode("utf-8"))
                        except UnicodeEncodeError:
                            raise ProviderStreamError("SSE 工具调用片段格式无效。") from None
                        slot["id"].append(call_id_fragment)
                    function = tool_delta.get("function")
                    if function is not None:
                        if not isinstance(function, dict) or set(function) - {"name", "arguments"}:
                            raise ProviderStreamError("SSE 函数工具片段格式无效。")
                        for field_name in ("name", "arguments"):
                            fragment = function.get(field_name)
                            if fragment is not None:
                                if not isinstance(fragment, str):
                                    raise ProviderStreamError("SSE 函数工具片段格式无效。")
                                try:
                                    sizes[field_name] += len(fragment.encode("utf-8"))
                                except UnicodeEncodeError:
                                    raise ProviderStreamError("SSE 函数工具片段格式无效。") from None
                                slot[field_name].append(fragment)
                    if (
                        sizes["id"] > 128
                        or sizes["name"] > 128
                        or sizes["arguments"] > MAX_PROVIDER_TOOL_ARGUMENT_BYTES
                    ):
                        raise ProviderStreamError("SSE 工具调用超过本地大小上限。")
            current_finish_reason = choice.get("finish_reason")
            if current_finish_reason is not None:
                if not isinstance(current_finish_reason, str) or current_finish_reason not in {
                    "stop", "length", "tool_calls", "content_filter"
                }:
                    raise ProviderStreamError("SSE 完成状态尚未支持。")
                if finish_reason is not None and finish_reason != current_finish_reason:
                    raise ProviderStreamError("SSE 完成状态不一致。")
                finish_reason = current_finish_reason
            return False

        stage = "connect"
        http_status: int | None = None
        received_bytes = 0
        started_at = time.monotonic()
        deadline = started_at + stream_timeout

        def parse_event(event_name: str, data: str) -> bool:
            nonlocal stage
            previous_stage = stage
            stage = "protocol"
            try:
                result = collect_event(event_name, data)
            except Exception:
                # Preserve the failed phase for the safe diagnostic built by the outer handler.
                stage = "protocol"
                raise
            else:
                stage = previous_stage
                return result

        parser = _SSEParser(parse_event)

        def add_diagnostics(error: ProviderStreamError, category: str) -> ProviderStreamError:
            elapsed_ms = max(0, int((time.monotonic() - started_at) * 1000))
            status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
            diagnostics = StreamDiagnostics(
                category=category,
                stage=stage,
                http_status=status,
                received_bytes=received_bytes,
                elapsed_ms=elapsed_ms,
            )
            return ProviderStreamError(error.safe_message, category=category, diagnostics=diagnostics)

        try:
            with urllib.request.urlopen(
                request,
                timeout=min(self.timeout, stream_timeout),
            ) as response:
                response_status = getattr(response, "status", None)
                if type(response_status) is int and 100 <= response_status <= 599:
                    http_status = response_status
                stage = "response_headers"
                headers = getattr(response, "headers", None)
                content_type = ""
                if headers is not None:
                    getter = getattr(headers, "get_content_type", None)
                    content_type = getter() if callable(getter) else str(headers.get("Content-Type", ""))
                if content_type.partition(";")[0].strip().casefold() != "text/event-stream":
                    raise ProviderStreamError("模型服务没有返回 text/event-stream。")
                content_length = headers.get("Content-Length") if headers is not None else None
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_STREAM_RESPONSE_BYTES:
                            raise ProviderStreamError("SSE 响应超过 4 MiB 上限。")
                    except ValueError:
                        raise ProviderStreamError("SSE Content-Length 格式无效。") from None
                stage = "response_body"
                raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if raw_socket is None:
                    raise ProviderStreamError("无法建立有界 SSE 网络读取。", category="bounded_reader_unavailable")
                while True:
                    stage = "response_body"
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise ProviderCancelled("provider stream cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProviderStreamError("SSE 流超过总时限。")
                    poll_timeout = min(STREAM_READ_POLL_SECONDS, remaining)
                    if raw_socket is not None:
                        try:
                            raw_socket.settimeout(poll_timeout)
                        except OSError:
                            try:
                                socket_closed = raw_socket.fileno() < 0
                            except OSError:
                                socket_closed = True
                            if not socket_closed:
                                raise ProviderStreamError("SSE 网络传输配置失败。", category="transport_error") from None
                            # A peer may close after sending Content-Length bytes; buffered
                            # response data can still be consumed and EOF is bounded.
                            raw_socket = None
                    try:
                        chunk = response.read1(8192)
                    except http.client.IncompleteRead as exc:
                        if isinstance(exc.partial, bytes):
                            received_bytes += len(exc.partial)
                        stage = "completion"
                        raise ProviderStreamError("SSE 响应在完成标记前结束。") from None
                    except TimeoutError:
                        if cancellation_event is not None and cancellation_event.is_set():
                            raise ProviderCancelled("provider stream cancelled") from None
                        if time.monotonic() >= deadline:
                            raise ProviderStreamError("SSE 流超过总时限。") from None
                        continue
                    except (OSError, urllib.error.URLError) as exc:
                        category = _stream_transport_category(exc, stage)
                        if (
                            category == "transport_error"
                            and stage == "response_body"
                            and deadline - time.monotonic() <= STREAM_READ_POLL_SECONDS
                        ):
                            # Some platform transports surface the final poll timeout as a
                            # generic OSError; keep that near-deadline read safely classified.
                            category = "read_timeout"
                        message = {
                            "read_timeout": "SSE 响应读取超时。",
                            "tls_failure": "SSE TLS 安全传输失败。",
                            "remote_disconnect": "SSE 对端提前断开连接。",
                            "connection_reset": "SSE 连接被对端重置。",
                            "name_resolution_failure": "SSE 主机名解析失败。",
                            "connect_timeout": "SSE 建连或响应头等待超时。",
                        }.get(category, "SSE 网络传输失败。")
                        raise ProviderStreamError(message, category=category) from None
                    except Exception:
                        raise ProviderStreamError("SSE 流读取处理失败。", category="internal_error") from None
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise ProviderCancelled("provider stream cancelled")
                    if time.monotonic() >= deadline:
                        raise ProviderStreamError("SSE 流超过总时限。")
                    if not chunk:
                        stage = "sse_parse"
                        parser.finish()
                        stage = "completion"
                        raise ProviderStreamError("SSE 响应在完成标记前结束。")
                    received_bytes += len(chunk)
                    stage = "sse_parse"
                    stream_done = parser.feed(chunk)
                    stage = "response_body"
                    if time.monotonic() >= deadline:
                        raise ProviderStreamError("SSE 流超过总时限。")
                    if stream_done:
                        stage = "completion"
                        break
        except urllib.error.HTTPError as exc:
            status = exc.code
            if type(status) is int and 100 <= status <= 599:
                http_status = status
            try:
                exc.close()
            except Exception:
                pass
            stage = "response_headers"
            status_text = f" HTTP {http_status}" if http_status is not None else ""
            error = ProviderStreamError(f"模型服务返回{status_text}。", category="http_status")
            raise add_diagnostics(error, "http_status") from None
        except ProviderStreamError as exc:
            raise add_diagnostics(exc, _stream_error_category(exc, stage)) from None
        except ProviderCancelled:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            category = _stream_transport_category(exc, stage)
            message = {
                "connect_timeout": "SSE 建连或响应头等待超时。",
                "read_timeout": "SSE 响应读取超时。",
                "tls_failure": "SSE TLS 安全传输失败。",
                "remote_disconnect": "SSE 对端提前断开连接。",
                "connection_reset": "SSE 连接被对端重置。",
                "name_resolution_failure": "SSE 主机名解析失败。",
            }.get(category, "SSE 网络传输失败。")
            error = ProviderStreamError(message, category=category)
            raise add_diagnostics(error, category) from None
        except Exception:
            error = ProviderStreamError("SSE 流处理失败。", category="internal_error")
            raise add_diagnostics(error, "internal_error") from None

        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider stream cancelled")
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_parts:
            message["tool_calls"] = [
                {
                    "id": "".join(tool_parts[index]["id"]),
                    "type": "function",
                    "function": {
                        "name": "".join(tool_parts[index]["name"]),
                        "arguments": "".join(tool_parts[index]["arguments"]),
                    },
                }
                for index in sorted(tool_parts)
            ]
        return self._turn_from_payload(
            {
                "choices": [{"message": message, "finish_reason": finish_reason}],
                "usage": usage_value,
            }
        )

    def complete_subagent(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        timeout_seconds: float,
        max_output_tokens: int,
        cancellation_event: threading.Event,
    ) -> AssistantTurn:
        """Run an isolated SSE turn with only the canonical, user-approved read tools."""
        if cancellation_event.is_set():
            raise ProviderCancelled("subagent cancelled")
        if (
            not isinstance(messages, list)
            or not 2 <= len(messages) <= 5
            or not all(isinstance(item, dict) for item in messages)
            or messages[0].get("role") != "system"
            or messages[1].get("role") != "user"
            or not isinstance(messages[0].get("content"), str)
            or not isinstance(messages[1].get("content"), str)
            or len(messages[1]["content"]) > MAX_SUBAGENT_TASK_CHARS
            or any(item.get("role") not in {"system", "user", "assistant", "tool"} for item in messages)
            or any(item.get("role") == "system" for item in messages[1:])
            or any(item.get("role") == "user" for item in messages[2:])
        ):
            raise ProviderError("子代理上下文格式无效。")
        try:
            if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
                raise ProviderError("子代理上下文超过本地大小上限。")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ProviderError("子代理上下文格式无效。") from None
        allowed_names = {"read_file", "list_directory"}
        if tools is None:
            tools = []
        if not isinstance(tools, list) or len(tools) > 2:
            raise ProviderError("子代理只读工具列表格式无效。")
        from .tools import TOOL_SCHEMAS

        canonical_read_tools = {
            schema["function"]["name"]: schema
            for schema in TOOL_SCHEMAS
            if schema["function"]["name"] in allowed_names
        }
        seen_tools: set[str] = set()
        for schema in tools:
            try:
                name = schema["function"]["name"]
            except (KeyError, TypeError):
                raise ProviderError("子代理只读工具列表格式无效。") from None
            if name not in allowed_names or name in seen_tools or schema != canonical_read_tools[name]:
                raise ProviderError("子代理只允许使用只读文件工具。")
            seen_tools.add(name)
        from .usage import estimate_payload_tokens

        if estimate_payload_tokens(messages, tools) > MAX_SUBAGENT_INPUT_TOKENS:
            raise ProviderError("子代理输入超过本地预算上限。")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_SUBAGENT_OUTPUT_TOKENS:
            raise ProviderError("子代理输出预算超出本地上限。")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= MAX_SUBAGENT_TIMEOUT_SECONDS
        ):
            raise ProviderError("子代理请求时限超出本地上限。")
        if cancellation_event.is_set():
            raise ProviderCancelled("subagent cancelled")
        started = time.monotonic()
        try:
            turn = self.complete_streaming(
                messages,
                tools,
                cancellation_event=cancellation_event,
                max_tokens=max_output_tokens,
                on_text_delta=lambda _fragment: None,
                timeout_seconds=float(timeout_seconds),
            )
        except ProviderStreamError:
            if cancellation_event.is_set():
                raise ProviderCancelled("subagent cancelled") from None
            if time.monotonic() - started >= timeout_seconds:
                raise TimeoutError("subagent deadline elapsed") from None
            raise
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError("subagent deadline elapsed")
        if cancellation_event.is_set():
            raise ProviderCancelled("subagent cancelled")
        if len(turn.tool_calls) > MAX_SUBAGENT_TOOL_CALLS_PER_TURN:
            raise ProviderError("子代理只读调用数超过本地上限。")
        return turn

    def _turn_from_payload(self, payload: Any) -> AssistantTurn:
        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError, RecursionError):
            raise ProviderError("模型响应格式无效。") from None
        if not isinstance(choice, dict):
            raise ProviderError("模型响应格式无效。")
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ProviderError("模型完成状态无效。")
        if not isinstance(message, dict):
            raise ProviderError("模型响应格式无效。")

        content_value = message.get("content")
        if content_value is not None and not isinstance(content_value, str):
            raise ProviderError("模型响应格式无效。")
        content = content_value if isinstance(content_value, str) else ""
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list) or len(raw_calls) > MAX_PROVIDER_TOOL_CALLS:
            raise ProviderError("模型返回了格式无效的工具调用。")
        calls: list[ToolCall] = []
        seen_call_ids: set[str] = set()
        for raw_call in raw_calls:
            try:
                if not isinstance(raw_call, dict):
                    raise ValueError("tool call must be an object")
                function = raw_call["function"]
                if not isinstance(function, dict):
                    raise ValueError("tool function must be an object")
                call_id = raw_call.get("id")
                name = function.get("name")
                raw_arguments = function.get("arguments")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or len(call_id) > 128
                    or any(ord(char) < 32 for char in call_id)
                    or call_id in seen_call_ids
                    or not isinstance(name, str)
                    or not name
                    or len(name) > 128
                    or not isinstance(raw_arguments, str)
                    or len(raw_arguments.encode("utf-8")) > MAX_PROVIDER_TOOL_ARGUMENT_BYTES
                ):
                    raise ValueError("invalid tool metadata")
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                if len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_PROVIDER_TOOL_ARGUMENT_BYTES:
                    raise ValueError("tool arguments exceed size limit")
                seen_call_ids.add(call_id)
                calls.append(
                    ToolCall(
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                )
            except (KeyError, TypeError, json.JSONDecodeError, ValueError, UnicodeEncodeError, RecursionError):
                # Preserve the assistant response but surface malformed tool calls
                # as an ordinary answer; never try to execute partial arguments.
                raise ProviderError("模型返回了格式无效的工具调用。") from None

        assistant_message: dict[str, Any] = {"role": "assistant", "content": content_value}
        reasoning_content = message.get("reasoning_content")
        if reasoning_content is not None:
            if not isinstance(reasoning_content, str):
                raise ProviderError("模型响应格式无效。")
            assistant_message["reasoning_content"] = reasoning_content
        if raw_calls:
            assistant_message["tool_calls"] = raw_calls
        return AssistantTurn(
            content=content,
            tool_calls=calls,
            assistant_message=assistant_message,
            usage=self._parse_usage(payload.get("usage")),
            finish_reason=finish_reason,
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
        cached_prompt = value.get("prompt_cache_hit_tokens")
        if type(cached_prompt) is not int or not 0 <= cached_prompt <= prompt:
            cached_prompt = None
        return ProviderUsage(prompt, completion, total, cached_prompt)


def provider_from_environment(
    provider_name: str,
    model_override: str | None = None,
    *,
    api_key_override: str | None = None,
    base_url_override: str | None = None,
    request_timeout_seconds: float = DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> Provider:
    """从当前进程配置创建提供商；测试可传入隔离的配置映射。"""
    settings = os.environ if environment is None else environment
    if provider_name == "mock":
        return MockProvider(model=model_override or "mock")
    if provider_name == "openai":
        model = model_override or settings.get("HARNESS_MODEL", "deepseek-flash")
        base_url = base_url_override or settings.get("HARNESS_BASE_URL", "https://api.deepseek.com")
        # 云端 Network secret 以 DEEPSEEK_API_KEY 提供占位符；仅 OpenAI 模式读取。
        # 这个变量只允许用于 DeepSeek 主机，避免把占位符或凭据发往其他站点。
        api_key = (
            api_key_override
            if api_key_override is not None
            else settings.get(DEEPSEEK_API_KEY_ENV, "")
        )
        if api_key_override is None and api_key:
            base_url = validate_deepseek_base_url(base_url)
        else:
            base_url = validate_base_url(base_url)
        return OpenAICompatibleProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=request_timeout_seconds,
            stream_timeout=request_timeout_seconds,
        )
    raise ProviderError(f"未知模型提供商：{provider_name}")
