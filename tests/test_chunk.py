"""Chunking.

The offset invariant is the point of these tests. If a chunk's text stops matching its
own offsets, citations become unlocatable — and that failure would be invisible until
someone tried to audit a record months later.
"""

from __future__ import annotations

import pytest

from gri.ingestion.normalise import normalise_text
from gri.processing.chunk import (
    MIN_CHUNK_CHARS,
    chunk_text,
    verify_offsets,
)

NOTICE = normalise_text(
    "The port of Example is closed to all traffic following a channel obstruction. "
    "Vessels should expect delays of up to 72 hours. "
    "The authority will issue a further update at 0600 local time. "
    "Masters are advised to contact port control on VHF channel 12. "
    "Anchorage remains available to the north of the fairway. "
    "No injuries have been reported."
)


class TestOffsetInvariant:
    """A chunk's text must be exactly clean_text[char_start:char_end]."""

    def test_offsets_match_for_a_typical_notice(self) -> None:
        chunks = chunk_text(NOTICE, max_chars=120, overlap_chars=20)
        assert verify_offsets(NOTICE, chunks)

    @pytest.mark.parametrize("max_chars", [60, 100, 200, 400, 800, 5000])
    def test_offsets_match_at_every_chunk_size(self, max_chars: int) -> None:
        chunks = chunk_text(NOTICE, max_chars=max_chars, overlap_chars=min(20, max_chars // 4))
        assert verify_offsets(NOTICE, chunks)
        assert chunks

    def test_offsets_match_with_no_overlap(self) -> None:
        chunks = chunk_text(NOTICE, max_chars=100, overlap_chars=0)
        assert verify_offsets(NOTICE, chunks)

    def test_verify_offsets_detects_a_broken_chunk(self) -> None:
        chunks = chunk_text(NOTICE, max_chars=120, overlap_chars=20)
        from dataclasses import replace

        tampered = [replace(chunks[0], text="not what is at those offsets"), *chunks[1:]]
        assert not verify_offsets(NOTICE, tampered)


class TestCoverage:
    def test_every_character_is_in_at_least_one_chunk(self) -> None:
        chunks = chunk_text(NOTICE, max_chars=120, overlap_chars=20)
        covered = set()
        for c in chunks:
            covered.update(range(c.char_start, c.char_end))
        assert covered == set(range(len(NOTICE)))

    def test_chunks_are_ordered_and_indexed_from_zero(self) -> None:
        chunks = chunk_text(NOTICE, max_chars=120, overlap_chars=20)
        assert [c.index for c in chunks] == list(range(len(chunks)))
        assert all(chunks[i].char_start <= chunks[i + 1].char_start for i in range(len(chunks) - 1))

    def test_short_document_is_one_chunk(self) -> None:
        text = "Example Port is closed to all traffic."
        chunks = chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert (chunks[0].char_start, chunks[0].char_end) == (0, len(text))


class TestEdgeCases:
    @pytest.mark.parametrize("text", ["", "   ", "\n\n"])
    def test_blank_input_yields_nothing(self, text: str) -> None:
        assert chunk_text(text) == []

    def test_single_sentence_longer_than_max_is_not_split_mid_word(self) -> None:
        """An arbitrary cut would produce a fragment that reads as nonsense in review."""
        long_sentence = normalise_text("word " * 400)
        chunks = chunk_text(long_sentence, max_chars=100, overlap_chars=10)
        assert verify_offsets(long_sentence, chunks)
        assert len(chunks) == 1

    def test_no_tiny_trailing_chunk(self) -> None:
        text = normalise_text("A sentence that is reasonably long and informative. " * 6 + "Ok.")
        chunks = chunk_text(text, max_chars=150, overlap_chars=20)
        assert verify_offsets(text, chunks)
        if len(chunks) > 1:
            assert chunks[-1].length >= MIN_CHUNK_CHARS

    def test_overlap_must_be_smaller_than_max(self) -> None:
        with pytest.raises(ValueError, match="overlap_chars must be smaller"):
            chunk_text(NOTICE, max_chars=100, overlap_chars=100)

    def test_text_with_no_sentence_terminator(self) -> None:
        text = "no terminator here just words that keep going for a while"
        chunks = chunk_text(text, max_chars=30, overlap_chars=5)
        assert verify_offsets(text, chunks)
        assert chunks
