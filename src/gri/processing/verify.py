"""Citation verification.

ADR-0002 D4. This module is the single most load-bearing piece of the project: it turns
"the model says it cited this" into a fact that deterministic code has checked.

**It is not a model call and must never become one.** Asking a model whether a quote
appears in a document reintroduces exactly the failure it exists to catch.

Matching is normalised on both sides using the same functions that produced
``clean_text`` (:mod:`gri.ingestion.normalise`), so a quote differing only in whitespace
or Unicode compatibility form still verifies, while a quote with different *words* does
not.

What this catches: fabricated quotes, and quotes attributed to the wrong document.
What it does not catch: a real quote attached to a wrong conclusion. That residual risk
is absorbed by the confidence score and the human review queue, and it is disclosed
rather than papered over.
"""

from __future__ import annotations

from dataclasses import dataclass

from gri.ingestion.normalise import normalise_text
from gri.logging import get_logger
from gri.schemas import EvidenceQuote

log = get_logger(__name__)

#: A quote shorter than this is not evidence -- "closed" appears in every notice and
#: would verify against anything. Matches EvidenceQuote.quote's min_length.
MIN_QUOTE_CHARS = 8


@dataclass(frozen=True)
class VerifiedQuote:
    """One quote checked against one document."""

    field_supported: str
    quote: str
    verified: bool
    #: Offsets into the NORMALISED document text, not the raw body.
    char_start: int | None = None
    char_end: int | None = None
    failure_reason: str | None = None
    method: str = "exact_normalised_substring"


@dataclass(frozen=True)
class VerificationReport:
    """The outcome for a whole extraction."""

    quotes: list[VerifiedQuote]

    @property
    def checked(self) -> int:
        return len(self.quotes)

    @property
    def failed(self) -> int:
        return sum(1 for q in self.quotes if not q.verified)

    @property
    def all_verified(self) -> bool:
        """True only when there is at least one quote and every one of them verified.

        Zero quotes is a failure, not a vacuous pass: a record with no evidence is exactly
        what NG-3 and NG-5 forbid.
        """
        return self.checked > 0 and self.failed == 0

    @property
    def failure_reasons(self) -> list[str]:
        return [q.failure_reason for q in self.quotes if q.failure_reason]


def verify_quote(quote: str, document_text: str | None) -> VerifiedQuote:
    """Locate one quote in one document's text."""
    field_placeholder = ""

    if document_text is None:
        return VerifiedQuote(
            field_supported=field_placeholder,
            quote=quote,
            verified=False,
            failure_reason=(
                "document has no stored text; this source's terms permit metadata only,"
                " so it cannot supply quoted evidence"
            ),
        )

    stripped = quote.strip()
    if len(stripped) < MIN_QUOTE_CHARS:
        return VerifiedQuote(
            field_supported=field_placeholder,
            quote=quote,
            verified=False,
            failure_reason=f"quote is shorter than {MIN_QUOTE_CHARS} characters",
        )

    haystack = normalise_text(document_text)
    needle = normalise_text(stripped)

    position = haystack.find(needle)
    if position == -1:
        return VerifiedQuote(
            field_supported=field_placeholder,
            quote=quote,
            verified=False,
            failure_reason="quote not found in the stored source text",
        )

    return VerifiedQuote(
        field_supported=field_placeholder,
        quote=quote,
        verified=True,
        char_start=position,
        char_end=position + len(needle),
    )


def verify_evidence(evidence: list[EvidenceQuote], document_text: str | None) -> VerificationReport:
    """Check every quote in an extraction against the document it claims to come from."""
    results: list[VerifiedQuote] = []

    for item in evidence:
        outcome = verify_quote(item.quote, document_text)
        # verify_quote does not know the field, so attach it here rather than threading
        # it through a function whose job is purely string location.
        results.append(
            VerifiedQuote(
                field_supported=item.field_supported,
                quote=outcome.quote,
                verified=outcome.verified,
                char_start=outcome.char_start,
                char_end=outcome.char_end,
                failure_reason=outcome.failure_reason,
                method=outcome.method,
            )
        )

    report = VerificationReport(quotes=results)
    if not report.all_verified:
        log.warning(
            "citation_verification_failed",
            checked=report.checked,
            failed=report.failed,
            reasons=report.failure_reasons[:5],
        )
    return report
