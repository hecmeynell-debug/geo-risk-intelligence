"""Normalisation and hashing.

These functions decide what counts as a duplicate and, from Phase 2, what counts as a
verified quote. The tests pin the behaviour deliberately: if a change here makes one of
these fail, that is a corpus-wide migration, not a passing tweak.
"""

from __future__ import annotations

import pytest

from gri.ingestion.normalise import (
    content_hash,
    metadata_hash,
    normalise_text,
    sha256_bytes,
    sha256_text,
)


class TestNormaliseText:
    def test_collapses_whitespace_runs(self) -> None:
        assert normalise_text("Port   closed\n\n  to traffic") == "Port closed to traffic"

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        assert normalise_text("  Port closed  ") == "Port closed"

    @pytest.mark.parametrize("line_ending", ["\r\n", "\r", "\n"])
    def test_line_endings_are_equivalent(self, line_ending: str) -> None:
        assert normalise_text(f"a{line_ending}b") == "a b"

    def test_nfkc_folds_non_breaking_space(self) -> None:
        nbsp = chr(0x00A0)
        assert normalise_text(f"Example{nbsp}Port") == "Example Port"

    def test_nfkc_folds_full_width_characters(self) -> None:
        full_width = "".join(chr(ord(c) - ord("A") + 0xFF21) for c in "PORT")
        assert normalise_text(full_width) == "PORT"

    @pytest.mark.parametrize(
        "code_point",
        [0x200B, 0x200E, 0x202D, 0x2060, 0xFEFF],
        ids=["zero-width-space", "ltr-mark", "ltr-override", "word-joiner", "bom"],
    )
    def test_strips_invisible_characters(self, code_point: int) -> None:
        assert normalise_text(f"Port{chr(code_point)} closed") == "Port closed"

    def test_invisible_characters_do_not_change_the_hash(self) -> None:
        """A CMS paste artefact must not fork a document into a false duplicate."""
        polluted = f"Example{chr(0x200B)} Port is closed."
        assert content_hash(polluted) == content_hash("Example Port is closed.")

    def test_empty_input(self) -> None:
        assert normalise_text("") == ""
        assert normalise_text("   \n  ") == ""

    def test_is_idempotent(self) -> None:
        once = normalise_text("  Example  Port \r\n closed ")
        assert normalise_text(once) == once


class TestContentHash:
    def test_is_a_sha256_hex_digest(self) -> None:
        digest = content_hash("Example Port is closed.")
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_ignores_reformatting(self) -> None:
        """A publisher re-flowing its own text must not read as a new document."""
        assert content_hash("Example Port\n\nis   closed.") == content_hash(
            "Example Port is closed."
        )

    def test_distinguishes_edited_text(self) -> None:
        """But an actual edit must produce a different document."""
        assert content_hash("Port closed to all traffic.") != content_hash(
            "Port closed to some traffic."
        )

    def test_is_case_sensitive(self) -> None:
        # Case can be meaningful in notices (vessel names, IMO codes), so it is kept.
        assert content_hash("Port Closed") != content_hash("port closed")

    def test_matches_manual_composition(self) -> None:
        assert content_hash(" a  b ") == sha256_text("a b")


class TestMetadataHash:
    def test_stable_for_same_inputs(self) -> None:
        args = ("Title", "https://example.invalid/1", "2026-09-01T00:00:00Z")
        assert metadata_hash(*args) == metadata_hash(*args)

    def test_field_boundaries_cannot_collide(self) -> None:
        """("ab", url, None) and ("a", url, None) with a shifted boundary must differ."""
        assert metadata_hash("ab", "https://x.invalid/", None) != metadata_hash(
            "a", "bhttps://x.invalid/", None
        )

    def test_missing_optional_fields_are_allowed(self) -> None:
        digest = metadata_hash(None, "https://example.invalid/1", None)
        assert len(digest) == 64

    def test_differs_from_content_hash_of_same_title(self) -> None:
        assert metadata_hash("Port closed", "https://x.invalid/", None) != content_hash(
            "Port closed"
        )


def test_sha256_bytes_hashes_raw_payload() -> None:
    assert sha256_bytes(b"") == ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
