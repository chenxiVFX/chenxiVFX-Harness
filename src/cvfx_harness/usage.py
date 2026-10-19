from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass
from typing import Any

DEFAULT_TOKEN_BUDGET = 16_384


@dataclass(frozen=True)
class ProviderUsage:
    """Token counts reported by a provider, when its response includes them."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_prompt_tokens: int | None = None


@dataclass
class UsageStats:
    requests: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    provider_usage_reports: int = 0
    provider_prompt_tokens: int = 0
    provider_completion_tokens: int = 0
    provider_cached_prompt_reports: int = 0
    provider_cached_prompt_tokens: int = 0

    def record_request(self, estimated_input_tokens: int) -> None:
        self.requests += 1
        self.estimated_input_tokens += max(0, estimated_input_tokens)

    def record_response(
        self,
        estimated_output_tokens: int,
        provider_usage: ProviderUsage | None,
    ) -> None:
        self.estimated_output_tokens += max(0, estimated_output_tokens)
        if provider_usage is not None:
            self.provider_usage_reports += 1
            self.provider_prompt_tokens += provider_usage.prompt_tokens
            self.provider_completion_tokens += provider_usage.completion_tokens
            if provider_usage.cached_prompt_tokens is not None:
                self.provider_cached_prompt_reports += 1
                self.provider_cached_prompt_tokens += provider_usage.cached_prompt_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "provider_usage_reports": self.provider_usage_reports,
            "provider_prompt_tokens": self.provider_prompt_tokens,
            "provider_completion_tokens": self.provider_completion_tokens,
            "provider_cached_prompt_reports": self.provider_cached_prompt_reports,
            "provider_cached_prompt_tokens": self.provider_cached_prompt_tokens,
        }

    @classmethod
    def from_dict(cls, value: Any) -> UsageStats:
        if not isinstance(value, dict):
            raise ValueError("usage counters must be an object")
        fields = (
            "requests",
            "estimated_input_tokens",
            "estimated_output_tokens",
            "provider_usage_reports",
            "provider_prompt_tokens",
            "provider_completion_tokens",
            "provider_cached_prompt_reports",
            "provider_cached_prompt_tokens",
        )
        if set(value) - set(fields):
            raise ValueError("usage counters contain unknown fields")
        counts: dict[str, int] = {}
        for field in fields:
            candidate = value.get(field, 0)
            if type(candidate) is not int or candidate < 0:
                raise ValueError("usage counters must be non-negative integers")
            counts[field] = candidate
        return cls(**counts)


def estimate_tokens(text: str) -> int:
    """Rough offline estimate; it is not a tokenizer or provider usage report."""
    wide = 0
    other = 0
    for char in text:
        if char.isspace():
            continue
        if unicodedata.east_asian_width(char) in {"W", "F"} or ord(char) > 0xFFFF:
            wide += 1
        else:
            other += 1
    return wide + math.ceil(other / 4)


def estimate_payload_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> int:
    payload = {"messages": messages, "tools": tools or []}
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return estimate_tokens(serialized)
