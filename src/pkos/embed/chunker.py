"""Offset-preserving chunking (careful: every chunk is an exact slice of body).

Chunks never cross a section boundary, so a citation always lands in one section and a
generated section (e.g. Granola's summary) is never blended with source text.
"""

import re
from dataclasses import dataclass
from typing import Any

TARGET_CHARS = 1200  # ~300 tokens: small enough to pack many into an 8k-TPM answer prompt
MAX_CHARS = 1600
_BREAK = re.compile(r"(?<=[.!?])\s+|\s+")


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    section: str | None


def _split_long(body: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split one over-long line at sentence ends, else whitespace, else hard."""
    out, pos = [], start
    while end - pos > MAX_CHARS:
        window = body[pos : pos + MAX_CHARS]
        cut = None
        for m in _BREAK.finditer(window):
            if m.start() >= TARGET_CHARS // 2:
                cut = m.start()
                if m.start() >= TARGET_CHARS:
                    break
        cut = cut or MAX_CHARS
        out.append((pos, pos + cut))
        pos += cut
        while pos < end and body[pos].isspace():
            pos += 1
    if pos < end:
        out.append((pos, end))
    return out


def chunk_body(body: str, sections: list[dict[str, Any]]) -> list[Chunk]:
    spans = [(s["start"], s["end"], s["name"]) for s in sections] or [(0, len(body), None)]
    chunks: list[Chunk] = []
    for sec_start, sec_end, name in spans:
        pieces: list[tuple[int, int]] = []
        line_start = sec_start
        while line_start < sec_end:
            nl = body.find("\n", line_start, sec_end)
            line_end = sec_end if nl == -1 else nl
            if line_end > line_start:
                pieces.extend(_split_long(body, line_start, line_end))
            line_start = line_end + 1
        cur_start = cur_end = None
        for a, b in pieces:
            if cur_start is None:
                cur_start, cur_end = a, b
            elif b - cur_start <= TARGET_CHARS:
                cur_end = b
            else:
                chunks.append(Chunk(cur_start, cur_end, name))
                cur_start, cur_end = a, b
        if cur_start is not None:
            chunks.append(Chunk(cur_start, cur_end, name))
    return chunks


def render_meta(row: dict[str, Any]) -> str:
    """The one 'meta' chunk per episode (X1): makes title, time and people searchable."""
    when = row["occurred_at"].strftime("%Y-%m-%d %H:%M UTC")
    if row.get("ends_at"):
        when += f" ({int((row['ends_at'] - row['occurred_at']).total_seconds() // 60)} min)"
    people = "; ".join(
        f"{p.get('name') or ''} <{p.get('email') or p.get('handle') or ''}> ({p['role']})".strip()
        for p in row.get("participants") or []
    )
    lines = [
        f"Title: {row.get('title') or '(untitled)'}",
        f"Source: {row['source']}",
        f"Date: {when}",
    ]
    if people:
        lines.append(f"Participants: {people}")
    if loc := (row.get("meta") or {}).get("location"):
        lines.append(f"Location: {loc}")
    return "\n".join(lines)
