"""Groq (OpenAI-compatible chat completions) over plain HTTP. No SDK."""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from pkos.llm.base import (
    Completion,
    DailyLimitReached,
    LLMError,
    Message,
    Profile,
    RequestTooLarge,
    estimate_tokens,
)
from pkos.llm.ratelimit import TokenBucket, bucket_for, parse_duration
from pkos.llm.usage import usage

BASE_URL = "https://api.groq.com/openai/v1"
MAX_SHORT_WAIT = 120.0  # longer waits mean a daily limit: stop the run, don't hang


class GroqLLM:
    def __init__(
        self,
        profile: Profile,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 6,
    ):
        self.profile = profile
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=180,
        )
        self._bucket = bucket or (
            bucket_for(f"groq:{profile.model}", profile.tpm_limit) if profile.tpm_limit else None
        )
        self._sleep = sleep
        self._max_retries = max_retries

    def _payload(self, messages: list[Message], schema: dict[str, Any] | None) -> dict[str, Any]:
        p = self.profile
        body: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "temperature": p.temperature,
            "max_completion_tokens": p.max_output_tokens,
        }
        if p.reasoning_effort:
            body["reasoning_effort"] = p.reasoning_effort
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": schema},
            }
        return body

    def complete(
        self, messages: list[Message], *, purpose: str, schema: dict[str, Any] | None = None
    ) -> Completion:
        p = self.profile
        need = estimate_tokens(messages, p.max_output_tokens)
        if p.tpm_limit and need > int(p.tpm_limit * 0.95):
            raise RequestTooLarge(
                f"{p.name}: ~{need} tokens estimated, limit {p.tpm_limit}/min; shrink the prompt"
            )
        body = self._payload(messages, schema)
        for attempt in range(self._max_retries + 1):
            if self._bucket:
                self._bucket.acquire(need)
            t0 = time.perf_counter()
            try:
                resp = self._http.post("/chat/completions", json=body)
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise LLMError(f"{p.name}: {type(e).__name__}") from None
                self._sleep(2**attempt)
                continue
            latency = (time.perf_counter() - t0) * 1000
            if self._bucket:
                remaining = resp.headers.get("x-ratelimit-remaining-tokens")
                self._bucket.sync(
                    int(remaining) if remaining and remaining.isdigit() else None,
                    parse_duration(resp.headers.get("x-ratelimit-reset-tokens")),
                )
            if resp.status_code == 200:
                return self._completion(resp.json(), schema, latency, purpose)
            if resp.status_code == 429:
                wait = (
                    parse_duration(resp.headers.get("retry-after"))
                    or parse_duration(resp.headers.get("x-ratelimit-reset-tokens"))
                    or 2.0**attempt
                )
                if wait > MAX_SHORT_WAIT:
                    raise DailyLimitReached(
                        f"{p.name}: provider limit, retry in {wait / 60:.0f} min; "
                        "progress so far is kept, re-run later to resume"
                    )
                if attempt < self._max_retries:
                    self._sleep(wait)
                    continue
            elif resp.status_code >= 500 and attempt < self._max_retries:
                self._sleep(2**attempt)
                continue
            raise LLMError(f"{p.name}: HTTP {resp.status_code} {_error_code(resp)}")
        raise LLMError(f"{p.name}: gave up after {self._max_retries} retries")

    def _completion(
        self, doc: dict[str, Any], schema: dict[str, Any] | None, latency: float, purpose: str
    ) -> Completion:
        p = self.profile
        text = doc["choices"][0]["message"].get("content") or ""
        data = None
        if schema is not None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise LLMError(f"{p.name}: structured output was not valid JSON") from None
        u = doc.get("usage") or {}
        tin, tout = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        reasoning = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        c = Completion(
            text,
            data,
            tin,
            tout,
            reasoning,
            p.cost(tin, tout),
            latency,
            p.name,
            doc.get("model", p.model),
        )
        usage.record(purpose, c)
        return c


def _error_code(resp: httpx.Response) -> str:
    """Error type/code only: provider messages can echo request content."""
    try:
        err = resp.json().get("error") or {}
        return f"{err.get('type', '')} {err.get('code', '')}".strip()
    except ValueError:
        return ""
