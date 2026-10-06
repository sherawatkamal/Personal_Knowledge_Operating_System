import random

from pkos.embed.chunker import MAX_CHARS, chunk_body
from pkos.episodes.normalize import build_body


def check_exact(body, sections, chunks):
    secs = {s["name"]: (s["start"], s["end"]) for s in sections}
    for c in chunks:
        assert 0 <= c.start < c.end <= len(body)
        assert c.end - c.start <= MAX_CHARS
        if c.section:
            s0, s1 = secs[c.section]
            assert s0 <= c.start and c.end <= s1, "chunks never cross a section boundary"
    covered = "".join(body[c.start : c.end] for c in chunks)
    assert "".join(body.split()) == "".join(covered.split()), "no text lost"


def sections_of(parts):
    body, secs = build_body(parts)
    return body, [{"name": s.name, "start": s.start, "end": s.end} for s in secs]


def test_short_sections_stay_whole_and_separate():
    body, secs = sections_of(
        [("transcript", "A: hi\nB: hello", False), ("granola_summary", "Summary.", True)]
    )
    chunks = chunk_body(body, secs)
    assert [(body[c.start : c.end], c.section) for c in chunks] == [
        ("A: hi\nB: hello", "transcript"),
        ("Summary.", "granola_summary"),
    ]


def test_long_text_splits_on_lines_and_preserves_offsets():
    rnd = random.Random(7)
    words = ["alpha", "beta", "gamma", "delta", "budget", "deck", "Friday"]
    lines = [
        " ".join(rnd.choice(words) for _ in range(rnd.randint(3, 60))) + "." for _ in range(200)
    ]
    body, secs = sections_of([("transcript", "\n".join(lines), False)])
    chunks = chunk_body(body, secs)
    assert len(chunks) > 5
    check_exact(body, secs, chunks)


def test_single_huge_line_is_split():
    body, secs = sections_of([("t", "word " * 2000, False)])
    chunks = chunk_body(body, secs)
    check_exact(body, secs, chunks)
    assert len(chunks) >= 6


def test_body_without_sections():
    chunks = chunk_body("one\ntwo", [])
    assert [(c.start, c.end, c.section) for c in chunks] == [(0, 7, None)]
