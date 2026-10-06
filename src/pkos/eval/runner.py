"""Run configurations over a question set: freeze check, test ledger (F1), results on disk."""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from pkos.config import REPO_ROOT
from pkos.eval import systems
from pkos.eval.configs import SystemConfig
from pkos.eval.questions import Question, QuestionSet
from pkos.eval.scoring import Judge, Resolution, resolve


@dataclass
class QResult:
    answer: str | None
    abstained: bool
    correct: bool
    method: str
    retrieved_episode_ids: list[int]
    cited_episode_ids: list[int]
    latency_ms: float
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    error: str | None = None
    queued_ms: float = 0.0


@dataclass
class ConfigRun:
    config: SystemConfig
    results: dict[str, QResult] = field(default_factory=dict)
    marked: bool = False  # † : config changed after a previous test-set look
    looks: int = 0  # test-set looks including this one


@dataclass
class FreezeStatus:
    state: str  # not_frozen | ok | changed
    messages: list[str] = field(default_factory=list)


@dataclass
class EvalRun:
    qset: QuestionSet
    split: str
    questions: list[Question]
    resolutions: dict[str, Resolution]
    runs: list[ConfigRun]
    freeze: FreezeStatus
    snapshot: dict[str, Any]
    git_commit: str
    started_at: str
    warnings: list[str] = field(default_factory=list)
    out_dir: Path | None = None

    @property
    def scored_ids(self) -> list[str]:
        return [q.id for q in self.questions if self.resolutions[q.id].scored]

    @property
    def common_ids(self) -> list[str]:
        """Questions scored without error in EVERY configuration: the only fair comparison set."""
        return [
            qid
            for qid in self.scored_ids
            if all(qid in r.results and r.results[qid].error is None for r in self.runs)
        ]


# ---------------------------------------------------------------- freeze


def _state_dir(data_dir: Path) -> Path:
    return data_dir / "eval"


def _freeze_path(qset: QuestionSet, data_dir: Path) -> Path:
    return _state_dir(data_dir) / f"{qset.path.stem}.frozen.json"


def _split_sha(split_map: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(split_map.items())).encode()).hexdigest()


def freeze(qset: QuestionSet, data_dir: Path) -> Path:
    path = _freeze_path(qset, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "csv": qset.path.name,
                "csv_sha256": qset.sha256,
                "split_sha256": _split_sha(qset.split_map),
                "splits": qset.split_map,
                "frozen_at": datetime.now(UTC).isoformat(),
            },
            indent=1,
        )
    )
    return path


def check_freeze(qset: QuestionSet, data_dir: Path) -> FreezeStatus:
    path = _freeze_path(qset, data_dir)
    if not path.exists():
        return FreezeStatus(
            "not_frozen", [f"question set {qset.path.name} is not frozen (pkos eval --freeze)"]
        )
    frozen = json.loads(path.read_text())
    if frozen["csv_sha256"] == qset.sha256:
        return FreezeStatus("ok")
    msgs = [f"question set changed since it was frozen at {frozen['frozen_at'][:19]}"]
    old, new = frozen["splits"], qset.split_map
    moved = sorted(q for q in old.keys() & new.keys() if old[q] != new[q])
    if moved:
        msgs.append(f"FREEZE VIOLATION: split changed for {', '.join(moved)}")
    added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    if added:
        msgs.append(f"added since freeze: {', '.join(added)}")
    if removed:
        msgs.append(f"removed since freeze: {', '.join(removed)}")
    return FreezeStatus("changed", msgs)


# ---------------------------------------------------------------- test ledger (F1)


def _ledger_path(qset: QuestionSet, data_dir: Path) -> Path:
    return _state_dir(data_dir) / f"{qset.path.stem}.test_ledger.jsonl"


def _record_test_look(qset: QuestionSet, data_dir: Path, run: ConfigRun, at: str) -> str | None:
    path = _ledger_path(qset, data_dir)
    prior = []
    if path.exists():
        prior = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    mine = [e for e in prior if e["config"] == run.config.name]
    run.looks = len(mine) + 1
    warning = None
    if any(e["sha256"] != run.config.sha256 for e in mine):
        run.marked = True
        warning = (
            f"† {run.config.name}: config changed after a previous test-set result was "
            f"seen ({run.looks} test looks). Tune on dev, not test."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "config": run.config.name,
                    "sha256": run.config.sha256,
                    "at": at,
                    "csv_sha256": qset.sha256,
                }
            )
            + "\n"
        )
    return warning


