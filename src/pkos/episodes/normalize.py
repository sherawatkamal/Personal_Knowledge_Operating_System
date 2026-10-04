"""Normalisation and the canonical content hash (careful code: see tests/unit/test_normalize.py).

The hash covers exactly the fields that carry meaning: title, times, participants, refs,
stable meta, body and section layout. It never covers `raw`, so volatile payload fields
(read/unread, labels, etag) cannot trigger an update or a re-extraction.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, replace
from datetime import UTC, datetime

from pkos.episodes.model import Episode, Participant, Section

SECTION_SEPARATOR = "\n\n"
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


class InvalidEpisode(ValueError):
    pass


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_WS.sub("", text)
    text = _EXCESS_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def build_body(parts: list[tuple[str, str | None, bool]]) -> tuple[str, list[Section]]:
    """Join (name, text, generated) parts into one body with exact section offsets.

    Empty parts are skipped. The separator between sections belongs to no section.
    """
    body, sections = "", []
    for name, text, generated in parts:
        text = normalize_text(text)
        if not text:
            continue
        if body:
            body += SECTION_SEPARATOR
        sections.append(Section(name, len(body), len(body) + len(text), generated))
        body += text
    return body, sections


def _normalize_participant(p: Participant) -> Participant:
    def clean(v: str | None) -> str | None:
        v = unicodedata.normalize("NFC", v).strip() if v else None
        return v or None

    email = clean(p.email)
    return replace(
        p,
        name=clean(p.name),
        email=email.lower() if email else None,
        handle=clean(p.handle),
        response=clean(p.response),
    )


def _participant_key(p: Participant) -> tuple[str, str, str, str]:
    return (p.role, p.email or "", p.name or "", p.handle or "")


def _utc(dt: datetime | None, field: str) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise InvalidEpisode(f"{field} must be timezone-aware")
    return dt.astimezone(UTC)


def normalize(ep: Episode) -> Episode:
    """Return a normalised copy, validated. Body is NOT re-normalised here: connectors build it
    with build_body so that section offsets stay exact."""
    participants = sorted(
        {_normalize_participant(p) for p in ep.participants}, key=_participant_key
    )
    out = replace(
        ep,
        title=normalize_text(ep.title) or None,
        occurred_at=_utc(ep.occurred_at, "occurred_at"),
        ends_at=_utc(ep.ends_at, "ends_at"),
        participants=participants,
    )
    _validate(out)
    return out


def _validate(ep: Episode) -> None:
    if not ep.source or not ep.external_id:
        raise InvalidEpisode("source and external_id are required")
    if ep.ends_at is not None and ep.ends_at < ep.occurred_at:
        raise InvalidEpisode(f"{ep.source}:{ep.external_id}: ends_at before occurred_at")
    prev_end = 0
    for s in ep.sections:
        if not (prev_end <= s.start < s.end <= len(ep.body)):
            raise InvalidEpisode(f"{ep.source}:{ep.external_id}: section {s.name!r} out of bounds")
        prev_end = s.end


def _canonical(ep: Episode) -> str:
    doc = {
        "title": ep.title,
        "thread_id": ep.thread_id,
        "occurred_at": ep.occurred_at.isoformat(),
        "ends_at": ep.ends_at.isoformat() if ep.ends_at else None,
        "participants": [asdict(p) for p in ep.participants],
        "refs": ep.refs,
        "meta": ep.meta,
        "body": ep.body,
        "sections": [asdict(s) for s in ep.sections],
    }
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(ep: Episode) -> str:
    """sha256 of the canonical form. Call on a normalised episode."""
    return hashlib.sha256(_canonical(ep).encode("utf-8")).hexdigest()
