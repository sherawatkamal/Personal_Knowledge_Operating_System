"""Provider-neutral types."""

from dataclasses import dataclass
from typing import Any, Protocol

Message = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}


class LLMError(Exception):
    pass


class RequestTooLarge(LLMError):
    """The request cannot fit the model's per-minute token limit. A caller bug: shrink it."""


class DailyLimitReached(LLMError):
    """The provider asked us to wait longer than a run should. Stop cleanly; resume later."""


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str  # groq | ollama
    model: str
    max_output_tokens: int
    reasoning_effort: str | None = None
    tpm_limit: int | None = None
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    temperature: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.price_in_per_m + output_tokens * self.price_out_per_m) / 1e6

    @property
    def max_input_tokens(self) -> int | None:
        """Largest prompt (estimated) that still fits one minute's budget with full output."""
        if self.tpm_limit is None:
            return None
        return int(self.tpm_limit * 0.95) - self.max_output_tokens


@dataclass(frozen=True)
class Completion:
    text: str
    data: dict[str, Any] | None  # parsed JSON when a schema was requested
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: float
    profile: str
    model: str


class LLM(Protocol):
    profile: Profile

    def complete(
        self, messages: list[Message], *, purpose: str, schema: dict[str, Any] | None = None
    ) -> Completion: ...


def estimate_prompt_tokens(messages: list[Message]) -> int:
    """Conservative: ~3 characters per token plus per-message overhead. Over-estimating keeps us
    under a hard per-minute limit; real counts come back in the response for accounting."""
    return sum(len(m["content"]) for m in messages) // 3 + 8 * len(messages) + 16


def estimate_tokens(messages: list[Message], max_output_tokens: int) -> int:
    return estimate_prompt_tokens(messages) + max_output_tokens
