"""End to end: synthetic corpus + synthetic CSV through null and oracle configurations."""

import json
from pathlib import Path

import pytest

from pkos import db, migrate
from pkos.config import REPO_ROOT
from pkos.eval import configs, report, runner
from pkos.eval.questions import load
from pkos.eval.scoring import Judge
from tests.fixtures import eval_corpus

SYNTH = Path(__file__).parents[1] / "fixtures" / "eval" / "synthetic_questions.csv"


@pytest.fixture
def conn(fresh_db):
    with db.connect(fresh_db, autocommit=True) as c:
        migrate.migrate(c, REPO_ROOT / "migrations")
        eval_corpus.seed(c)
        yield c


def run(conn, tmp_path, names=("null", "oracle"), split="test", qpath=SYNTH, config_dir=None):
    chosen = configs.resolve(list(names), config_dir or configs.CONFIG_DIR)
    return runner.run_eval(conn, load(qpath), chosen, split, Judge(), tmp_path)


def by_name(r):
    return {cr.config.name: report.metrics(r, cr) for cr in r.runs}


def test_skip_reasons_and_common_set(conn, tmp_path):
    r = run(conn, tmp_path)
    skipped = {q: res.skip_reason for q, res in r.resolutions.items() if not res.scored}
    assert skipped == {
        "q11": "source_not_connected",
        "q12": "gold_not_found",
        "q13": "gold_tombstoned",
    }
    assert r.common_ids == ["q01", "q02", "q03", "q04", "q07", "q08", "q10"]
    assert all(q.split == "test" for q in r.questions), "dev questions never enter a test run"


def test_oracle_scores_perfectly_and_null_is_the_floor(conn, tmp_path):
    m = by_name(run(conn, tmp_path))
    o, n = m["oracle"], m["null"]
    assert o.overall == 1.0 and o.recall_at_k == 1.0 and o.cite_prec == 1.0
    assert o.false_abstain == 0.0 and o.false_ans_unans == 0.0
    assert n.overall == pytest.approx(2 / 7), "null is right only on the 2 unanswerables"
    assert n.false_abstain == 1.0 and n.false_ans_unans == 0.0
    assert n.recall_at_k == 0.0 and n.cite_prec is None


def test_text_table_shows_skips_and_n(conn, tmp_path):
    text = report.render_text(run(conn, tmp_path))
    assert "scored 7 / 10" in text
    assert "gold_not_found 1 [q12]" in text and "source_not_connected 1" in text
    assert "NOT FROZEN" in text
    assert "oracle (diagnostic)" in text and "n=7" in text


def test_freeze_detects_changes_and_split_moves(conn, tmp_path):
    qpath = tmp_path / "set.csv"
    qpath.write_text(SYNTH.read_text())
    runner.freeze(load(qpath), tmp_path)
    assert run(conn, tmp_path, qpath=qpath).freeze.state == "ok"
    qpath.write_text(
        SYNTH.read_text().replace(
            "May 14,gmail:syn_m2,exact,gmail,dev", "May 14,gmail:syn_m2,exact,gmail,test"
        )
    )
    status = run(conn, tmp_path, qpath=qpath).freeze
    assert status.state == "changed"
    assert any("FREEZE VIOLATION" in m and "q06" in m for m in status.messages)


def test_ledger_marks_config_changed_after_test_look(conn, tmp_path):
    cdir = tmp_path / "configs"
    cdir.mkdir()
    (cdir / "null.toml").write_text('name = "null"\n[system]\nkind = "null"\n')
    first = run(conn, tmp_path, names=("null",), config_dir=cdir)
    assert not first.runs[0].marked and first.runs[0].looks == 1
    again = run(conn, tmp_path, names=("null",), config_dir=cdir)
    assert not again.runs[0].marked, "re-running an unchanged config is not tuning"
    (cdir / "null.toml").write_text('name = "null"\n# threshold tweak\n[system]\nkind = "null"\n')
    changed = run(conn, tmp_path, names=("null",), config_dir=cdir)
    assert changed.runs[0].marked and changed.runs[0].looks == 3
    assert any("†" in w for w in changed.warnings)
    assert "null†" in report.render_text(changed)


def test_dev_runs_do_not_touch_the_ledger(conn, tmp_path):
    run(conn, tmp_path, split="dev")
    assert not list(tmp_path.glob("eval/*.test_ledger.jsonl"))


def test_markdown_refuses_dev_and_drops_diagnostic(conn, tmp_path):
    with pytest.raises(report.NotPublishable, match="dev"):
        report.render_markdown(run(conn, tmp_path, split="dev"))
    md = report.render_markdown(run(conn, tmp_path))
    assert "| null |" in md and "oracle" not in md
    with pytest.raises(report.NotPublishable, match="diagnostic"):
        report.render_markdown(run(conn, tmp_path, names=("oracle",)))


def test_results_written_with_provenance(conn, tmp_path):
    r = run(conn, tmp_path)
    out = runner.write_results(r, tmp_path, {"tables.txt": report.render_text(r)})
    doc = json.loads((out / "results.json").read_text())
    assert doc["question_set"]["sha256"] == load(SYNTH).sha256
    assert doc["corpus"]["granola"]["live_episodes"] == 2
    assert doc["corpus"]["gmail"]["live_episodes"] == 2, "tombstoned episode not counted live"
    assert doc["skipped"]["q12"]["reason"] == "gold_not_found"
    assert set(doc["results"]) == {"null", "oracle"}
    assert (out / "tables.txt").exists()
    again = runner.write_results(r, tmp_path, {})
    assert again != out and (out / "results.json").exists(), "same-second runs must not collide"


def test_system_error_is_recorded_and_excluded(conn, tmp_path, monkeypatch):
    from pkos.eval import systems

    class Flaky(systems.NullSystem):
        def answer(self, question, ctx):
            if question.id == "q03":
                raise RuntimeError("boom")
            return super().answer(question, ctx)

    monkeypatch.setattr(systems, "NullSystem", Flaky)
    r = run(conn, tmp_path)
    assert "q03" not in r.common_ids
    assert r.runs[0].results["q03"].error == "RuntimeError: boom"
    assert "errors: 1" in report.render_text(r)
