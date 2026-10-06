"""Ollama, running natively on the host (D8), reached from the container."""

import json
import time
from typing import Any

import httpx

from pkos.llm.base import Completion, LLMError, Message, Profile
from pkos.llm.usage import usage


class OllamaLLM:
    def __init__(
        self, profile: Profile, base_url: str, *, transport: httpx.BaseTransport | None = None
    ):
        self.profile = profile
        self.base_url = base_url
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=900)

    def complete(
        self, messages: list[Message], *, purpose: str, schema: dict[str, Any] | None = None
    ) -> Completion:
        p = self.profile
        body: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": p.temperature,
                "num_predict": p.max_output_tokens,
                "num_ctx": 16384,
            },
        }
        if p.reasoning_effort:
            body["think"] = p.reasoning_effort  # gpt-oss accepts low | medium | high
        if schema is not None:
            body["format"] = schema
        t0 = time.perf_counter()
        try:
            resp = self._http.post("/api/chat", json=body)
        except httpx.TransportError:
            raise LLMError(
                f"{p.name}: Ollama not reachable at {self.base_url}; is it running on the host?"
            ) from None
        latency = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            raise LLMError(f"{p.name}: Ollama HTTP {resp.status_code}")
        doc = resp.json()
        text = (doc.get("message") or {}).get("content") or ""
        data = None
        if schema is not None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise LLMError(f"{p.name}: structured output was not valid JSON") from None
        tin, tout = int(doc.get("prompt_eval_count", 0)), int(doc.get("eval_count", 0))
        c = Completion(text, data, tin, tout, 0, 0.0, latency, p.name, p.model)
        usage.record(purpose, c)
        return c
