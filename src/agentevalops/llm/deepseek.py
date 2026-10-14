"""DeepSeek adapter, speaking the OpenAI-compatible chat completions API."""

from __future__ import annotations

import time
from typing import Any, Sequence

from .base import (
    ChatMessage,
    LLMError,
    LLMResponse,
    LLMUnavailableError,
    MissingCredentialError,
    Usage,
)

DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekClient:
    """Thin, testable wrapper over the provider call path.

    The ``openai`` SDK is imported lazily so the rest of the platform (and the whole
    offline test suite) keeps working without it installed. Pass ``client=`` to
    inject a stub and exercise this class without any network access.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise MissingCredentialError("DeepSeekClient 需要一个非空的 api_key")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise LLMUnavailableError(
                    '缺少 openai SDK，请执行: pip install -e ".[llm]"'
                ) from exc
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout_s,
                max_retries=self._max_retries,
            )
        return self._client

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        client = self._ensure_client()
        started = time.perf_counter()
        try:
            completion = client.chat.completions.create(
                model=self._model,
                messages=[message.to_dict() for message in messages],
                temperature=self._temperature if temperature is None else temperature,
                max_tokens=self._max_tokens if max_tokens is None else max_tokens,
            )
        except LLMError:
            # Never double-wrap our own error types.
            raise
        except Exception as exc:
            # Deliberately broad: provider SDKs raise an unpredictable family of
            # transport/auth/rate-limit exceptions, and this is the boundary layer.
            raise LLMUnavailableError(
                f"DeepSeek 调用失败: {type(exc).__name__}: {exc}"
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0
        return LLMResponse(
            text=self._extract_text(completion),
            model=getattr(completion, "model", None) or self._model,
            usage=self._extract_usage(completion),
            latency_ms=latency_ms,
            # Providers may serve a different model than requested (aliases), so keep
            # both ids: the manifest pins what we asked for, the trace shows what ran.
            requested_model=self._model,
        )

    @staticmethod
    def _extract_text(completion: Any) -> str:
        choices = getattr(completion, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        return getattr(message, "content", None) or ""

    @staticmethod
    def _usage_field(usage: Any, name: str) -> int:
        """Read a usage field, tolerating both SDK objects and plain stubs."""
        dumped = usage.model_dump() if hasattr(usage, "model_dump") else None
        if isinstance(dumped, dict) and name in dumped:
            return int(dumped[name] or 0)
        return int(getattr(usage, name, 0) or 0)

    @classmethod
    def _extract_usage(cls, completion: Any) -> Usage:
        usage = getattr(completion, "usage", None)
        if usage is None:
            return Usage()
        return Usage(
            prompt_tokens=cls._usage_field(usage, "prompt_tokens"),
            completion_tokens=cls._usage_field(usage, "completion_tokens"),
            cache_hit_tokens=cls._usage_field(usage, "prompt_cache_hit_tokens"),
        )
