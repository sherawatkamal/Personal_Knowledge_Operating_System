"""Profiles from configs/llm.toml -> LLM instances."""

import tomllib
from pathlib import Path

from pkos.config import REPO_ROOT, Settings, get_settings
from pkos.llm.base import LLM, LLMError, Profile

LLM_CONFIG = REPO_ROOT / "configs" / "llm.toml"


def load_profiles(path: Path = LLM_CONFIG) -> dict[str, Profile]:
    doc = tomllib.loads(path.read_text())
    out = {}
    for name, p in doc.get("profiles", {}).items():
        if p.get("provider") not in ("groq", "ollama"):
            raise LLMError(f"profile {name}: unknown provider {p.get('provider')!r}")
        out[name] = Profile(name=name, **p)
    return out


_instances: dict[str, LLM] = {}


def get_llm(name: str, settings: Settings | None = None) -> LLM:
    """One instance per profile per process, so its rate-limit bucket is shared."""
    if name in _instances:
        return _instances[name]
    _instances[name] = _build(name, settings or get_settings())
    return _instances[name]


def _build(name: str, settings: Settings) -> LLM:
    profiles = load_profiles()
    if name not in profiles:
        raise LLMError(f"unknown model profile {name!r} (have: {', '.join(profiles)})")
    profile = profiles[name]
    if profile.provider == "groq":
        from pkos.llm.groq import GroqLLM

        if not settings.groq_api_key:
            raise LLMError(f"profile {name} needs PKOS_GROQ_API_KEY in .env")
        return GroqLLM(profile, settings.groq_api_key.get_secret_value())
    from pkos.llm.ollama import OllamaLLM

    return OllamaLLM(profile, settings.ollama_url)
