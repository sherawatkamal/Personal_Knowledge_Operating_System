"""In-process usage accounting, printed after each command. Persisted to model_calls in step 5."""

import threading
from collections import defaultdict
from dataclasses import dataclass

from pkos.llm.base import Completion


@dataclass
class Tally:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0


class Usage:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_key: dict[tuple[str, str], Tally] = defaultdict(Tally)

    def record(self, purpose: str, c: Completion) -> None:
        with self._lock:
            t = self.by_key[(purpose, c.profile)]
            t.calls += 1
            t.input_tokens += c.input_tokens
            t.output_tokens += c.output_tokens
            t.reasoning_tokens += c.reasoning_tokens
            t.cost_usd += c.cost_usd

    def summary(self) -> str:
        if not self.by_key:
            return "model calls: 0"
        lines = []
        for (purpose, profile), t in sorted(self.by_key.items()):
            lines.append(
                f"{purpose:<8} {profile:<20} calls {t.calls:>4}  in {t.input_tokens:>7}  "
                f"out {t.output_tokens:>6} (reasoning {t.reasoning_tokens})  "
                f"list-price ${t.cost_usd:.4f}"
            )
        return "\n".join(lines)

    def reset(self) -> None:
        with self._lock:
            self.by_key.clear()


usage = Usage()
