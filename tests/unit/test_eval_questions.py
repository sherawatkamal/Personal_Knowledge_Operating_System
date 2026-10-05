from pathlib import Path

import pytest

from pkos.eval.questions import QuestionSetError, load

SYNTH = Path(__file__).parents[1] / "fixtures" / "eval" / "synthetic_questions.csv"
HEAD = "id,question,class,answerable,gold_answer,gold_sources,judge,sources_needed,split,origin\n"


def write(tmp_path, *rows) -> Path:
    p = tmp_path / "q.csv"
    p.write_text(HEAD + "\n".join(rows) + "\n")
    return p


def test_synthetic_set_loads():
    qs = load(SYNTH)
    assert len(qs.questions) == 14
    assert {q.split for q in qs.questions} == {"dev", "test"}
    assert len(qs.sha256) == 64
    q7 = next(q for q in qs.questions if q.id == "q07")
    assert [str(g) for g in q7.gold_sources] == ["granola:syn_g1", "granola:syn_g2"]


def test_hash_changes_with_content(tmp_path):
    a = load(write(tmp_path, "q1,Q?,lookup,false,,,model,,test,user"))
    b = load(write(tmp_path, "q1,Q2?,lookup,false,,,model,,test,user"))
    assert a.sha256 != b.sha256


@pytest.mark.parametrize(
    "row, problem",
    [
        ("q1,Q?,lookups,false,,,model,,test,user", "class"),
        ("q1,Q?,lookup,maybe,,,model,,test,user", "answerable"),
        ("q1,Q?,lookup,true,,gmail:m1,exact,gmail,test,user", "need gold_answer"),
        ("q1,Q?,lookup,false,X,,exact,,test,user", "must have empty"),
        ("q1,Q?,lookup,true,X,dropbox:1,exact,gmail,test,user", "known source"),
        ("q1,Q?,lookup,true,X,gmail:m1,exact,granola,test,user", "sources_needed"),
        ("q1,Q?,lookup,true,X,gmail:m1,exact,gmail,train,user", "split"),
        ("q1,Q?,lookup,true,X,gmail:m1,exact,gmail,test,near_miss", "near_miss"),
        ("q1,,lookup,false,,,model,,test,user", "empty question"),
    ],
)
def test_each_rule_is_enforced(tmp_path, row, problem):
    with pytest.raises(QuestionSetError, match=problem):
        load(write(tmp_path, row))


def test_all_problems_reported_at_once(tmp_path):
    p = write(
        tmp_path, "q1,Q?,bad,false,,,model,,test,user", "q1,Q?,lookup,false,,,nope,,test,user"
    )
    with pytest.raises(QuestionSetError) as e:
        load(p)
    msg = str(e.value)
    assert "2 problem(s)" in msg or "3 problem(s)" in msg
    assert "line 2" in msg and "line 3" in msg and "duplicate id" in msg


def test_missing_column_rejected(tmp_path):
    p = tmp_path / "q.csv"
    p.write_text("id,question\nq1,Q?\n")
    with pytest.raises(QuestionSetError, match="missing columns"):
        load(p)
