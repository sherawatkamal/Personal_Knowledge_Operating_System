"""Gold resolution (skip reasons), judging, and metrics."""

import hashlib
import json
import string
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg

from pkos.eval.questions import Question
from pkos.eval.systems import Answer

# ---------------------------------------------------------------- resolution (P3)

SKIP_SOURCE_NOT_CONNECTED = "source_not_connected"
SKIP_GOLD_NOT_FOUND = "gold_not_found"
SKIP_GOLD_TOMBSTONED = "gold_tombstoned"
SKIP_REASONS = (SKIP_SOURCE_NOT_CONNECTED, SKIP_GOLD_NOT_FOUND, SKIP_GOLD_TOMBSTONED)


@dataclass(frozen=True)
class Resolution:
    skip_reason: str | None  # None means scored
    detail: str = ""
    gold_episode_ids: tuple[int, ...] = ()

    @property
    def scored(self) -> bool:
        return self.skip_reason is None


def resolve(conn: psycopg.Connection, questions: list[Question]) -> dict[str, Resolution]:
    """Never raises for a missing gold source: it skips the question with a reason."""
    connected = {
        r[0]
        for r in conn.execute("SELECT source FROM sync_state WHERE last_success_at IS NOT NULL")
    }
    pairs = {(g.source, g.external_id) for q in questions for g in q.gold_sources}
    found: dict[tuple[str, str], tuple[int, bool]] = {}
    if pairs:
        srcs, exts = zip(*pairs, strict=True)
        # Reads the base table on purpose: the harness must tell "tombstoned" from "missing".
        for eid, src, ext, deleted in conn.execute(
            """SELECT e.id, e.source, e.external_id, e.deleted_at IS NOT NULL
               FROM episodes e JOIN unnest(%s::text[], %s::text[]) AS g(source, external_id)
               USING (source, external_id)""",
            (list(srcs), list(exts)),
        ):
            found[(src, ext)] = (eid, deleted)

    out: dict[str, Resolution] = {}
    for q in questions:
        missing_sources = [s for s in q.sources_needed if s not in connected]
        if missing_sources:
            out[q.id] = Resolution(SKIP_SOURCE_NOT_CONNECTED, ", ".join(missing_sources))
            continue
        not_found = [str(g) for g in q.gold_sources if (g.source, g.external_id) not in found]
        if not_found:
            out[q.id] = Resolution(SKIP_GOLD_NOT_FOUND, ", ".join(not_found))
            continue
        tomb = [str(g) for g in q.gold_sources if found[(g.source, g.external_id)][1]]
        if tomb:
            out[q.id] = Resolution(SKIP_GOLD_TOMBSTONED, ", ".join(tomb))
            continue
        ids = tuple(found[(g.source, g.external_id)][0] for g in q.gold_sources)
        out[q.id] = Resolution(None, gold_episode_ids=ids)
    return out


# ---------------------------------------------------------------- judging


class JudgeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Judgement:
    correct: bool
    method: str  # abstain_rule | exact | model


_PUNCT = {ord(c): " " for c in string.punctuation + "“”‘’–—…"}


def normalize_answer(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().translate(_PUNCT)
    return " ".join(text.split())


class JudgeCache:
    """Append-only JSONL cache, so re-scoring unchanged answers costs zero model calls."""

    def __init__(self, path: Path):
        self.path = path
        self._entries: dict[str, bool] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._entries[row["key"]] = row["correct"]

    @staticmethod
    def key(
        q: Question, answer: str, gold_ids: tuple[int, ...], model: str, prompt_sha: str
    ) -> str:
        doc = json.dumps([q.question, answer, q.gold_answer, sorted(gold_ids), model, prompt_sha])
        return hashlib.sha256(doc.encode()).hexdigest()

    def get(self, key: str) -> bool | None:
        return self._entries.get(key)

    def put(self, key: str, correct: bool) -> None:
        self._entries[key] = correct
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({"key": key, "correct": correct}) + "\n")


# A model judge: (question, answer, gold_ids) -> correct. Wired in step 4 via the llm layer.
ModelJudgeFn = Callable[[Question, str, tuple[int, ...]], bool]


@dataclass
class Judge:
    model_judge: ModelJudgeFn | None = None
    model_id: str = ""
    prompt_sha: str = ""
    cache: JudgeCache | None = None
    model_calls: int = 0

    def judge(self, q: Question, answer: Answer, gold_ids: tuple[int, ...]) -> Judgement:
        if not q.answerable:
            return Judgement(answer.abstained, "abstain_rule")
        if answer.abstained:
            return Judgement(False, "abstain_rule")
        if normalize_answer(answer.text) == normalize_answer(q.gold_answer):
            return Judgement(True, "exact")
        if q.judge == "exact":
            return Judgement(False, "exact")
        if self.model_judge is None:
            raise JudgeUnavailable(f"{q.id}: needs a model judge, which arrives in step 4")
        key = JudgeCache.key(q, answer.text, gold_ids, self.model_id, self.prompt_sha)
        if self.cache is not None and (hit := self.cache.get(key)) is not None:
            return Judgement(hit, "model")
        correct = self.model_judge(q, answer.text, gold_ids)
        self.model_calls += 1
        if self.cache is not None:
            self.cache.put(key, correct)
        return Judgement(correct, "model")


# ---------------------------------------------------------------- metrics


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))  # ceil
    return ordered[int(rank) - 1]


def ratio(num: int, den: int) -> float | None:
    return num / den if den else None
