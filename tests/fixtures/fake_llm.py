"""A scripted LLM for tests: records what it was sent, returns what the test decides."""

from collections.abc import Callable

from pkos.llm import Completion, Profile


class FakeLLM:
    def __init__(
        self,
        respond: Callable[[list[dict]], dict],
        *,
        tpm_limit: int | None = 8000,
        max_output_tokens: int = 500,
        name: str = "fake",
    ):
        self.profile = Profile(
            name, "groq", "fake-model", max_output_tokens=max_output_tokens, tpm_limit=tpm_limit
        )
        self.respond = respond
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, purpose, schema=None):
        self.calls.append(messages)
        data = self.respond(messages)
        return Completion("", data, 100, 20, 0, 0.0, 1.0, self.profile.name, "fake-model")
