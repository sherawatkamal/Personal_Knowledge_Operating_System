"""LLM layer against mocked providers: limits, retries, structured output, accounting."""

import json

import httpx
import pytest

from pkos.llm import DailyLimitReached, LLMError, Profile, RequestTooLarge, load_profiles, usage
from pkos.llm.groq import GroqLLM
from pkos.llm.ollama import OllamaLLM
from pkos.llm.ratelimit import TokenBucket, parse_duration

KEY = "gsk_syntheticTestKey0123456789abcdefgh"
PROFILE = Profile(
    "t",
    "groq",
    "openai/gpt-oss-120b",
    max_output_tokens=500,
    reasoning_effort="low",
    tpm_limit=8000,
    price_in_per_m=0.15,
    price_out_per_m=0.60,
)
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def ok_response(content='{"ok": true}', **headers):
    return httpx.Response(
        200,
        headers={"x-ratelimit-remaining-tokens": "7000", **headers},
        json={
            "model": "openai/gpt-oss-120b",
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        },
    )


def groq(handler, sleeps=None):
    return GroqLLM(
        PROFILE,
        KEY,
        transport=httpx.MockTransport(handler),
        bucket=None,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


MSGS = [{"role": "user", "content": "Synthetic question?"}]


@pytest.fixture(autouse=True)
def clean_usage():
    usage.reset()
    yield
    usage.reset()


def test_shipped_profiles_load():
    p = load_profiles()
    assert p["groq-120b-answer"].model == "openai/gpt-oss-120b"
    assert p["groq-qwen-judge"].model != p["groq-120b-answer"].model
    assert p["ollama-20b-answer"].provider == "ollama"


def test_request_carries_strict_schema_and_reasoning_effort():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        assert req.headers["Authorization"] == f"Bearer {KEY}"
        return ok_response()

    c = groq(handler).complete(MSGS, purpose="answer", schema=SCHEMA)
    assert seen["response_format"]["json_schema"]["strict"] is True
    assert seen["reasoning_effort"] == "low" and seen["max_completion_tokens"] == 500
    assert c.data == {"ok": True}
    assert c.input_tokens == 100 and c.reasoning_tokens == 5
    assert c.cost_usd == pytest.approx((100 * 0.15 + 20 * 0.60) / 1e6)


def test_usage_is_accounted():
    groq(lambda r: ok_response()).complete(MSGS, purpose="answer")
    t = usage.by_key[("answer", "t")]
    assert t.calls == 1 and t.input_tokens == 100 and t.output_tokens == 20


def test_oversized_request_is_refused_before_sending():
    def handler(req):
        raise AssertionError("must not be sent")

    big = [{"role": "user", "content": "x" * 30000}]
    with pytest.raises(RequestTooLarge):
        groq(handler).complete(big, purpose="answer")


def test_short_429_waits_and_retries():
    calls, sleeps = [], []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, json={})
        return ok_response()

    groq(handler, sleeps).complete(MSGS, purpose="answer")
    assert len(calls) == 2 and sleeps == [3.0]


def test_long_429_stops_cleanly_as_daily_limit():
    def handler(req):
        return httpx.Response(429, headers={"retry-after": "3600"}, json={})

    with pytest.raises(DailyLimitReached, match="resume"):
        groq(handler).complete(MSGS, purpose="answer")


def test_errors_carry_code_not_content_or_key():
    def handler(req):
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": "bad",
                    "message": "echo: Synthetic",
                }
            },
        )

    with pytest.raises(LLMError) as e:
        groq(handler).complete(MSGS, purpose="answer")
    assert "invalid_request_error bad" in str(e.value)
    assert "Synthetic" not in str(e.value) and KEY not in str(e.value)


def test_invalid_json_under_schema_is_an_error():
    with pytest.raises(LLMError, match="not valid JSON"):
        groq(lambda r: ok_response(content="not json")).complete(MSGS, purpose="x", schema=SCHEMA)


@pytest.mark.parametrize(
    "value, seconds",
    [("3.179s", 3.179), ("1m26.4s", 86.4), ("7.66ms", 0.00766), ("2h1m3s", 7263.0), ("4", 4.0)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == pytest.approx(seconds)


def test_bucket_paces_to_the_per_minute_rate():
    now = [0.0]
    b = TokenBucket(6000, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert b.acquire(6000) == 0
    waited = b.acquire(3000)  # 6000/min = 100/s: needs 30 s
    assert waited == pytest.approx(30.0)
    with pytest.raises(ValueError):
        b.acquire(7000)


def test_bucket_sync_trusts_provider_remaining():
    now = [0.0]
    b = TokenBucket(8000, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    b.sync(remaining=1000, reset_seconds=50)
    assert b.acquire(1000) == 0
    assert b.acquire(800) == pytest.approx(800 / (8000 / 60))


def test_ollama_structured_and_unreachable():
    prof = Profile("o", "ollama", "gpt-oss:20b", max_output_tokens=200, reasoning_effort="low")
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"ok": false}'},
                "prompt_eval_count": 50,
                "eval_count": 7,
            },
        )

    c = OllamaLLM(prof, "http://host", transport=httpx.MockTransport(handler)).complete(
        MSGS, purpose="answer", schema=SCHEMA
    )
    assert seen["format"] == SCHEMA and seen["think"] == "low" and seen["stream"] is False
    assert c.data == {"ok": False} and c.cost_usd == 0.0

    def down(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="not reachable"):
        OllamaLLM(prof, "http://host", transport=httpx.MockTransport(down)).complete(
            MSGS, purpose="answer"
        )
