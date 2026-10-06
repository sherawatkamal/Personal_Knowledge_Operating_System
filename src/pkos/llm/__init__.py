"""The one door to language models. Business logic calls `get_llm(profile).complete(...)` and
never imports a provider module or SDK (enforced by tests/unit/test_llm_boundary.py)."""

from pkos.llm.base import (
    LLM,
    Completion,
    DailyLimitReached,
    LLMError,
    Profile,
    RequestTooLarge,
    estimate_tokens,
)
from pkos.llm.registry import get_llm, load_profiles
from pkos.llm.usage import usage

__all__ = [
    "LLM",
    "Completion",
    "DailyLimitReached",
    "LLMError",
    "Profile",
    "RequestTooLarge",
    "estimate_tokens",
    "get_llm",
    "load_profiles",
    "usage",
]
