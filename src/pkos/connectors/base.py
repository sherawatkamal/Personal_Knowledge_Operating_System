"""The Connector interface (D2): the one extension point for new sources."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from pkos.episodes import Episode


@dataclass(frozen=True)
class Change:
    """One upstream change. `episode is None` means the item was deleted upstream."""

    external_id: str
    episode: Episode | None


class Connector(Protocol):
    source: str

    def changes(self, watermark: dict[str, Any], known: dict[str, bool]) -> Iterator[Change]:
        """Yield changes since `watermark`.

        `known` maps every stored external_id to whether it is tombstoned, so a connector
        without upstream delete events can detect deletions by diffing, and can refetch a
        tombstoned item that reappears.
        """
        ...

    def new_watermark(self) -> dict[str, Any]:
        """The watermark to store. Only valid after changes() was fully consumed."""
        ...
