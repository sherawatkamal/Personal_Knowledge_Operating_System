"""Systems under test. The harness only sees this interface; configs choose the implementation."""

from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg

from pkos.eval.configs import ConfigError, SystemConfig
from pkos.eval.questions import Question


@dataclass(frozen=True)
class Retrieved:
    episode_id: int
    item_kind: str = "episode"  # chunk | fact | commitment from step 4 on
    item_id: int | None = None
    span: tuple[int, int] | None = None
    scores: dict[str, float] = field(default_factory=dict)


@dataclass
class Usage:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    queued_ms: float = 0.0  # rate-limit waiting, excluded from latency


@dataclass
class Answer:
    text: str | None  # None means the system abstained
    retrieved: list[Retrieved] = field(default_factory=list)
    cited: list[int] = field(default_factory=list)  # indexes into `retrieved`
    usage: Usage = field(default_factory=Usage)

    @property
    def abstained(self) -> bool:
        return self.text is None

    @property
    def cited_episode_ids(self) -> list[int]:
        return [self.retrieved[i].episode_id for i in self.cited]


@dataclass(frozen=True)
class Context:
    """What a system may read. `gold_episode_ids` exists only for diagnostic systems."""

    conn: psycopg.Connection
    gold_episode_ids: tuple[int, ...]


class System(Protocol):
    def answer(self, question: Question, ctx: Context) -> Answer: ...


class NullSystem:
    """Always abstains. The floor every real configuration must beat."""

    def answer(self, question: Question, ctx: Context) -> Answer:
        return Answer(text=None)


class OracleSystem:
    """Reads the gold answer and gold sources. The ceiling, used to check the harness itself.

    A harness that doesn't score the oracle at 1.0 is broken. Diagnostic only: never published.
    """

    def answer(self, question: Question, ctx: Context) -> Answer:
        if not question.answerable:
            return Answer(text=None)
        retrieved = [Retrieved(eid) for eid in ctx.gold_episode_ids]
        return Answer(
            text=question.gold_answer, retrieved=retrieved, cited=list(range(len(retrieved)))
        )


class RetrievalSystem:
    """Runs the same pipeline as `pkos ask`, with the config's retrieval and answer settings."""

    def __init__(self, config: SystemConfig, embedder, llm_factory):
        self.config, self.embedder, self.llm_factory = config, embedder, llm_factory

    def answer(self, question: Question, ctx: Context) -> Answer:
        from pkos.answer.pipeline import ask

        res = ask(
            ctx.conn,
            self.config.raw,
            question.question,
            embedder=self.embedder,
            llm_factory=self.llm_factory,
        )
        retrieved = [
            Retrieved(h.episode_id, "chunk", h.chunk_id, h.span, dict(h.scores))
            for h in res.retrieved
        ]
        index = {h.chunk_id: i for i, h in enumerate(res.retrieved)}
        c = res.completion
        usage = Usage(1, c.input_tokens, c.output_tokens, c.cost_usd, c.queued_ms) if c else Usage()
        return Answer(res.text, retrieved, [index[h.chunk_id] for h in res.cited_hits], usage)


def build(config: SystemConfig, *, embedder=None, llm_factory=None) -> System:
    match config.kind:
        case "null":
            return NullSystem()
        case "oracle":
            if not config.diagnostic:
                raise ConfigError(f"{config.name}: an oracle config must set diagnostic = true")
            return OracleSystem()
        case "retrieval":
            if llm_factory is None:
                raise ConfigError(f"{config.name}: retrieval systems need an LLM factory")
            modes = config.raw.get("retrieval", {}).get("modes", [])
            if "vector" in modes and embedder is None:
                raise ConfigError(f"{config.name}: vector mode needs an embedder")
            return RetrievalSystem(config, embedder, llm_factory)
    raise ConfigError(f"{config.name}: unknown kind {config.kind!r}")


def describe(config: SystemConfig) -> dict[str, Any]:
    return {
        "name": config.name,
        "kind": config.kind,
        "sha256": config.sha256,
        "diagnostic": config.diagnostic,
    }
