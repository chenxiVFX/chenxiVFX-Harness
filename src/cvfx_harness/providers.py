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
DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_ANTHROPIC_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
ANTHROPIC_API_VERSION = "2023-06-01"
MAX_PROVIDER_TOOL_CALLS = 16
MAX_ANTHROPIC_CONTENT_BLOCKS = 256
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


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )


def _openai_finish_reason_error(finish_reason: Any, has_tool_calls: bool) -> str | None:
    supported = {
        "stop",
        "length",
        "content_filter",
        "tool_calls",
        "insufficient_system_resource",
        "aborted",
    }
    if not isinstance(finish_reason, str) or finish_reason not in supported:
        return "模型完成状态格式无效。"
    if has_tool_calls != (finish_reason == "tool_calls"):
        return "模型工具调用与完成状态不一致；未执行工具。"
    return None


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


def validate_deepseek_anthropic_base_url(
    base_url: str,
    *,
    allow_loopback_http: bool = False,
) -> str:
    """Restrict the DeepSeek Anthropic-compatible adapter to its documented path."""
    validated = validate_base_url(base_url)
    parsed = urlsplit(validated)
    hostname = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        raise ProviderError("Anthropic 兼容 Base URL 格式无效。") from None
    if parsed.path.rstrip("/") != "/anthropic":
        raise ProviderError("DeepSeek Anthropic 兼容模式只允许 /anthropic 路径。")
    if (
        parsed.scheme == "https"
        and hostname == "api.deepseek.com"
        and port in {None, 443}
    ):
        return validated
    if (
        allow_loopback_http
        and parsed.scheme == "http"
        and hostname in {"localhost", "127.0.0.1", "::1"}
        and port is not None
    ):
        return validated
    raise ProviderError(
        f"使用 {DEEPSEEK_API_KEY_ENV} 时仅允许 HTTPS api.deepseek.com:443/anthropic。"
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so an authenticated request cannot change target hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _open_without_redirect(request: urllib.request.Request, *, timeout: float):
    opener = urllib.request.build_opener(_NoRedirectHandler)
    return opener.open(request, timeout=timeout)


class _SSEReadGuard:
    """Interrupt blocking HTTP reads without retrying a timed-out makefile buffer.

    Parsing and callbacks stay on the caller thread. A single joined watchdog
    only shuts down this response socket on cancellation or the total deadline.
    """

    def __init__(self, response, deadline: float, cancellation_event: threading.Event | None) -> None:
        self.response = response
        self.deadline = deadline
        self.cancellation_event = cancellation_event
        self.socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        self._finished = threading.Event()
        self._expired = threading.Event()
        self._watchdog: threading.Thread | None = None

    def _check(self) -> None:
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            raise ProviderCancelled("provider stream cancelled")
        if self._expired.is_set() or time.monotonic() >= self.deadline:
            raise ProviderStreamError("SSE 流超过总时限。", category="read_timeout")

    def _shutdown(self) -> None:
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # The response may already have closed after its final bytes.

    def _watch(self) -> None:
        while not self._finished.is_set():
            remaining = max(0.0, self.deadline - time.monotonic())
            if self._finished.wait(min(STREAM_READ_POLL_SECONDS, remaining)):
                return
            cancelled = self.cancellation_event is not None and self.cancellation_event.is_set()
            expired = time.monotonic() >= self.deadline
            if cancelled or expired:
                if expired:
                    self._expired.set()
                self._shutdown()
                return

    def __enter__(self):
        if self.socket is None or not all(
            callable(getattr(self.socket, name, None)) for name in ("settimeout", "shutdown", "fileno")
        ):
            raise ProviderStreamError("无法建立有界 SSE 网络读取。", category="bounded_reader_unavailable")
        self._check()
        self._watchdog = threading.Thread(target=self._watch, name="cvfx-sse-watchdog")
        self._watchdog.start()
        return self

    def read1(self, size: int) -> bytes:
        self._check()
        try:
            # A short timeout poisons SocketIO's makefile buffer after a normal
            # generation pause. A real timeout is terminal; never resume it.
            self.socket.settimeout(max(0.001, self.deadline - time.monotonic()))
        except OSError:
            if self.socket.fileno() >= 0:
                raise ProviderStreamError("SSE 网络传输配置失败。", category="transport_error") from None
        try:
            chunk = self.response.read1(size)
        except BaseException:
            self._check()
            raise
        self._check()
        return chunk

    def __exit__(self, *_args) -> None:
        self._finished.set()
        self._shutdown()
        if self._watchdog is not None:
            self._watchdog.join()


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
            with _open_without_redirect(request, timeout=self.timeout) as response:
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
            payload = _strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
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
                completion_error = _openai_finish_reason_error(finish_reason, bool(tool_parts))
                if completion_error is not None:
                    raise ProviderStreamError(completion_error)
                return True
            try:
                chunk = _strict_json_loads(data)
            except (ValueError, RecursionError):
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
            if finish_reason is not None:
                raise ProviderStreamError("SSE 完成状态后收到额外候选内容。")
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
                    "stop", "length", "tool_calls", "content_filter",
                    "insufficient_system_resource", "aborted",
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
            with _open_without_redirect(
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
                with _SSEReadGuard(response, deadline, cancellation_event) as reader:
                    while True:
                        stage = "response_body"
                        try:
                            chunk = reader.read1(8192)
                        except http.client.IncompleteRead as exc:
                            if isinstance(exc.partial, bytes):
                                received_bytes += len(exc.partial)
                            stage = "completion"
                            raise ProviderStreamError("SSE 响应在完成标记前结束。") from None
                        except TimeoutError:
                            raise ProviderStreamError("SSE 响应读取超时。", category="read_timeout") from None
                        except (OSError, urllib.error.URLError) as exc:
                            category = _stream_transport_category(exc, stage)
                            message = {
                                "read_timeout": "SSE 响应读取超时。",
                                "tls_failure": "SSE TLS 安全传输失败。",
                                "remote_disconnect": "SSE 对端提前断开连接。",
                                "connection_reset": "SSE 连接被对端重置。",
                                "name_resolution_failure": "SSE 主机名解析失败。",
                            }.get(category, "SSE 网络传输失败。")
                            raise ProviderStreamError(message, category=category) from None
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
                            raise ProviderStreamError("SSE 流超过总时限。", category="read_timeout")
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
        if not isinstance(payload, dict):
            raise ProviderError("模型响应格式无效。")
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderError("模型响应格式无效。")
        choice = choices[0]
        if choice.get("index", 0) != 0:
            raise ProviderError("模型候选项格式无效。")
        finish_reason = choice.get("finish_reason")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError("模型响应格式无效。")
        if message.get("role") != "assistant":
            raise ProviderError("模型响应角色无效。")

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
                    or raw_call.get("type") != "function"
                    or not isinstance(name, str)
                    or not name
                    or len(name) > 128
                    or not isinstance(raw_arguments, str)
                    or len(raw_arguments.encode("utf-8")) > MAX_PROVIDER_TOOL_ARGUMENT_BYTES
                ):
                    raise ValueError("invalid tool metadata")
                arguments = _strict_json_loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                if len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_PROVIDER_TOOL_ARGUMENT_BYTES:
                    raise ValueError("tool arguments exceed size limit")
                seen_call_ids.add(call_id)
                calls.append(
                    ToolCall(
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                )
            except (KeyError, TypeError, ValueError, UnicodeEncodeError, RecursionError):
                # Preserve the assistant response but surface malformed tool calls
                # as an ordinary answer; never try to execute partial arguments.
                raise ProviderError("模型返回了格式无效的工具调用。") from None

        completion_error = _openai_finish_reason_error(finish_reason, bool(calls))
        if completion_error is not None:
            raise ProviderError(completion_error)

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
        if (
            type(prompt) is not int
            or type(completion) is not int
            or not 0 <= prompt <= 2_000_000_000
            or not 0 <= completion <= 2_000_000_000
        ):
            return None
        total = prompt + completion
        if "total_tokens" in value:
            reported_total = value["total_tokens"]
            if type(reported_total) is not int or reported_total != total:
                return None

        cache_values: list[int] = []
        cache_invalid = False
        if "prompt_cache_hit_tokens" in value and value["prompt_cache_hit_tokens"] is not None:
            cache_value = value["prompt_cache_hit_tokens"]
            if type(cache_value) is not int or not 0 <= cache_value <= prompt:
                cache_invalid = True
            else:
                cache_values.append(cache_value)

        if "prompt_tokens_details" in value:
            details = value["prompt_tokens_details"]
            if not isinstance(details, dict):
                cache_invalid = True
            elif details.get("cached_tokens") is not None:
                cache_value = details["cached_tokens"]
                if type(cache_value) is not int or not 0 <= cache_value <= prompt:
                    cache_invalid = True
                else:
                    cache_values.append(cache_value)

        if "prompt_cache_miss_tokens" in value and value["prompt_cache_miss_tokens"] is not None:
            cache_miss = value["prompt_cache_miss_tokens"]
            if (
                type(cache_miss) is not int
                or not 0 <= cache_miss <= prompt
                or not cache_values
                or cache_values[-1] + cache_miss != prompt
            ):
                cache_invalid = True

        cached_prompt = (
            cache_values[0]
            if cache_values and not cache_invalid and all(item == cache_values[0] for item in cache_values)
            else None
        )
        return ProviderUsage(prompt, completion, total, cached_prompt)


class _AnthropicEventAccumulator:
    """Validate and accumulate the documented Anthropic Messages SSE event sequence."""

    def __init__(self, on_text_delta: Callable[[str], None]) -> None:
        self._on_text_delta = on_text_delta
        self.started = False
        self.stopped = False
        self.message_delta_seen = False
        self.stop_reason: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.usage_invalid = False
        self.blocks: dict[int, dict[str, Any]] = {}
        self.open_blocks: set[int] = set()
        self._assistant_bytes = 0

    @staticmethod
    def _token_count(value: Any) -> int | None:
        return value if type(value) is int and 0 <= value <= 2_000_000_000 else None

    def _read_usage(self, usage: Any, *, final: bool = False) -> None:
        if not isinstance(usage, dict):
            return
        if "input_tokens" in usage:
            input_tokens = self._token_count(usage["input_tokens"])
            if input_tokens is None:
                self.usage_invalid = True
            else:
                self.input_tokens = input_tokens
        if final and "output_tokens" in usage:
            output_tokens = self._token_count(usage["output_tokens"])
            if output_tokens is None:
                self.usage_invalid = True
            else:
                self.output_tokens = output_tokens

    def _add_assistant_bytes(self, value: str) -> None:
        try:
            self._assistant_bytes += len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ProviderStreamError("Anthropic 响应文本格式无效。") from None
        if self._assistant_bytes > MAX_STREAM_ASSISTANT_BYTES:
            raise ProviderStreamError("Anthropic 助手内容超过 2 MiB 上限。")

    def handle(self, event_name: str, data: str) -> bool:
        if event_name == "error":
            raise ProviderStreamError("DeepSeek Anthropic 接口返回 SSE 错误事件。")
        try:
            event = _strict_json_loads(data)
        except (ValueError, RecursionError):
            raise ProviderStreamError("Anthropic SSE 事件 JSON 格式无效。", category="parse_error") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ProviderStreamError("Anthropic SSE 事件格式无效。")
        if event_name != event["type"]:
            raise ProviderStreamError("Anthropic SSE 事件名与数据类型不一致。")

        kind = event["type"]
        allowed_event_keys = {
            "ping": {"type"},
            "message_start": {"type", "message"},
            "content_block_start": {"type", "index", "content_block"},
            "content_block_delta": {"type", "index", "delta"},
            "content_block_stop": {"type", "index"},
            "message_delta": {"type", "delta", "usage"},
            "message_stop": {"type"},
        }.get(kind)
        if allowed_event_keys is None or set(event) - allowed_event_keys:
            raise ProviderStreamError("Anthropic SSE 含有尚未支持的事件字段或类型。")
        if kind == "ping":
            return False
        if self.stopped:
            raise ProviderStreamError("Anthropic message_stop 后收到额外事件。")
        if kind == "message_start":
            if self.started:
                raise ProviderStreamError("Anthropic SSE 重复 message_start。")
            message = event.get("message")
            if (
                not isinstance(message, dict)
                or set(message) - {
                    "id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage",
                }
                or message.get("type") != "message"
                or message.get("role") != "assistant"
                or message.get("content") != []
            ):
                raise ProviderStreamError("Anthropic message_start 格式无效。")
            self.started = True
            self._read_usage(message.get("usage"))
            return False

        if not self.started:
            raise ProviderStreamError("Anthropic SSE 在 message_start 前收到内容。")

        if kind == "content_block_start":
            if self.message_delta_seen:
                raise ProviderStreamError("Anthropic message_delta 后收到内容块。")
            index = event.get("index")
            block = event.get("content_block")
            if (
                type(index) is not int
                or not 0 <= index < MAX_ANTHROPIC_CONTENT_BLOCKS
                or index in self.blocks
                or not isinstance(block, dict)
            ):
                raise ProviderStreamError("Anthropic content_block_start 格式无效。")
            block_type = block.get("type")
            if (
                block_type == "text"
                and set(block) == {"type", "text"}
                and block.get("text") == ""
            ):
                current: dict[str, Any] = {"type": "text", "parts": [block["text"]]}
                self._add_assistant_bytes(block["text"])
            elif (
                block_type == "thinking"
                and set(block) <= {"type", "thinking", "signature"}
                and {"type", "thinking"} <= set(block)
                and block.get("thinking") == ""
            ):
                current = {"type": "thinking", "parts": [block["thinking"]]}
                signature = block.get("signature")
                if signature is not None:
                    if not isinstance(signature, str):
                        raise ProviderStreamError("Anthropic thinking signature 格式无效。")
                    current["signature"] = signature
                    self._add_assistant_bytes(signature)
                self._add_assistant_bytes(block["thinking"])
            elif (
                block_type == "tool_use"
                and set(block) == {"type", "id", "name", "input"}
                and isinstance(block.get("id"), str)
                and block.get("id")
                and len(block["id"]) <= 128
                and isinstance(block.get("name"), str)
                and block.get("name")
                and len(block["name"]) <= 128
                and isinstance(block.get("input"), dict)
                and block.get("input") == {}
            ):
                current = {
                    "type": "tool_use",
                    "id": block["id"],
                    "name": block["name"],
                    "input": block["input"],
                    "json_parts": [],
                    "json_bytes": 0,
                }
            else:
                raise ProviderStreamError("Anthropic 响应含有尚未支持的内容块。")
            self.blocks[index] = current
            self.open_blocks.add(index)
            return False

        if kind == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta")
            if type(index) is not int or index not in self.open_blocks or not isinstance(delta, dict):
                raise ProviderStreamError("Anthropic content_block_delta 格式无效。")
            block = self.blocks[index]
            delta_type = delta.get("type")
            if delta_type == "text_delta" and block["type"] == "text":
                if set(delta) != {"type", "text"}:
                    raise ProviderStreamError("Anthropic text_delta 含有尚未支持的字段。")
                fragment = delta.get("text")
                if not isinstance(fragment, str):
                    raise ProviderStreamError("Anthropic text_delta 格式无效。")
                self._add_assistant_bytes(fragment)
                block["parts"].append(fragment)
                if fragment:
                    try:
                        self._on_text_delta(fragment)
                    except Exception:
                        raise ProviderStreamError("无法显示 Anthropic 流式文本。", category="local_output_error") from None
            elif delta_type == "thinking_delta" and block["type"] == "thinking":
                if set(delta) != {"type", "thinking"}:
                    raise ProviderStreamError("Anthropic thinking_delta 含有尚未支持的字段。")
                if "signature" in block:
                    raise ProviderStreamError("Anthropic thinking_delta 出现在签名之后。")
                fragment = delta.get("thinking")
                if not isinstance(fragment, str):
                    raise ProviderStreamError("Anthropic thinking_delta 格式无效。")
                self._add_assistant_bytes(fragment)
                block["parts"].append(fragment)
            elif delta_type == "signature_delta" and block["type"] == "thinking":
                if set(delta) != {"type", "signature"}:
                    raise ProviderStreamError("Anthropic signature_delta 含有尚未支持的字段。")
                signature = delta.get("signature")
                if not isinstance(signature, str) or not signature or "signature" in block:
                    raise ProviderStreamError("Anthropic signature_delta 格式无效。")
                block["signature"] = signature
                self._add_assistant_bytes(signature)
            elif delta_type == "input_json_delta" and block["type"] == "tool_use":
                if set(delta) != {"type", "partial_json"}:
                    raise ProviderStreamError("Anthropic input_json_delta 含有尚未支持的字段。")
                fragment = delta.get("partial_json")
                if not isinstance(fragment, str):
                    raise ProviderStreamError("Anthropic input_json_delta 格式无效。")
                try:
                    block["json_bytes"] += len(fragment.encode("utf-8"))
                except UnicodeEncodeError:
                    raise ProviderStreamError("Anthropic input_json_delta 格式无效。") from None
                if block["json_bytes"] > MAX_PROVIDER_TOOL_ARGUMENT_BYTES:
                    raise ProviderStreamError("Anthropic 工具参数超过本地大小上限。")
                block["json_parts"].append(fragment)
            else:
                raise ProviderStreamError("Anthropic SSE 含有尚未支持的 delta 类型。")
            return False

        if kind == "content_block_stop":
            index = event.get("index")
            if type(index) is not int or index not in self.open_blocks:
                raise ProviderStreamError("Anthropic content_block_stop 格式无效。")
            block = self.blocks[index]
            if block["type"] == "thinking" and not block.get("signature"):
                raise ProviderStreamError("Anthropic thinking 块缺少签名；未执行工具。")
            if block["type"] == "tool_use" and block["json_parts"]:
                try:
                    parsed = json.loads(
                        "".join(block["json_parts"]),
                        object_pairs_hook=_unique_json_object,
                    )
                except (ValueError, RecursionError):
                    raise ProviderStreamError("Anthropic 工具参数 JSON 无效。") from None
                if not isinstance(parsed, dict):
                    raise ProviderStreamError("Anthropic 工具参数必须是 JSON 对象。")
                try:
                    if len(json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_PROVIDER_TOOL_ARGUMENT_BYTES:
                        raise ProviderStreamError("Anthropic 工具参数超过本地大小上限。")
                except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
                    raise ProviderStreamError("Anthropic 工具参数 JSON 无效。") from None
                block["input"] = parsed
            self.open_blocks.remove(index)
            return False

        if kind == "message_delta":
            if self.open_blocks:
                raise ProviderStreamError("Anthropic message_delta 前仍有未结束内容块。")
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise ProviderStreamError("Anthropic message_delta 格式无效。")
            if set(delta) - {"stop_reason", "stop_sequence"}:
                raise ProviderStreamError("Anthropic message_delta 含有尚未支持的字段。")
            reason = delta.get("stop_reason")
            if reason is not None:
                if not isinstance(reason, str) or reason not in {"end_turn", "max_tokens", "stop_sequence", "tool_use"}:
                    raise ProviderStreamError("Anthropic stop_reason 尚未支持。")
                if self.stop_reason is not None and self.stop_reason != reason:
                    raise ProviderStreamError("Anthropic stop_reason 不一致。")
                self.stop_reason = reason
            self.message_delta_seen = True
            self._read_usage(event.get("usage"), final=True)
            return False

        if kind == "message_stop":
            if not self.message_delta_seen or self.stop_reason is None or self.open_blocks:
                raise ProviderStreamError("Anthropic message_stop 前缺少完整 message_delta。")
            self.stopped = True
            tool_count = sum(block["type"] == "tool_use" for block in self.blocks.values())
            if (tool_count > 0) != (self.stop_reason == "tool_use"):
                raise ProviderStreamError("Anthropic 工具调用与 stop_reason 不一致。")
            if tool_count > MAX_PROVIDER_TOOL_CALLS:
                raise ProviderStreamError("Anthropic 工具调用超过本地数量上限。")
            return True

        raise ProviderStreamError("Anthropic SSE 含有尚未支持的事件类型。")

    def payload(self) -> dict[str, Any]:
        if not self.stopped or self.stop_reason is None:
            raise ProviderStreamError("Anthropic SSE 在 message_stop 前结束。")
        content: list[dict[str, Any]] = []
        for index in sorted(self.blocks):
            block = self.blocks[index]
            if block["type"] == "text":
                content.append({"type": "text", "text": "".join(block["parts"])})
            elif block["type"] == "thinking":
                content.append({
                    "type": "thinking",
                    "thinking": "".join(block["parts"]),
                    "signature": block["signature"],
                })
            else:
                content.append({
                    "type": "tool_use",
                    "id": block["id"],
                    "name": block["name"],
                    "input": block["input"],
                })
        usage: dict[str, int] | None = None
        if (
            not self.usage_invalid
            and self.input_tokens is not None
            and self.output_tokens is not None
        ):
            usage = {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}
        return {
            "type": "message",
            "role": "assistant",
            "content": content,
            "stop_reason": self.stop_reason,
            "usage": usage,
        }


class DeepSeekAnthropicProvider:
    """DeepSeek-only adapter for the documented Anthropic Messages-compatible route."""

    label = "deepseek-anthropic"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
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
        if not isinstance(model, str) or model not in DEEPSEEK_ANTHROPIC_MODELS:
            raise ProviderError("DeepSeek Anthropic 兼容模式只支持 deepseek-flash 和 deepseek-v4-pro。")
        base_url = validate_deepseek_anthropic_base_url(
            base_url,
            allow_loopback_http=not api_key,
        )
        parsed = urlsplit(base_url)
        is_loopback = (parsed.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
        if not api_key and not is_loopback:
            raise ProviderError("未提供 API 密钥。")
        self.endpoint = f"{base_url.rstrip('/')}/v1/messages"
        self.base_url = base_url
        self._api_key = api_key
        self.model = model
        self.timeout = float(timeout)
        self.stream_timeout = float(stream_timeout)

    def _request_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int,
        stream: bool,
    ) -> dict[str, Any]:
        if type(max_tokens) is not int or not 1 <= max_tokens <= 65536:
            raise ProviderError("max_tokens must be between 1 and 65536")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ProviderError("Anthropic 请求上下文格式无效。")
        system_parts: list[str] = []
        anth_messages: list[dict[str, Any]] = []
        previous_source_role: str | None = None
        for message in messages:
            if not isinstance(message, dict):
                raise ProviderError("Anthropic 请求消息格式无效。")
            role = message.get("role")
            if role == "system":
                content = message.get("content")
                if not isinstance(content, str):
                    raise ProviderError("Anthropic system 消息格式无效。")
                system_parts.append(content)
                previous_source_role = "system"
                continue
            if role == "tool":
                call_id = message.get("tool_call_id")
                content = message.get("content")
                if not isinstance(call_id, str) or not call_id or len(call_id) > 128 or not isinstance(content, str):
                    raise ProviderError("Anthropic tool_result 消息格式无效。")
                block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
                if previous_source_role == "tool" and anth_messages and anth_messages[-1]["role"] == "user":
                    anth_messages[-1]["content"].append(block)
                else:
                    anth_messages.append({"role": "user", "content": [block]})
                previous_source_role = "tool"
                continue
            if not isinstance(role, str) or role not in {"user", "assistant"}:
                raise ProviderError("Anthropic 请求角色尚未支持。")
            if role == "assistant" and "deepseek_anthropic_content" in message:
                content = message["deepseek_anthropic_content"]
                if not isinstance(content, list) or len(content) > MAX_ANTHROPIC_CONTENT_BLOCKS:
                    raise ProviderError("Anthropic 原生 assistant 内容格式无效。")
            elif role == "assistant" and message.get("tool_calls"):
                if message.get("reasoning_content"):
                    raise ProviderError("无法安全转换无 Anthropic 签名的历史 reasoning 内容。")
                content = []
                for call in message["tool_calls"]:
                    try:
                        function = call["function"]
                        args = _strict_json_loads(function["arguments"])
                        if not isinstance(args, dict):
                            raise ValueError
                        content.append({
                            "type": "tool_use",
                            "id": call["id"],
                            "name": function["name"],
                            "input": args,
                        })
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError, RecursionError):
                        raise ProviderError("Anthropic assistant tool_use 历史格式无效。") from None
            else:
                content = message.get("content", "")
                if not isinstance(content, str):
                    raise ProviderError("Anthropic 请求消息内容格式无效。")
            anth_messages.append({"role": role, "content": content})
            previous_source_role = role

        if not anth_messages:
            raise ProviderError("Anthropic 请求缺少 user/assistant 消息。")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": anth_messages,
            "stream": stream,
            # Disable model-side hidden reasoning by default. DeepSeek documents this
            # toggle; any returned thinking blocks are still validated and preserved.
            "thinking": {"type": "disabled"},
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if tools:
            if len(tools) > MAX_PROVIDER_TOOL_CALLS:
                raise ProviderError("Anthropic tool 定义超过本地上限。")
            converted_tools: list[dict[str, Any]] = []
            for schema in tools:
                try:
                    function = schema["function"]
                    name = function["name"]
                    input_schema = function["parameters"]
                    description = function.get("description", "")
                except (KeyError, TypeError):
                    raise ProviderError("Anthropic tool 定义格式无效。") from None
                if (
                    schema.get("type") != "function"
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(description, str)
                    or not isinstance(input_schema, dict)
                ):
                    raise ProviderError("Anthropic tool 定义格式无效。")
                converted_tools.append({
                    "name": name,
                    "description": description,
                    "input_schema": input_schema,
                })
            body["tools"] = converted_tools
            body["tool_choice"] = {"type": "auto"}
        try:
            if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_STREAM_RESPONSE_BYTES:
                raise ProviderError("Anthropic 请求超过 4 MiB 本地上限。")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise ProviderError("Anthropic 请求内容格式无效。") from None
        return body

    def _request(self, body: dict[str, Any], *, stream: bool) -> urllib.request.Request:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/event-stream" if stream else "application/json",
            "anthropic-version": ANTHROPIC_API_VERSION,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

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
        body = self._request_body(messages, tools, max_tokens=max_tokens, stream=False)
        try:
            with _open_without_redirect(self._request(body, stream=False), timeout=self.timeout) as response:
                raw = response.read(MAX_STREAM_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                exc.close()
            except Exception:
                pass
            safe_status = status if type(status) is int and 100 <= status <= 599 else "未知"
            raise ProviderError(f"DeepSeek Anthropic 接口返回 HTTP {safe_status}。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("连接 DeepSeek Anthropic 接口失败；TLS 和目标主机未放宽。") from None
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider request cancelled after response")
        if len(raw) > MAX_STREAM_RESPONSE_BYTES:
            raise ProviderError("Anthropic 响应超过 4 MiB 上限。")
        try:
            payload = _strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ProviderError("Anthropic 响应格式无效。") from None
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
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider stream cancelled before send")
        if not callable(on_text_delta):
            raise ProviderStreamError("Anthropic 文本回调无效。")
        stream_timeout = self.stream_timeout
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= MAX_STREAM_DURATION_SECONDS
            ):
                raise ProviderStreamError("Anthropic 单次请求时限无效。")
            stream_timeout = min(stream_timeout, float(timeout_seconds))
        body = self._request_body(messages, tools, max_tokens=max_tokens, stream=True)
        request = self._request(body, stream=True)
        started_at = time.monotonic()
        deadline = started_at + stream_timeout
        received_bytes = 0
        http_status: int | None = None
        stage = "connect"
        accumulator = _AnthropicEventAccumulator(on_text_delta)

        def on_event(event_name: str, data: str) -> bool:
            nonlocal stage
            stage = "protocol"
            return accumulator.handle(event_name, data)

        parser = _SSEParser(on_event)

        def diagnostic_error(error: ProviderStreamError, category: str) -> ProviderStreamError:
            status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
            return ProviderStreamError(
                error.safe_message,
                category=category,
                diagnostics=StreamDiagnostics(
                    category=category,
                    stage=stage,
                    http_status=status,
                    received_bytes=received_bytes,
                    elapsed_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                ),
            )

        try:
            with _open_without_redirect(request, timeout=min(self.timeout, stream_timeout)) as response:
                status = getattr(response, "status", None)
                if type(status) is int and 100 <= status <= 599:
                    http_status = status
                stage = "response_headers"
                headers = getattr(response, "headers", None)
                content_type = ""
                if headers is not None:
                    getter = getattr(headers, "get_content_type", None)
                    content_type = getter() if callable(getter) else str(headers.get("Content-Type", ""))
                if content_type.partition(";")[0].strip().casefold() != "text/event-stream":
                    raise ProviderStreamError("DeepSeek Anthropic 接口没有返回 text/event-stream。")
                content_length = headers.get("Content-Length") if headers is not None else None
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_STREAM_RESPONSE_BYTES:
                            raise ProviderStreamError("Anthropic SSE 响应超过 4 MiB 上限。")
                    except ValueError:
                        raise ProviderStreamError("Anthropic SSE Content-Length 格式无效。") from None
                with _SSEReadGuard(response, deadline, cancellation_event) as reader:
                    while True:
                        stage = "response_body"
                        try:
                            chunk = reader.read1(8192)
                        except http.client.IncompleteRead as exc:
                            if isinstance(exc.partial, bytes):
                                received_bytes += len(exc.partial)
                            raise ProviderStreamError("Anthropic SSE 在 message_stop 前断开。", category="missing_end_marker") from None
                        except TimeoutError:
                            raise ProviderStreamError("Anthropic SSE 响应读取超时。", category="read_timeout") from None
                        except (OSError, urllib.error.URLError) as exc:
                            category = _stream_transport_category(exc, stage)
                            message = {
                                "read_timeout": "Anthropic SSE 响应读取超时。",
                                "tls_failure": "Anthropic SSE TLS 安全传输失败。",
                                "remote_disconnect": "Anthropic SSE 对端提前断开连接。",
                                "connection_reset": "Anthropic SSE 连接被对端重置。",
                                "name_resolution_failure": "Anthropic SSE 主机名解析失败。",
                            }.get(category, "Anthropic SSE 网络传输失败。")
                            raise ProviderStreamError(message, category=category) from None
                        if not chunk:
                            stage = "sse_parse"
                            parser.finish()
                            raise ProviderStreamError("Anthropic SSE 在 message_stop 前结束。", category="missing_end_marker")
                        received_bytes += len(chunk)
                        stage = "sse_parse"
                        parser.feed(chunk)
                        if time.monotonic() >= deadline:
                            raise ProviderStreamError("Anthropic SSE 超过总时限。", category="read_timeout")
                        if parser.done:
                            break
        except urllib.error.HTTPError as exc:
            status = exc.code
            if type(status) is int and 100 <= status <= 599:
                http_status = status
            try:
                exc.close()
            except Exception:
                pass
            error = ProviderStreamError(
                f"DeepSeek Anthropic 接口返回 HTTP {http_status}。",
                category="http_status",
            )
            raise diagnostic_error(error, "http_status") from None
        except ProviderStreamError as exc:
            category = exc.category or _stream_error_category(exc, stage)
            raise diagnostic_error(exc, category) from None
        except ProviderCancelled:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            category = _stream_transport_category(exc, stage)
            message = {
                "connect_timeout": "Anthropic SSE 建连或响应头等待超时。",
                "read_timeout": "Anthropic SSE 响应读取超时。",
                "tls_failure": "Anthropic SSE TLS 安全传输失败。",
                "remote_disconnect": "Anthropic SSE 对端提前断开连接。",
                "connection_reset": "Anthropic SSE 连接被对端重置。",
                "name_resolution_failure": "Anthropic SSE 主机名解析失败。",
            }.get(category, "Anthropic SSE 网络传输失败。")
            raise diagnostic_error(ProviderStreamError(message, category=category), category) from None
        except Exception:
            error = ProviderStreamError("Anthropic SSE 流处理失败。", category="internal_error")
            raise diagnostic_error(error, "internal_error") from None
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCancelled("provider stream cancelled")
        return self._turn_from_payload(accumulator.payload())

    def _turn_from_payload(self, payload: Any) -> AssistantTurn:
        if (
            not isinstance(payload, dict)
            or set(payload) - {"id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage"}
            or payload.get("type") != "message"
            or payload.get("role") != "assistant"
            or not isinstance(payload.get("content"), list)
            or len(payload["content"]) > MAX_ANTHROPIC_CONTENT_BLOCKS
        ):
            raise ProviderError("Anthropic 响应格式无效。")
        stop_reason = payload.get("stop_reason")
        if not isinstance(stop_reason, str) or stop_reason not in {"end_turn", "max_tokens", "stop_sequence", "tool_use"}:
            raise ProviderError("Anthropic 响应 stop_reason 无效。")
        content_parts: list[str] = []
        calls: list[ToolCall] = []
        seen_ids: set[str] = set()
        safe_blocks: list[dict[str, Any]] = []
        assistant_bytes = 0
        for block in payload["content"]:
            if not isinstance(block, dict):
                raise ProviderError("Anthropic 响应内容块格式无效。")
            block_type = block.get("type")
            if block_type == "text":
                if set(block) != {"type", "text"}:
                    raise ProviderError("Anthropic 文本块含有尚未支持的字段。")
                text = block.get("text")
                if not isinstance(text, str):
                    raise ProviderError("Anthropic 文本块格式无效。")
                content_parts.append(text)
                safe_blocks.append({"type": "text", "text": text})
                value_for_size = text
            elif block_type == "thinking":
                if set(block) != {"type", "thinking", "signature"}:
                    raise ProviderError("Anthropic thinking 块含有尚未支持的字段。")
                thinking = block.get("thinking")
                signature = block.get("signature")
                if not isinstance(thinking, str) or not isinstance(signature, str) or not signature:
                    raise ProviderError("Anthropic thinking 块缺少有效文本或签名。")
                safe_blocks.append({"type": "thinking", "thinking": thinking, "signature": signature})
                value_for_size = thinking + signature
            elif block_type == "tool_use":
                if set(block) != {"type", "id", "name", "input"}:
                    raise ProviderError("Anthropic tool_use 块含有尚未支持的字段。")
                call_id = block.get("id")
                name = block.get("name")
                arguments = block.get("input")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or len(call_id) > 128
                    or any(ord(char) < 32 for char in call_id)
                    or call_id in seen_ids
                    or not isinstance(name, str)
                    or not name
                    or len(name) > 128
                    or any(ord(char) < 32 for char in name)
                    or not isinstance(arguments, dict)
                ):
                    raise ProviderError("Anthropic tool_use 块格式无效。")
                try:
                    raw_arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    argument_bytes = len(raw_arguments.encode("utf-8"))
                except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
                    raise ProviderError("Anthropic tool_use 参数格式无效。") from None
                if argument_bytes > MAX_PROVIDER_TOOL_ARGUMENT_BYTES:
                    raise ProviderError("Anthropic 工具参数超过本地大小上限。")
                seen_ids.add(call_id)
                calls.append(ToolCall(call_id, name, arguments))
                safe_blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
                value_for_size = raw_arguments
            else:
                raise ProviderError("Anthropic 响应含有尚未支持的内容块。")
            try:
                assistant_bytes += len(value_for_size.encode("utf-8"))
            except UnicodeEncodeError:
                raise ProviderError("Anthropic 响应文本格式无效。") from None
            if assistant_bytes > MAX_STREAM_ASSISTANT_BYTES:
                raise ProviderError("Anthropic 助手内容超过 2 MiB 上限。")
            if len(calls) > MAX_PROVIDER_TOOL_CALLS:
                raise ProviderError("Anthropic 工具调用超过本地数量上限。")
        if (bool(calls)) != (stop_reason == "tool_use"):
            raise ProviderError("Anthropic 工具调用与 stop_reason 不一致；未执行工具。")
        usage_value = payload.get("usage")
        usage: ProviderUsage | None = None
        if isinstance(usage_value, dict):
            prompt = usage_value.get("input_tokens")
            completion = usage_value.get("output_tokens")
            if (
                type(prompt) is int
                and 0 <= prompt <= 2_000_000_000
                and type(completion) is int
                and 0 <= completion <= 2_000_000_000
            ):
                usage = ProviderUsage(prompt, completion, prompt + completion)
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
            "deepseek_anthropic_content": safe_blocks,
        }
        return AssistantTurn(
            content="".join(content_parts),
            tool_calls=calls,
            assistant_message=assistant_message,
            usage=usage,
            finish_reason=stop_reason,
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
        """Keep the common subagent boundary: exact canonical read tools only."""
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
        allowed_names = {"read_file", "list_directory"}
        tools = [] if tools is None else tools
        if not isinstance(tools, list) or len(tools) > 2:
            raise ProviderError("子代理只读工具列表格式无效。")
        from .tools import TOOL_SCHEMAS
        canonical = {
            schema["function"]["name"]: schema
            for schema in TOOL_SCHEMAS
            if schema["function"]["name"] in allowed_names
        }
        seen: set[str] = set()
        for schema in tools:
            try:
                name = schema["function"]["name"]
            except (KeyError, TypeError):
                raise ProviderError("子代理只读工具列表格式无效。") from None
            if name not in allowed_names or name in seen or schema != canonical[name]:
                raise ProviderError("子代理只允许使用只读文件工具。")
            seen.add(name)
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
        started = time.monotonic()
        try:
            result = self.complete_streaming(
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
        if len(result.tool_calls) > MAX_SUBAGENT_TOOL_CALLS_PER_TURN:
            raise ProviderError("子代理只读调用数超过本地上限。")
        return result


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
        if api_key:
            # An explicit key override is still a DeepSeek key. Never let that
            # test/embedding hook widen the destination allowlist.
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
    if provider_name == "deepseek-anthropic":
        model = model_override or settings.get("HARNESS_MODEL", "deepseek-flash")
        base_url = (
            base_url_override
            or settings.get("HARNESS_BASE_URL", DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL)
        )
        api_key = (
            api_key_override
            if api_key_override is not None
            else settings.get(DEEPSEEK_API_KEY_ENV, "")
        )
        base_url = validate_deepseek_anthropic_base_url(
            base_url,
            allow_loopback_http=not api_key,
        )
        return DeepSeekAnthropicProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=request_timeout_seconds,
            stream_timeout=request_timeout_seconds,
        )
    raise ProviderError(f"未知模型提供商：{provider_name}")
