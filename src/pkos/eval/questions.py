"""Question set loading and validation. Malformed rows fail loudly, all at once."""

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

SOURCES = ("granola", "gmail", "gcal", "slack")
CLASSES = ("lookup", "relational", "temporal", "aggregate", "obligational")
SPLITS = ("dev", "test")
ORIGINS = ("user", "drafted", "near_miss")
JUDGES = ("exact", "model")
COLUMNS = (
    "id",
    "question",
    "class",
    "answerable",
    "gold_answer",
    "gold_sources",
    "judge",
    "sources_needed",
    "split",
    "origin",
)


class QuestionSetError(ValueError):
    pass


@dataclass(frozen=True)
class GoldSource:
    source: str
    external_id: str

    def __str__(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    qclass: str
    answerable: bool
    gold_answer: str
    gold_sources: tuple[GoldSource, ...]
    judge: str
    sources_needed: tuple[str, ...]
    split: str
    origin: str


@dataclass(frozen=True)
class QuestionSet:
    path: Path
    sha256: str
    questions: tuple[Question, ...]

    @property
    def split_map(self) -> dict[str, str]:
        return {q.id: q.split for q in self.questions}


def _list(cell: str) -> list[str]:
    return [p.strip() for p in cell.split(";") if p.strip()]


def load(path: Path) -> QuestionSet:
    data = path.read_bytes()
    rows = list(csv.DictReader(data.decode("utf-8-sig").splitlines()))
    errors: list[str] = []
    if not rows:
        raise QuestionSetError(f"{path}: no questions")
    missing = [c for c in COLUMNS if c not in rows[0]]
    if missing:
        raise QuestionSetError(f"{path}: missing columns: {', '.join(missing)}")

    questions, seen = [], set()
    for line, r in enumerate(rows, start=2):

        def err(msg: str, line: int = line, r: dict = r) -> None:
            errors.append(f"line {line} ({r.get('id') or '?'}): {msg}")

        qid = (r["id"] or "").strip()
        if not qid:
            err("empty id")
        elif qid in seen:
            err("duplicate id")
        seen.add(qid)
        if not (r["question"] or "").strip():
            err("empty question")
        for col, allowed in (
            ("class", CLASSES),
            ("split", SPLITS),
            ("origin", ORIGINS),
            ("judge", JUDGES),
        ):
            if r[col].strip() not in allowed:
                err(f"{col} {r[col]!r} not in {allowed}")
        ans = r["answerable"].strip().lower()
        if ans not in ("true", "false"):
            err(f"answerable must be true or false, got {r['answerable']!r}")
        answerable = ans == "true"

        gold: list[GoldSource] = []
        for token in _list(r["gold_sources"]):
            src, sep, ext = token.partition(":")
            if not sep or not ext or src not in SOURCES:
                err(f"gold source {token!r} is not source:external_id with a known source")
            else:
                gold.append(GoldSource(src, ext))
        needed = _list(r["sources_needed"])
        for src in needed:
            if src not in SOURCES:
                err(f"sources_needed {src!r} not in {SOURCES}")
        for g in gold:
            if g.source not in needed:
                err(f"gold source {g} needs {g.source!r} listed in sources_needed")

        if answerable and (not r["gold_answer"].strip() or not gold):
            err("answerable questions need gold_answer and gold_sources")
        if not answerable and (r["gold_answer"].strip() or gold):
            err("unanswerable questions must have empty gold_answer and gold_sources")
        if r["origin"].strip() == "near_miss" and answerable:
            err("near_miss questions are unanswerable by definition")

        questions.append(
            Question(
                id=qid,
                question=r["question"].strip(),
                qclass=r["class"].strip(),
                answerable=answerable,
                gold_answer=r["gold_answer"].strip(),
                gold_sources=tuple(gold),
                judge=r["judge"].strip(),
                sources_needed=tuple(needed),
                split=r["split"].strip(),
                origin=r["origin"].strip(),
            )
        )
    if errors:
        raise QuestionSetError(f"{path}: {len(errors)} problem(s):\n  " + "\n  ".join(errors))
    return QuestionSet(path, hashlib.sha256(data).hexdigest(), tuple(questions))
