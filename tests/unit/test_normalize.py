"""Content hash: deterministic, insensitive to encoding noise, sensitive to meaning."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from pkos.episodes import Episode, Participant
from pkos.episodes.normalize import (
    InvalidEpisode,
    build_body,
    content_hash,
    normalize,
    normalize_text,
)

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


def ep(**kw) -> Episode:
    body, sections = build_body([("notes", kw.pop("text", "Agreed to send the deck."), False)])
    base = dict(
        source="granola",
        external_id="not_synthetic01",
        occurred_at=T0,
        title="Design review",
        body=body,
        sections=sections,
        participants=[Participant("creator", "Ada Lovelace", "ada@example.com")],
    )
    base.update(kw)
    return Episode(**base)


def h(e: Episode) -> str:
    return content_hash(normalize(e))


def test_hash_is_deterministic():
    assert h(ep()) == h(ep())
    assert len(h(ep())) == 64


def test_unicode_nfc_and_nfd_hash_the_same():
    nfc = "Café meeting"
    nfd = "Café meeting"
    assert nfc != nfd
    assert h(ep(text=nfc)) == h(ep(text=nfd))


def test_line_endings_and_trailing_whitespace_ignored():
    assert h(ep(text="line one\nline two")) == h(ep(text="line one  \r\nline two\r\n\r\n\r\n"))


def test_participant_order_and_email_case_ignored():
    a = Participant("to", "Bob", "Bob@Example.com")
    b = Participant("to", "Cy", "cy@example.com")
    assert h(ep(participants=[a, b])) == h(
        ep(participants=[b, replace(a, email="bob@example.com")])
    )


def test_timezone_representation_ignored():
    est = timezone(timedelta(hours=-5))
    assert h(ep(occurred_at=T0)) == h(ep(occurred_at=T0.astimezone(est)))


def test_raw_payload_never_affects_hash():
    """Volatile fields (labels, read state, etags) live in raw and must not trigger updates."""
    assert h(ep(raw={"labels": ["UNREAD"]})) == h(ep(raw={"labels": [], "etag": "x"}))


@pytest.mark.parametrize(
    "change",
    [
        dict(text="Agreed to send the deck by Friday."),
        dict(title="Design review v2"),
        dict(occurred_at=T0 + timedelta(minutes=30)),
        dict(participants=[Participant("creator", "Ada Lovelace", "ada@other.com")]),
        dict(meta={"folder_ids": ["fol_1"]}),
        dict(refs=[{"type": "web_url", "url": "https://example.com/x"}]),
    ],
)
def test_meaningful_changes_change_hash(change):
    assert h(ep(**change)) != h(ep())


def test_naive_datetime_rejected():
    with pytest.raises(InvalidEpisode, match="timezone"):
        normalize(ep(occurred_at=datetime(2026, 3, 2, 15, 0)))


def test_ends_before_start_rejected():
    with pytest.raises(InvalidEpisode, match="ends_at"):
        normalize(ep(ends_at=T0 - timedelta(minutes=1)))


def test_build_body_offsets_are_exact():
    body, sections = build_body(
        [("my_notes", "  mine\r\n", False), ("empty", "", False), ("summary", "theirs", True)]
    )
    assert body == "mine\n\ntheirs"
    assert [body[s.start : s.end] for s in sections] == ["mine", "theirs"]
    assert [s.name for s in sections] == ["my_notes", "summary"]
    assert sections[1].generated


def test_section_out_of_bounds_rejected():
    e = ep()
    bad = replace(e.sections[0], end=len(e.body) + 5)
    with pytest.raises(InvalidEpisode, match="out of bounds"):
        normalize(replace(e, sections=[bad]))


def test_normalize_text_collapses_blank_runs():
    assert normalize_text("a\n\n\n\nb") == "a\n\nb"
