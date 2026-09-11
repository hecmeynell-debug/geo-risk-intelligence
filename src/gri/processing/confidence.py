"""Confidence scoring and the human-review decision.

CONSTRAINTS.md Section 6 defines six triggers. This module implements them as code; the
database enforces two of them again as CHECK constraints, so a bug here cannot publish a
record that should have been reviewed.

The model supplies a self-assessed confidence. We do not take it at face value: it is an
input that verification evidence can only ever *lower*. A model that failed its citations
does not get to keep a high score for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gri.processing.verify import VerificationReport
from gri.schemas import ExtractedEvent
from gri.taxonomy import SEVERITY_ALWAYS_REVIEW

#: Below this, a record always goes to review (CONSTRAINTS.md Section 6, trigger 1).
REVIEW_THRESHOLD = 0.60

#: Ceiling applied when any citation failed. A record with a fabricated quote cannot be
#: "mostly fine": it is by definition weakly evidenced.
FAILED_CITATION_CEILING = 0.35

#: Ceiling when the source could not supply quotable text at all.
NO_QUOTABLE_TEXT_CEILING = 0.50

#: Penalty per material field left empty. Small individually; they accumulate.
MISSING_FIELD_PENALTY = 0.05


@dataclass
class ConfidenceAssessment:
    """The scored outcome, and why."""

    confidence: float
    requires_human_review: bool
    #: Human-readable trigger names, recorded so a reviewer sees why it reached them.
    reasons: list[str] = field(default_factory=list)

    @property
    def band(self) -> str:
        if self.confidence >= 0.85:
            return "well_evidenced"
        if self.confidence >= REVIEW_THRESHOLD:
            return "partially_evidenced"
        return "weak"


def assess(
    event: ExtractedEvent,
    verification: VerificationReport,
    *,
    source_stores_full_text: bool = True,
    conflicting_sources: bool = False,
    is_update_without_grounded_change: bool = False,
) -> ConfidenceAssessment:
    """Score a candidate record and decide whether it needs a human.

    ``event.confidence`` is the model's self-assessment. It is only ever reduced here --
    never raised -- because the evidence checks can disprove the model's optimism but
    cannot corroborate it.
    """
    confidence = float(event.confidence)
    reasons: list[str] = []

    # Trigger 2: a failed citation is the strongest possible signal.
    if verification.failed > 0:
        confidence = min(confidence, FAILED_CITATION_CEILING)
        reasons.append(
            f"{verification.failed} of {verification.checked} citations failed verification"
        )

    if verification.checked == 0:
        confidence = min(confidence, FAILED_CITATION_CEILING)
        reasons.append("no evidence quotes were supplied")

    if not source_stores_full_text:
        confidence = min(confidence, NO_QUOTABLE_TEXT_CEILING)
        reasons.append("source terms permit metadata only, so quotes cannot be verified")

    # Missing material fields. Each is a small dent in how completely the record is
    # grounded, not a disqualification.
    missing = [
        name
        for name, present in (
            ("locations", bool(event.locations)),
            ("actors", bool(event.actors)),
            ("affected_sectors", bool(event.affected_sectors)),
        )
        if not present
    ]
    if missing:
        confidence -= MISSING_FIELD_PENALTY * len(missing)
        reasons.append(f"no {', '.join(missing)} extracted")

    # Trigger 3.
    if conflicting_sources:
        confidence = min(confidence, REVIEW_THRESHOLD - 0.01)
        reasons.append("sources conflict on a material field")

    # Trigger 6.
    if is_update_without_grounded_change:
        reasons.append(
            "classified as an update but the change note is not grounded in new evidence"
        )

    confidence = max(0.0, min(1.0, confidence))

    requires_review = False
    if confidence < REVIEW_THRESHOLD:
        requires_review = True
        if not reasons:
            reasons.append(f"confidence {confidence:.2f} is below {REVIEW_THRESHOLD}")
    # Trigger 4: high-impact claims always get a human, however well evidenced.
    if event.severity in SEVERITY_ALWAYS_REVIEW:
        requires_review = True
        reasons.append(f"severity is {event.severity}, which always requires review")
    if verification.failed > 0 or verification.checked == 0:
        requires_review = True
    if conflicting_sources or is_update_without_grounded_change:
        requires_review = True

    return ConfidenceAssessment(
        confidence=round(confidence, 3),
        requires_human_review=requires_review,
        reasons=reasons,
    )


def flag_must_stay(severity: str, confidence: float | None) -> list[str]:
    """Database guarantees that keep ``requires_human_review`` set, whatever a human says.

    Mirrors the two CHECK constraints on ``events``. Checked here so the result can
    explain itself, rather than surfacing as a constraint violation.
    """
    kept: list[str] = []
    if severity in SEVERITY_ALWAYS_REVIEW:
        kept.append(
            f"severity is '{severity}': high-impact records always carry the review flag"
            " (ck_events_high_severity_requires_review)"
        )
    if confidence is not None and confidence < REVIEW_THRESHOLD:
        kept.append(
            f"confidence {confidence:.2f} is below {REVIEW_THRESHOLD}"
            " (ck_events_low_confidence_requires_review)"
        )
    return kept