# ---------------------------------------------------------------- provenance of a run


def corpus_snapshot(conn: psycopg.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT source, count(*), max(occurred_at) FROM live_episodes GROUP BY source ORDER BY 1"
    ).fetchall()
    return {
        src: {"live_episodes": n, "latest": latest.isoformat() if latest else None}
        for src, n, latest in rows
    }


def git_commit(repo: Path = REPO_ROOT) -> str:
    """Read HEAD without a git binary (the container has none)."""
    try:
        head = (repo / ".git" / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[5:]
        ref_file = repo / ".git" / ref
        if ref_file.exists():
            return ref_file.read_text().strip()[:12]
        for line in (repo / ".git" / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:12]
    except OSError:
        pass
    return "unknown"


# ---------------------------------------------------------------- run


def run_eval(
    conn: psycopg.Connection,
    qset: QuestionSet,
    configs: list[SystemConfig],
    split: str,
    judge: Judge,
    data_dir: Path,
    *,
    embedder=None,
    llm_factory=None,
) -> EvalRun:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    questions = [q for q in qset.questions if q.split == split]
    resolutions = resolve(conn, questions)
    run = EvalRun(
        qset=qset,
        split=split,
        questions=questions,
        resolutions=resolutions,
        runs=[],
        freeze=check_freeze(qset, data_dir),
        snapshot=corpus_snapshot(conn),
        git_commit=git_commit(),
        started_at=started,
    )
    for config in configs:
        system = systems.build(config, embedder=embedder, llm_factory=llm_factory)
        cr = ConfigRun(config)
        if split == "test" and (w := _record_test_look(qset, data_dir, cr, started)):
            run.warnings.append(w)
        for q in questions:
            res = resolutions[q.id]
            if not res.scored:
                continue
            ctx = systems.Context(conn, res.gold_episode_ids)
            t0 = time.perf_counter()
            try:
                ans = system.answer(q, ctx)
                # Latency is the system's own time: free-tier rate-limit waiting is excluded
                # (recorded separately), or every number would measure the account tier.
                latency = (time.perf_counter() - t0) * 1000 - ans.usage.queued_ms
                j = judge.judge(q, ans, res.gold_episode_ids)
            except Exception as e:  # recorded, excluded from the common set, never silent
                cr.results[q.id] = QResult(
                    None,
                    True,
                    False,
                    "error",
                    [],
                    [],
                    0,
                    0,
                    0,
                    0,
                    0.0,
                    error=f"{type(e).__name__}: {e}",
                )
                continue
            cr.results[q.id] = QResult(
                answer=ans.text,
                abstained=ans.abstained,
                correct=j.correct,
                method=j.method,
                retrieved_episode_ids=[r.episode_id for r in ans.retrieved],
                cited_episode_ids=ans.cited_episode_ids,
                latency_ms=latency,
                model_calls=ans.usage.model_calls,
                input_tokens=ans.usage.input_tokens,
                output_tokens=ans.usage.output_tokens,
                cost_usd=ans.usage.cost_usd,
            )
        run.runs.append(cr)
    return run


def write_results(run: EvalRun, data_dir: Path, tables: dict[str, str]) -> Path:
    stamp = run.started_at.replace(":", "").replace("-", "")
    base = _state_dir(data_dir) / "runs" / f"{stamp}-{run.split}"
    out, n = base, 1
    while out.exists():  # two runs in the same second must not overwrite each other
        n += 1
        out = base.with_name(f"{base.name}-{n}")
    out.mkdir(parents=True)
    doc = {
        "started_at": run.started_at,
        "split": run.split,
        "question_set": {
            "file": run.qset.path.name,
            "sha256": run.qset.sha256,
            "freeze": run.freeze.state,
        },
        "git_commit": run.git_commit,
        "corpus": run.snapshot,
        "warnings": run.warnings + run.freeze.messages,
        "configs": [
            systems.describe(r.config) | {"marked": r.marked, "test_looks": r.looks}
            for r in run.runs
        ],
        "skipped": {
            qid: {"reason": r.skip_reason, "detail": r.detail}
            for qid, r in run.resolutions.items()
            if not r.scored
        },
        "common_question_ids": run.common_ids,
        "results": {
            r.config.name: {qid: asdict(qr) for qid, qr in r.results.items()} for r in run.runs
        },
    }
    (out / "results.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    for name, text in tables.items():
        (out / name).write_text(text)
    run.out_dir = out
    return out
