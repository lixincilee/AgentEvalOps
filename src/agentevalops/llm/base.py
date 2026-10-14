"""Provider-agnostic chat contracts.

Nothing in this module imports a third-party package: the deterministic test suite
must keep running with no network and no provider SDK installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable


class LLMError(RuntimeError):
    """Base class for every model-layer failure."""


class LLMUnavailableError(LLMError):
    """Network failure, timeout, rate limit, or provider 5xx."""


class MissingCredentialError(LLMError):
    """No usable API key was configured."""


class BudgetExceededError(LLMError):
    """A run went past its configured spend ceiling."""


class UnknownModelPriceError(LLMError):
    """A call used a model with no configured price, so its cost is unknowable.

    Silently treating such a call as free would understate spend and defeat the
    budget guard, so this is an error by default.
    """


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls("system", content)

    @classmethod
    def user(cls, content: str) -> "ChatMessage":
        return cls("user", content)

    @classmethod
    def assistant(cls, content: str) -> "ChatMessage":
        return cls("assistant", content)

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class Usage:
    """Token accounting. Required for the cost-per-success metric."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cache_hit_tokens + other.cache_hit_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    cached: bool = False
    requested_model: str | None = None
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "requested_model": self.requested_model,
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
            "cached": self.cached,
        }


@runtime_checkable
class LLMClient(Protocol):
    @property
    def model_id(self) -> str: ...

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


APPROX_CHARS_PER_TOKEN = 4


def approx_tokens(text: str) -> int:
    """Cheap deterministic token estimate for offline runs.

    Only ever used by the fake client; real usage always comes from the provider.
    """
    return max(1, len(text) // APPROX_CHARS_PER_TOKEN) if text else 0
