"""Citation verification.

This is the load-bearing check of the whole project: it decides whether a quote the model
produced actually exists in the source. These tests pin both halves of that — what must
verify, and what must not.
"""

from __future__ import annotations

import pytest

from gri.ingestion.normalise import normalise_text
from gri.processing.verify import MIN_QUOTE_CHARS, verify_evidence, verify_quote
from gri.schemas import EvidenceQuote

DOCUMENT = normalise_text(
    "The port of Example is closed to all traffic following a channel obstruction. "
    "Vessels should expect delays of up to 72 hours. "
    "The authority will issue a further update at 0600 local time."
)


def quote(text: str, field: str = "summary") -> EvidenceQuote:
    return EvidenceQuote(field_supported=field, quote=text)


class TestQuotesThatMustVerify:
    def test_exact_quote(self) -> None:
        result = verify_quote("closed to all traffic", DOCUMENT)
        assert result.verified
        assert result.char_start is not None and result.char_end is not None
        assert DOCUMENT[result.char_start : result.char_end] == "closed to all traffic"

    def test_quote_differing_only_in_whitespace(self) -> None:
        """A model re-wrapping a quote across lines has not changed what it says."""
        assert verify_quote("closed   to  all\n traffic", DOCUMENT).verified

    def test_quote_with_a_non_breaking_space(self) -> None:
        nbsp = chr(0x00A0)
        assert verify_quote(f"closed{nbsp}to all traffic", DOCUMENT).verified

    def test_quote_with_an_invisible_character(self) -> None:
        assert verify_quote(f"closed to all{chr(0x200B)} traffic", DOCUMENT).verified

    def test_quote_with_surrounding_whitespace(self) -> None:
        assert verify_quote("   delays of up to 72 hours   ", DOCUMENT).verified

    def test_whole_document_as_a_quote(self) -> None:
        assert verify_quote(DOCUMENT, DOCUMENT).verified


class TestQuotesThatMustNotVerify:
    def test_fabricated_quote(self) -> None:
        """The failure this module exists to catch."""
        result = verify_quote("closed to all shipping indefinitely", DOCUMENT)
        assert not result.verified
        assert result.failure_reason == "quote not found in the stored source text"

    def test_paraphrase(self) -> None:
        assert not verify_quote("the port has been shut to every vessel", DOCUMENT).verified

    def test_single_changed_word(self) -> None:
        assert not verify_quote("delays of up to 96 hours", DOCUMENT).verified

    def test_stitched_together_passages(self) -> None:
        """Joining two separated passages misrepresents what the source said in one place."""
        assert not verify_quote(
            "closed to all traffic will issue a further update", DOCUMENT
        ).verified

    @pytest.mark.parametrize("short", ["closed", "port", "72", ""])
    def test_quote_too_short_to_be_evidence(self, short: str) -> None:
        """'closed' appears in every notice and would verify against anything."""
        result = verify_quote(short, DOCUMENT)
        assert not result.verified
        assert str(MIN_QUOTE_CHARS) in (result.failure_reason or "")

    def test_document_with_no_stored_text(self) -> None:
        result = verify_quote("closed to all traffic", None)
        assert not result.verified
        assert "metadata only" in (result.failure_reason or "")


class TestVerificationReport:
    def test_all_verified_when_every_quote_is_found(self) -> None:
        report = verify_evidence(
            [quote("closed to all traffic"), quote("delays of up to 72 hours", "severity")],
            DOCUMENT,
        )
        assert report.all_verified
        assert (report.checked, report.failed) == (2, 0)

    def test_one_bad_quote_fails_the_whole_report(self) -> None:
        report = verify_evidence(
            [quote("closed to all traffic"), quote("a completely invented passage")],
            DOCUMENT,
        )
        assert not report.all_verified
        assert (report.checked, report.failed) == (2, 1)

    def test_no_quotes_is_a_failure_not_a_vacuous_pass(self) -> None:
        """A record with no evidence is exactly what NG-3 and NG-5 forbid."""
        report = verify_evidence([], DOCUMENT)
        assert not report.all_verified
        assert report.checked == 0

    def test_field_is_carried_through(self) -> None:
        report = verify_evidence([quote("closed to all traffic", "event_type")], DOCUMENT)
        assert report.quotes[0].field_supported == "event_type"

    def test_verified_quotes_record_their_method(self) -> None:
        report = verify_evidence([quote("closed to all traffic")], DOCUMENT)
        assert report.quotes[0].method == "exact_normalised_substring"

    def test_offsets_locate_the_passage(self) -> None:
        report = verify_evidence([quote("delays of up to 72 hours")], DOCUMENT)
        found = report.quotes[0]
        assert found.char_start is not None and found.char_end is not None
        assert DOCUMENT[found.char_start : found.char_end] == "delays of up to 72 hours"
