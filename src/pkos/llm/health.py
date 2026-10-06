"""Provider reachability for `pkos health`. Lives in pkos.llm so nothing else talks to providers."""

import httpx

from pkos.config import Settings
from pkos.llm.groq import BASE_URL as GROQ_URL
from pkos.llm.registry import load_profiles


def check_providers(settings: Settings) -> list[tuple[str, str, str]]:
    """(name, level, detail), level in ok | warn | fail. Unconfigured or unreachable optional
    providers warn; a configured key that the provider rejects fails."""
    profiles = load_profiles().values()
    out = []
    if not settings.groq_api_key:
        out.append(("groq", "warn", "not configured (PKOS_GROQ_API_KEY)"))
    else:
        try:
            r = httpx.get(
                f"{GROQ_URL}/models",
                timeout=10,
                headers={"Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}"},
            )
            if r.status_code == 200:
                have = {m["id"] for m in r.json().get("data", [])}
                need = {p.model for p in profiles if p.provider == "groq"}
                missing = sorted(need - have)
                out.append(
                    (
                        "groq",
                        "fail" if missing else "ok",
                        f"key accepted; missing models: {', '.join(missing)}"
                        if missing
                        else f"key accepted; {len(need)} profile model(s) available",
                    )
                )
            else:
                out.append(("groq", "fail", f"key rejected (HTTP {r.status_code})"))
        except httpx.TransportError as e:
            out.append(("groq", "warn", f"unreachable ({type(e).__name__})"))
    want = sorted({p.model for p in profiles if p.provider == "ollama"})
    try:
        r = httpx.get(f"{settings.ollama_url}/api/tags", timeout=5)
        have = {m["name"] for m in r.json().get("models", [])}
        missing = [m for m in want if m not in have and f"{m}:latest" not in have]
        out.append(
            (
                "ollama",
                "warn" if missing else "ok",
                f"running; model(s) not pulled: {', '.join(missing)}"
                if missing
                else f"running; {', '.join(want)} available",
            )
        )
    except (httpx.TransportError, ValueError):
        out.append(
            (
                "ollama",
                "warn",
                f"not reachable at {settings.ollama_url} (local configurations unavailable)",
            )
        )
    return out
