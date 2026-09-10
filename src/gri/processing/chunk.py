"""Splitting a document's ``clean_text`` into chunks.

The invariant that matters: **a chunk's text is exactly ``clean_text[char_start:char_end]``**.
Everything downstream depends on it — citation offsets are recorded against the same
normalised text, so a chunker that trimmed, re-joined, or re-normalised its output would
silently break the ability to locate a quote in the source.

Notices in this domain are short. Chunking exists so that a long circular does not dilute
its own embedding, not because documents routinely exceed a context window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Target chunk size in characters. Notices are short and factual; large chunks blur the
#: single fact an embedding needs to capture.
DEFAULT_MAX_CHARS = 800

#: Overlap between consecutive chunks, so a sentence spanning a boundary is still wholly
#: present in at least one chunk.
DEFAULT_OVERLAP_CHARS = 120

#: A chunk shorter than this is merged into its predecessor rather than kept alone -- a
#: 12-character trailing fragment embeds to noise.
MIN_CHUNK_CHARS = 80

#: Sentence boundary: terminator, then whitespace. Applied to already-normalised text, so
#: whitespace is always a single space.
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s")


@dataclass(frozen=True)
class Chunk:
    """A passage of a document, with exact offsets back into ``clean_text``."""

    index: int
    text: str
    char_start: int
    char_end: int

    @property
    def length(self) -> int:
        return self.char_end - self.char_start


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of sentences, covering the whole string with no gaps."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        spans.append((cursor, end))
        cursor = end
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


def chunk_text(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split normalised text into overlapping chunks aligned to sentence boundaries.

    Returns an empty list for blank input. A document shorter than ``max_chars`` yields
    exactly one chunk covering the whole text.
    """
    if not text or not text.strip():
        return []

    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be smaller than max_chars")

    spans = _sentence_spans(text)
    if not spans:
        return []

    # Greedily pack whole sentences up to max_chars. A single sentence longer than
    # max_chars becomes its own oversized chunk rather than being cut mid-word: an
    # arbitrary split would produce a chunk whose text still maps to real offsets but
    # reads as a fragment, which is worse for both embedding and review.
    raw: list[tuple[int, int]] = []
    start, end = spans[0]
    for span_start, span_end in spans[1:]:
        if span_end - start <= max_chars:
            end = span_end
            continue
        raw.append((start, end))
        # Step back by the overlap, but never before the previous chunk's start.
        start = max(start, span_start - overlap_chars) if overlap_chars else span_start
        # Realign to a sentence boundary at or before the overlap point.
        start = _align_to_boundary(spans, start, span_start)
        end = span_end
    raw.append((start, end))

    merged = _merge_short_tail(raw)
    return [
        Chunk(index=i, text=text[s:e], char_start=s, char_end=e) for i, (s, e) in enumerate(merged)
    ]


def _align_to_boundary(spans: list[tuple[int, int]], target: int, upper: int) -> int:
    """Snap ``target`` back to the start of the sentence containing it."""
    candidate = upper
    for span_start, _ in spans:
        if span_start <= target:
            candidate = span_start
        else:
            break
    return min(candidate, upper)


def _merge_short_tail(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Fold a too-short final chunk back into its predecessor."""
    if len(spans) < 2:
        return spans
    *head, (last_start, last_end) = spans
    if last_end - last_start >= MIN_CHUNK_CHARS:
        return [*head, (last_start, last_end)]
    prev_start, _ = head[-1]
    return [*head[:-1], (prev_start, last_end)]


def verify_offsets(text: str, chunks: list[Chunk]) -> bool:
    """True when every chunk's text matches its own offsets into ``text``.

    Cheap enough to assert in the pipeline, and it guards the one invariant whose failure
    would be invisible until a citation could not be located months later.
    """
    return all(text[c.char_start : c.char_end] == c.text for c in chunks)
