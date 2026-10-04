"""Synthetic Granola API payloads matching the shape observed on 2026-10-04. No real data."""

from typing import Any

OWNER = {"name": "Test Owner", "email": "owner@example.com"}


def segment(text: str, who: str, start: str, end: str, name: str | None = None) -> dict:
    return {
        "text": text,
        "start_time": start,
        "end_time": end,
        "speaker": {
            "source": "microphone" if who == "me" else "speaker",
            "attribution": who,
            **({"name": name} if name else {}),
        },
    }


def note_summary(note_id: str, updated_at: str, title: str = "Synthetic meeting") -> dict:
    return {
        "id": note_id,
        "object": "note",
        "title": title,
        "owner": OWNER,
        "created_at": "2026-03-02T15:00:00.000Z",
        "updated_at": updated_at,
    }


def note_detail(note_id: str, updated_at: str, **overrides: Any) -> dict:
    detail = note_summary(note_id, updated_at) | {
        "web_url": f"https://notes.granola.ai/d/{note_id}",
        "calendar_event": None,
        "attendees": [OWNER],
        "folder_membership": [],
        "space_membership": [],
        "transcript": [
            segment(
                "Morning.",
                "me",
                "2026-03-02T15:00:05.000Z",
                "2026-03-02T15:00:06.000Z",
                "Test Owner",
            ),
            segment(
                "Let's review the",
                "them",
                "2026-03-02T15:00:07.000Z",
                "2026-03-02T15:00:08.000Z",
                "Pat Example",
            ),
            segment(
                "budget.",
                "them",
                "2026-03-02T15:00:08.000Z",
                "2026-03-02T15:00:09.000Z",
                "Pat Example",
            ),
            segment(
                "I'll send the deck by Friday.",
                "me",
                "2026-03-02T15:00:10.000Z",
                "2026-03-02T15:30:00.000Z",
                "Test Owner",
            ),
        ],
        "summary_text": "Budget review. Owner to send the deck by Friday.",
        "summary_markdown": "### Budget review\n- Owner to send the deck by Friday.",
        "private_notes_text": "",
        "private_notes_markdown": "",
    }
    return detail | overrides


class FakeGranola:
    """In-memory Granola API for httpx.MockTransport. Mutate `notes` between syncs."""

    def __init__(self, page_size: int = 30):
        self.notes: dict[str, dict] = {}
        self.page_size = page_size
        self.requests: list[str] = []
        self.too_large: set[str] = set()
        self.fail_next: list[int] = []  # status codes to return before succeeding

    def add(self, note_id: str, updated_at: str, **overrides: Any) -> None:
        self.notes[note_id] = note_detail(note_id, updated_at, **overrides)

    def handler(self, request):
        import httpx

        self.requests.append(f"{request.url.path}?{request.url.query.decode()}")
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={"code": "RATE_LIMITED"})
        assert request.headers["Authorization"].startswith("Bearer grn_")
        parts = request.url.path.split("/")[2:]  # after /v1
        if parts == ["notes"]:
            ids = sorted(self.notes)
            start = int(request.url.params.get("cursor") or 0)
            page = ids[start : start + self.page_size]
            more = start + self.page_size < len(ids)
            return httpx.Response(
                200,
                json={
                    "notes": [{k: self.notes[i][k] for k in note_summary("x", "y")} for i in page],
                    "hasMore": more,
                    "cursor": str(start + self.page_size) if more else None,
                },
            )
        note_id = parts[1]
        if note_id not in self.notes:
            return httpx.Response(404, json={"code": "NOT_FOUND"})
        note = self.notes[note_id]
        if len(parts) == 3 and parts[2] == "transcript":
            tr = note["transcript"]
            start = int(request.url.params.get("cursor") or 0)
            more = start + 2 < len(tr)
            return httpx.Response(
                200,
                json={
                    "transcript": tr[start : start + 2],
                    "hasMore": more,
                    "cursor": str(start + 2) if more else None,
                },
            )
        if "transcript" in request.url.params.get("include", ""):
            if note_id in self.too_large:
                return httpx.Response(413, json={"code": "TRANSCRIPT_TOO_LARGE"})
            return httpx.Response(200, json=note)
        return httpx.Response(200, json={k: v for k, v in note.items() if k != "transcript"})
