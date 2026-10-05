"""Comparison tables. Every number is computed over the common question set only."""

import csv
import io
from collections import Counter
from dataclasses import dataclass

from pkos.eval.questions import CLASSES, ORIGINS
from pkos.eval.runner import ConfigRun, EvalRun
from pkos.eval.scoring import SKIP_GOLD_NOT_FOUND, SKIP_REASONS, percentile, ratio


class NotPublishable(ValueError):
    pass


@dataclass
class Metrics:
    n: int
    by_class: dict[str, tuple[int, int]]  # class -> (correct, n)
    by_origin: dict[str, tuple[int, int]]
    overall: float | None
    k: int
    recall_at_k: float | None
    cite_prec: float | None
    false_abstain: float | None
    false_ans_unans: float | None
    tokens_per_q: float | None
    p50_ms: float | None
    p95_ms: float | None
    cost_per_q: float | None


def metrics(run: EvalRun, cr: ConfigRun) -> Metrics:
    ids = run.common_ids
    qs = {q.id: q for q in run.questions}
    k = int(cr.config.raw.get("retrieval", {}).get("k", 10))
    by_class: dict[str, list[int]] = {}
    by_origin: dict[str, list[int]] = {}
    hits = answerable = abstained_ans = unans = answered_unans = 0
    cited_total = cited_gold = 0
    latencies, tokens, cost = [], 0, 0.0
    for qid in ids:
        q, r = qs[qid], cr.results[qid]
        gold = set(run.resolutions[qid].gold_episode_ids)
        for bucket, key in ((by_class, q.qclass), (by_origin, q.origin)):
            c = bucket.setdefault(key, [0, 0])
            c[0] += r.correct
            c[1] += 1
        if q.answerable:
            answerable += 1
            hits += bool(gold & set(r.retrieved_episode_ids[:k]))
            abstained_ans += r.abstained
        else:
            unans += 1
            answered_unans += not r.abstained
        cited_total += len(r.cited_episode_ids)
        cited_gold += sum(1 for e in r.cited_episode_ids if e in gold)
        latencies.append(r.latency_ms)
        tokens += r.input_tokens + r.output_tokens
        cost += r.cost_usd
    n = len(ids)
    correct = sum(c for c, _ in by_class.values())
    return Metrics(
        n=n,
        by_class={c: tuple(v) for c, v in by_class.items()},
        by_origin={o: tuple(v) for o, v in by_origin.items()},
        overall=ratio(correct, n),
        k=k,
        recall_at_k=ratio(hits, answerable),
        cite_prec=ratio(cited_gold, cited_total),
        false_abstain=ratio(abstained_ans, answerable),
        false_ans_unans=ratio(answered_unans, unans),
        tokens_per_q=tokens / n if n else None,
        p50_ms=percentile(latencies, 50),
        p95_ms=percentile(latencies, 95),
        cost_per_q=cost / n if n else None,
    )


def _f(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def _label(cr: ConfigRun) -> str:
    label = cr.config.name + ("†" if cr.marked else "")
    return label + (" (diagnostic)" if cr.config.diagnostic else "")


def _class_columns(run: EvalRun) -> list[str]:
    present = {q.qclass for q in run.questions if q.id in set(run.common_ids)}
    return [c for c in CLASSES if c in present]


def header(run: EvalRun) -> list[str]:
    sha = run.qset.sha256
    freeze = {"ok": "frozen ✓", "not_frozen": "NOT FROZEN", "changed": "CHANGED SINCE FREEZE ✗"}
    skipped = Counter(r.skip_reason for r in run.resolutions.values() if not r.scored)
    parts = []
    for reason in SKIP_REASONS:
        if skipped[reason]:
            part = f"{reason} {skipped[reason]}"
            if reason == SKIP_GOLD_NOT_FOUND:  # usually a typo in the CSV: name them
                ids = sorted(q for q, r in run.resolutions.items() if r.skip_reason == reason)
                part += f" [{','.join(ids)}]"
            parts.append(part)
    errors = len(run.scored_ids) - len(run.common_ids)
    lines = [
        f"Question set {run.qset.path.name} {sha[:4]}…{sha[-3:]} ({freeze[run.freeze.state]})"
        f"   split={run.split}   scored {len(run.common_ids)} / {len(run.questions)}"
        + (f"   skipped: {', '.join(parts)}" if parts else "")
        + (f"   errors: {errors}" if errors else ""),
        "Corpus: "
        + (
            " · ".join(
                f"{s} {v['live_episodes']} live (latest {v['latest'][:10]})"
                for s, v in run.snapshot.items()
            )
            or "empty"
        )
        + f"   commit {run.git_commit}   {run.started_at}",
    ]
    lines += [f"WARNING: {m}" for m in run.freeze.messages + run.warnings]
    return lines


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(
            cell.ljust(w) if i == 0 else cell.rjust(w)
            for i, (cell, w) in enumerate(zip(r, widths, strict=True))
        ).rstrip()
        for r in rows
    )


