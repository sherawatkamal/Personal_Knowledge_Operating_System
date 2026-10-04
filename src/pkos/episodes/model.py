"""The one shape every connector normalises into. Everything downstream reads only this."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Participant:
    role: str  # from|to|cc|bcc|organizer|attendee|creator|author
    name: str | None = None
    email: str | None = None
    handle: str | None = None
    response: str | None = None  # calendar RSVP


@dataclass(frozen=True)
class Section:
    """A named slice of `body`. `generated` marks text written by a model, not a person (D13)."""

    name: str
    start: int
    end: int
    generated: bool = False


@dataclass
class Episode:
    source: str
    external_id: str
    occurred_at: datetime
    body: str = ""
    sections: list[Section] = field(default_factory=list)
    title: str | None = None
    thread_id: str | None = None
    ends_at: datetime | None = None
    participants: list[Participant] = field(default_factory=list)
    refs: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)  # stable fields only: it is hashed
    raw: dict[str, Any] = field(default_factory=dict)  # payload as received: never hashed
