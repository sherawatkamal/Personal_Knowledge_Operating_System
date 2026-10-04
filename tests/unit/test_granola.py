"""Granola client and mapping against a synthetic in-memory API."""

import httpx
import pytest

from pkos.connectors.granola import (
    SECTION_SUMMARY,
    SECTION_TRANSCRIPT,
    GranolaClient,
    GranolaConnector,
    GranolaError,
    render_transcript,
    to_episode,
)
from pkos.episodes.normalize import normalize
from tests.fixtures.granola import FakeGranola, note_detail

KEY = "grn_syntheticTestKey0123456789abcdef"


def client(fake: FakeGranola) -> GranolaClient:
    return GranolaClient(
        KEY, transport=httpx.MockTransport(fake.handler), min_interval=0, sleep=lambda s: None
    )


def test_transcript_merges_consecutive_turns():
    text = render_transcript(note_detail("not_1", "2026-03-02T16:00:00Z")["transcript"])
    assert text.splitlines() == [
        "Test Owner: Morning.",
        "Pat Example: Let's review the budget.",
        "Test Owner: I'll send the deck by Friday.",
    ]


def test_transcript_label_falls_back_to_attribution():
    seg = {"text": "hi", "speaker": {"attribution": "them"}}
    assert render_transcript([seg]) == "them: hi"


def test_mapping_sections_and_provenance():
    ep = normalize(to_episode(note_detail("not_1", "2026-03-02T16:00:00Z")))
    assert ep.source == "granola" and ep.external_id == "not_1"
    names = [s.name for s in ep.sections]
    assert names == [SECTION_TRANSCRIPT, SECTION_SUMMARY], "empty private notes are skipped"
    summary = ep.sections[1]
    assert summary.generated and not ep.sections[0].generated
    assert ep.body[summary.start : summary.end].startswith("Budget review.")
    assert ep.ends_at is not None and ep.ends_at > ep.occurred_at
    assert {p.role for p in ep.participants} == {"creator", "attendee"}
    assert ep.refs == [{"type": "web_url", "url": "https://notes.granola.ai/d/not_1"}]
    assert ep.raw["id"] == "not_1"


def test_private_notes_come_first_and_are_not_generated():
    ep = to_episode(note_detail("not_1", "t", private_notes_text="My own notes."))
    first = ep.sections[0]
    assert first.name == "my_notes" and not first.generated
    assert ep.body[first.start : first.end] == "My own notes."


def test_pagination_reads_every_page():
    fake = FakeGranola(page_size=2)
    for i in range(5):
        fake.add(f"not_{i}", "2026-03-02T16:00:00Z")
    assert [n["id"] for n in client(fake).list_notes()] == [f"not_{i}" for i in range(5)]
    assert sum(1 for r in fake.requests if r.startswith("/v1/notes?")) == 3


def test_413_falls_back_to_paged_transcript():
    fake = FakeGranola()
    fake.add("not_big", "2026-03-02T16:00:00Z")
    fake.too_large.add("not_big")
    note = client(fake).get_note("not_big")
    assert note["transcript"] == fake.notes["not_big"]["transcript"]
    assert any("/transcript" in r for r in fake.requests)


def test_retries_rate_limit_then_succeeds():
    fake = FakeGranola()
    fake.add("not_1", "t")
    fake.fail_next = [429, 503]
    assert [n["id"] for n in client(fake).list_notes()] == ["not_1"]


def test_gives_up_after_max_retries():
    fake = FakeGranola()
    fake.fail_next = [429] * 10
    c = GranolaClient(
        KEY,
        transport=httpx.MockTransport(fake.handler),
        min_interval=0,
        max_retries=2,
        sleep=lambda s: None,
    )
    with pytest.raises(GranolaError, match="gave up"):
        list(c.list_notes())


def test_error_messages_never_contain_the_key():
    def deny(request):
        return httpx.Response(401, json={"code": "UNAUTHORIZED"})

    c = GranolaClient(KEY, transport=httpx.MockTransport(deny), min_interval=0)
    with pytest.raises(GranolaError) as exc:
        list(c.list_notes())
    assert KEY not in str(exc.value) and "401" in str(exc.value)


def test_incremental_fetches_only_changed_notes():
    fake = FakeGranola()
    fake.add("not_old", "2026-03-01T10:00:00Z")
    fake.add("not_new", "2026-03-05T10:00:00Z")
    conn = GranolaConnector(client(fake))
    known = {"not_old": False, "not_new": False}
    changes = list(conn.changes({"max_updated_at": "2026-03-04T00:00:00+00:00"}, known))
    assert [c.external_id for c in changes] == ["not_new"]
    assert conn.new_watermark() == {"max_updated_at": "2026-03-05T10:00:00+00:00"}


def test_deleted_upstream_yields_tombstone_change():
    fake = FakeGranola()
    fake.add("not_kept", "2026-03-05T10:00:00Z")
    conn = GranolaConnector(client(fake))
    changes = list(
        conn.changes(
            {"max_updated_at": "2026-03-06T00:00:00+00:00"}, {"not_kept": False, "not_gone": False}
        )
    )
    assert [(c.external_id, c.episode) for c in changes] == [("not_gone", None)]


def test_tombstoned_note_that_reappears_is_refetched():
    fake = FakeGranola()
    fake.add("not_back", "2026-03-01T10:00:00Z")
    conn = GranolaConnector(client(fake))
    changes = list(
        conn.changes({"max_updated_at": "2026-03-06T00:00:00+00:00"}, {"not_back": True})
    )
    assert [c.external_id for c in changes] == ["not_back"] and changes[0].episode is not None
