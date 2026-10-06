"""Business logic never calls a provider directly: only src/pkos/llm/ may."""

import re
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "pkos"
FORBIDDEN = re.compile(
    r"^\s*(from|import)\s+(pkos\.llm\.(groq|ollama)|openai|groq|anthropic|ollama)\b", re.M
)


def test_only_llm_package_imports_providers():
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "llm" not in p.relative_to(SRC).parts and FORBIDDEN.search(p.read_text())
    ]
    assert offenders == [], f"provider imported outside pkos.llm: {offenders}"
