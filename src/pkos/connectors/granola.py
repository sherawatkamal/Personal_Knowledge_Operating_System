"""Granola connector: public REST API at public-api.granola.ai/v1 (verified 2026-10-04).

Observed API facts this code relies on:
- GET /notes: cursor pagination (`notes`, `hasMore`, `cursor`), page_size <= 30, items carry
  id, title, owner, created_at, updated_at. No deleted-note events: deletion is detected by
  diffing the full listing against what we hold.
- GET /notes/{id}?include=transcript: summary_text (Granola's AI summary, model-generated),
  private_notes_text (the user's own notes, when owned), attendees, calendar_event,
  transcript segments {text, start_time, end_time, speaker{source, attribution, name}}.
  A 413 means the transcript is too large to embed; it is then paged from /transcript.
- 5 requests/second sustained, burst 25 per 5 seconds.
"""

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx

from pkos.connectors.base import Change
from pkos.episodes import Episode, Participant
from pkos.episodes.normalize import build_body

BASE_URL = "https://public-api.granola.ai/v1"
PAGE_SIZE = 30
# Re-fetch notes updated within this window of the watermark, so clock skew or a note
# updated mid-sync cannot be missed. Re-fetching is a no-op thanks to the content hash.
WATERMARK_OVERLAP = timedelta(minutes=5)

# Body layout (D13, pending sign-off): the user's own notes, then the transcript, then
# Granola's AI summary. The summary is marked generated so extraction can exclude it.
SECTION_MY_NOTES = "my_notes"
SECTION_TRANSCRIPT = "transcript"
SECTION_SUMMARY = "granola_summary"


class GranolaError(Exception):
    pass


class NotFound(GranolaError):
    pass


class TranscriptTooLarge(GranolaError):
    pass


class GranolaClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = 0.21,  # stays under 5 req/s
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            transport=transport,
            timeout=60,
        )
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._sleep = sleep
        self._last = 0.0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise GranolaError(f"GET {path}: {type(e).__name__}") from None
                self._sleep(2**attempt)
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                raise NotFound(f"GET {path}: 404")
            if resp.status_code == 413:
                raise TranscriptTooLarge(f"GET {path}: 413")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries:
                    break
                retry_after = resp.headers.get("Retry-After")
                self._sleep(float(retry_after) if retry_after else 2**attempt)
                continue
            # Error bodies carry a code and message, never our credentials; headers are not logged.
            code = _error_code(resp)
            raise GranolaError(f"GET {path}: {resp.status_code} {code}")
        raise GranolaError(f"GET {path}: gave up after {self._max_retries} retries")

    def list_notes(self) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            params: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            page = self._get("/notes", params)
            yield from page.get("notes", [])
            if not page.get("hasMore"):
                return
            cursor = page.get("cursor")
            if not cursor:
                raise GranolaError("hasMore without a cursor")

    def get_note(self, note_id: str) -> dict[str, Any]:
        try:
            return self._get(f"/notes/{note_id}", {"include": "transcript"})
        except TranscriptTooLarge:
            note = self._get(f"/notes/{note_id}")
            note["transcript"] = list(self._transcript_pages(note_id))
            return note

    def _transcript_pages(self, note_id: str) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self._get(f"/notes/{note_id}/transcript", {"cursor": cursor} if cursor else None)
            yield from page.get("transcript", [])
            if not page.get("hasMore"):
                return
            cursor = page.get("cursor")
            if not cursor:
                raise GranolaError("transcript hasMore without a cursor")


def _error_code(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("code", ""))
    except ValueError:
        return ""


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def render_transcript(segments: list[dict[str, Any]]) -> str:
    """One line per speaker turn: consecutive segments by the same speaker are merged.

    The label is the speaker name Granola reports, else "me"/"them" from attribution.
    Timestamps stay in `raw`; they would only add noise to search and extraction.
    """
    lines: list[str] = []
    last_label = None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = seg.get("speaker") or {}
        label = speaker.get("name") or speaker.get("attribution") or "unknown"
        if label == last_label:
            lines[-1] += " " + text
        else:
            lines.append(f"{label}: {text}")
            last_label = label
    return "\n".join(lines)


def to_episode(note: dict[str, Any]) -> Episode:
    transcript = note.get("transcript") or []
    body, sections = build_body(
        [
            (SECTION_MY_NOTES, note.get("private_notes_text"), False),
            (SECTION_TRANSCRIPT, render_transcript(transcript), False),
            (SECTION_SUMMARY, note.get("summary_text"), True),
        ]
    )
    created = _ts(note["created_at"])
    starts = [t for t in (_ts(s.get("start_time")) for s in transcript) if t]
    ends = [t for t in (_ts(s.get("end_time")) for s in transcript) if t]
    occurred = min([created, *starts])
    ends_at = max(ends) if ends and max(ends) > occurred else None

    participants = []
    if owner := note.get("owner"):
        participants.append(Participant("creator", owner.get("name"), owner.get("email")))
    for a in note.get("attendees") or []:
        participants.append(Participant("attendee", a.get("name"), a.get("email")))

    refs = []
    if url := note.get("web_url"):
        refs.append({"type": "web_url", "url": url})
    if (event := note.get("calendar_event")) and event.get("id"):
        refs.append({"type": "calendar_event", "id": event["id"]})

    meta = {
        "folder_ids": sorted(f["id"] for f in note.get("folder_membership") or [] if "id" in f),
        "space_ids": sorted(s["id"] for s in note.get("space_membership") or [] if "id" in s),
    }
    return Episode(
        source="granola",
        external_id=note["id"],
        occurred_at=occurred,
        ends_at=ends_at,
        title=note.get("title"),
        body=body,
        sections=sections,
        participants=participants,
        refs=refs,
        meta={k: v for k, v in meta.items() if v},
        raw=note,
    )


class GranolaConnector:
    source = "granola"

    def __init__(self, client: GranolaClient):
        self.client = client
        self._watermark: dict[str, Any] | None = None

    def changes(self, watermark: dict[str, Any], known: dict[str, bool]) -> Iterator[Change]:
        listed = {n["id"]: _ts(n["updated_at"]) for n in self.client.list_notes()}
        since = _ts(watermark.get("max_updated_at"))

        for ext_id, tombstoned in known.items():
            if not tombstoned and ext_id not in listed:
                yield Change(ext_id, None)

        for ext_id, updated in sorted(listed.items(), key=lambda kv: kv[1]):
            stale = since is None or updated >= since - WATERMARK_OVERLAP
            if ext_id in known and not known[ext_id] and not stale:
                continue
            try:
                yield Change(ext_id, to_episode(self.client.get_note(ext_id)))
            except NotFound:  # deleted between listing and fetch
                yield Change(ext_id, None)

        newest = max(listed.values(), default=since)
        self._watermark = {"max_updated_at": newest.isoformat()} if newest else {}

    def new_watermark(self) -> dict[str, Any]:
        if self._watermark is None:
            raise RuntimeError("new_watermark() before changes() was consumed")
        return self._watermark
