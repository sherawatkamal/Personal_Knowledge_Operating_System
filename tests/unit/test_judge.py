import pytest

from pkos.eval.questions import GoldSource, Question
from pkos.eval.scoring import Judge, JudgeCache, JudgeUnavailable, normalize_answer, percentile
from pkos.eval.systems import Answer


def q(answerable=True, judge="model", gold="The revised deck by Friday"):
    return Question(
        "q1",
        "What did I agree to send?",
        "obligational",
        answerable,
        gold if answerable else "",
        (GoldSource("granola", "g1"),) if answerable else (),
        judge,
        ("granola",),
        "test",
        "drafted",
    )


def test_unanswerable_correct_only_when_abstaining():
    j = Judge()
    assert j.judge(q(False), Answer(None), ()).correct
    assert not j.judge(q(False), Answer("Something"), ()).correct


def test_abstaining_on_answerable_is_wrong_without_a_model_call():
    j = Judge()
    assert not j.judge(q(), Answer(None), (1,)).correct
    assert j.model_calls == 0


def test_normalised_exact_match_short_circuits():
    j = Judge()
    r = j.judge(q(), Answer("the revised deck, by Friday!"), (1,))
    assert r.correct and r.method == "exact" and j.model_calls == 0


def test_exact_judge_never_calls_model():
    assert not Judge().judge(q(judge="exact"), Answer("the slides"), (1,)).correct


def test_model_judge_unavailable_until_step_4():
    with pytest.raises(JudgeUnavailable, match="step 4"):
        Judge().judge(q(), Answer("the slides by Friday"), (1,))


def test_cache_makes_rescoring_free(tmp_path):
    calls = []

    def fake(question, answer, gold_ids):
        calls.append(answer)
        return True

    path = tmp_path / "judge.jsonl"
    j1 = Judge(model_judge=fake, model_id="m", prompt_sha="p", cache=JudgeCache(path))
    assert j1.judge(q(), Answer("the slides by Friday"), (1,)).correct
    j2 = Judge(model_judge=fake, model_id="m", prompt_sha="p", cache=JudgeCache(path))
    assert j2.judge(q(), Answer("the slides by Friday"), (1,)).correct
    assert len(calls) == 1 and j2.model_calls == 0

    j3 = Judge(model_judge=fake, model_id="m", prompt_sha="p2", cache=JudgeCache(path))
    j3.judge(q(), Answer("the slides by Friday"), (1,))
    assert len(calls) == 2, "a judge prompt change must miss the cache"


def test_normalize_answer():
    assert normalize_answer("  “40,000”  Dollars. ") == "40 000 dollars"


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([5, 1, 3], 50) == 3
    assert percentile(list(range(1, 101)), 95) == 95
