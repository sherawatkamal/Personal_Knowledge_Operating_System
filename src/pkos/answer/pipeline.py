"""One ask pipeline for the CLI, the eval harness and (step 4b) the UI: what you see is what
the harness measures."""

from collections.abc import Callable
from typing import Any

import psycopg

from pkos.answer import stage
from pkos.embed.base import Embedder
from pkos.llm import LLM
from pkos.retrieve import chunks


class PipelineConfigError(ValueError):
    pass


def ask(
    conn: psycopg.Connection,
    config: dict[str, Any],
    question: str,
    *,
    embedder: Embedder | None,
    llm_factory: Callable[[str], LLM],
) -> stage.AnswerResult:
    r, a = config.get("retrieval", {}), config.get("answer", {})
    if r.get("corpora", ["chunks"]) != ["chunks"]:
        raise PipelineConfigError("only the 'chunks' corpus exists until step 9")
    modes = list(r.get("modes", ["fts", "vector"]))
    hits = chunks.search(
        conn,
        embedder if "vector" in modes else None,
        question,
        modes=modes,
        pool=int(r.get("pool", 50)),
        rrf_k=int(r.get("rrf_k", 60)),
    )
    if "llm" not in a:
        raise PipelineConfigError("[answer] llm = <profile> is required")
    return stage.answer(
        llm_factory(a["llm"]),
        question,
        hits,
        context_tokens=int(a.get("context_tokens", 4500)),
        max_items=int(r.get("k", 20)),
        min_top_similarity=float(a.get("min_top_similarity", 0.0)),
    )