def _rows(run: EvalRun) -> tuple[list[list[str]], list[list[str]], list[list[str]]]:
    classes = _class_columns(run)
    ms = [(cr, metrics(run, cr)) for cr in run.runs]
    n_by_class = Counter(q.qclass for q in run.questions if q.id in set(run.common_ids))
    acc = [
        ["ACCURACY", *classes, "overall"],
        ["", *[f"n={n_by_class[c]}" for c in classes], f"n={len(run.common_ids)}"],
    ]
    for cr, m in ms:
        acc.append([_label(cr), *[_f(ratio(*m.by_class[c])) for c in classes], _f(m.overall)])
    ret = [
        [
            "RETRIEVAL & COST",
            "k",
            "recall@k",
            "cite_prec",
            "false_abstain",
            "false_ans_unans",
            "tok/q",
            "p50ms",
            "p95ms",
            "$/q",
        ]
    ]
    for cr, m in ms:
        ret.append(
            [
                _label(cr),
                str(m.k),
                _f(m.recall_at_k),
                _f(m.cite_prec),
                _f(m.false_abstain),
                _f(m.false_ans_unans),
                _f(m.tokens_per_q, 0),
                _f(m.p50_ms, 1),
                _f(m.p95_ms, 1),
                _f(m.cost_per_q, 4),
            ]
        )
    origins = [o for o in ORIGINS if any(o in m.by_origin for _, m in ms)]
    orig = []
    if len(origins) > 1:
        n_by_origin = Counter(q.origin for q in run.questions if q.id in set(run.common_ids))
        orig = [["BY ORIGIN", *origins], ["", *[f"n={n_by_origin[o]}" for o in origins]]]
        for cr, m in ms:
            orig.append(
                [
                    _label(cr),
                    *[_f(ratio(*m.by_origin[o])) if o in m.by_origin else "—" for o in origins],
                ]
            )
    return acc, ret, orig


def render_text(run: EvalRun) -> str:
    acc, ret, orig = _rows(run)
    blocks = ["\n".join(header(run)), _table(acc), _table(ret)]
    if orig:
        blocks.append(_table(orig))
    return "\n\n".join(blocks) + "\n"


def render_csv(run: EvalRun) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for block in _rows(run):
        if block:
            w.writerows(block)
            w.writerow([])
    return buf.getvalue()


def render_markdown(run: EvalRun) -> str:
    """The publishable form (README). Refuses dev results; drops diagnostic configs."""
    if run.split != "test":
        raise NotPublishable("dev-split results are never published; run with --split test")
    published = [cr for cr in run.runs if not cr.config.diagnostic]
    if not published:
        raise NotPublishable("only diagnostic configs in this run; nothing publishable")
    view = EvalRun(**{**run.__dict__, "runs": published})
    acc, ret, orig = _rows(view)

    def md(rows: list[list[str]]) -> str:
        head, *body = rows
        return "\n".join(
            [
                "| " + " | ".join(head) + " |",
                "|" + "---|" * len(head),
                *("| " + " | ".join(r) + " |" for r in body),
            ]
        )

    lines = [f"_{h}_  " for h in header(view)]
    out = "\n".join(lines) + "\n\n" + md(acc) + "\n\n" + md(ret) + "\n"
    if orig:
        out += "\n" + md(orig) + "\n"
    if any(cr.marked for cr in published):
        out += "\n† config changed after an earlier test-set result was seen.\n"
    return out
