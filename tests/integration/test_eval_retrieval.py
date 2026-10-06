"""A retrieval config end to end through the harness, with a fake LLM and the test embedder."""

from pathlib import Path

import pytest

from pkos import db, migrate
from pkos.config import REPO_ROOT
from pkos.embed.index import index_pending
from pkos.eval import configs, report, runner
from pkos.eval.questions import load
from pkos.eval.scoring import Judge
from tests.fixtures import eval_corpus
from tests.fixtures.embedder import HashEmbedder
from tests.fixtures.fake_llm import FakeLLM

SYNTH = Path(__file__).parents[1] / "fixtures" / "eval" / "synthetic_questions.csv"


@pytest.fixture
def conn(fresh_db):
    with db.connect(fresh_db, autocommit=True) as c:
        migrate.migrate(c, REPO_ROOT / "migrations")
        eval_corpus.seed(c)
        index_pending(c, HashEmbedder())
        yield c


def test_hybrid_config_runs_through_the_harness(conn, tmp_path):
    def respond(messages):  # cite source 1 with its own text: plausible but not gold-exact
        return {"abstain": False, "claims": [{"text": "See the source.", "cites": [1]}]}

    llm = FakeLLM(respond)
    judged = []

    def fake_judge(q, answer, gold_ids):
        judged.append(q.id)
        return False

    chosen = configs.resolve(["hybrid", "null"])
    run = runner.run_eval(
        conn,
        load(SYNTH),
        chosen,
        "test",
        Judge(model_judge=fake_judge),
        tmp_path,
        embedder=HashEmbedder(),
        llm_factory=lambda name: llm,
    )
    hybrid = next(r for r in run.runs if r.config.name == "hybrid")
    assert set(hybrid.results) == set(run.common_ids)
    r1 = hybrid.results["q01"]
    assert r1.retrieved_episode_ids and r1.cited_episode_ids
    assert not r1.abstained
    m = report.metrics(run, hybrid)
    assert m.recall_at_k is not None and 0 < m.recall_at_k <= 1
    assert m.false_ans_unans == 1.0, "answering every unanswerable question is caught"
    assert judged, "non-exact answers on model-judged questions go to the model judge"
    assert len(llm.calls) == len(run.common_ids)
