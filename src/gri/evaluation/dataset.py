"""The Phase 4 labelled set: synthetic documents with hand-written expected outcomes.

Every document is invented, the way ``scripts/live_extraction_smoke.py`` and
``tests/factories.py`` already are -- no candidate source's terms have been reviewed
(NG-2), so nothing here may depend on a real report. Each :class:`EvalDocument` pairs a
document with the "model output" a correct extraction would produce for it
(:class:`~gri.schemas.ExtractionResult`) and the outcome the pipeline should reach.

ADR-0006 explains why this dataset scores classification and review-routing quality
rather than duplicating the citation gate, and why ``update``/``duplicate`` examples run
two documents in sequence rather than one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from gri.ingestion.normalise import normalise_text
from gri.schemas import (
    EvidenceQuote,
    ExtractedActor,
    ExtractedEvent,
    ExtractedLocation,
    ExtractionResult,
)
from gri.taxonomy import EVAL_LABEL_TYPES

#: Identifies this dataset's rows in ``evaluation_datasets`` / ``evaluation_examples``.
#: Bump the version if the dataset's documents or expectations change; a new version
#: leaves prior runs' history attributable to the dataset they were actually run against.
DATASET_NAME = "phase4-core"
DATASET_VERSION = "v1"


@dataclass(frozen=True)
class EvalDocument:
    """One document to run through the pipeline, and what it should produce.

    ``expected`` is compared field by field against what the pipeline actually did
    (:func:`gri.evaluation.harness.run_scenario`); every key below is always present, with
    ``None`` standing in for "not applicable" rather than "not checked" -- a document that
    should create no event is asserting that just as much as one that should.

    * ``relation``: ``new_event`` / ``update`` / ``duplicate``, or ``None`` when no event
      is expected (``background`` or ``insufficient_evidence``).
    * ``outcome``: the exact :attr:`gri.processing.pipeline.PipelineResult.outcome`
      string -- ``published``, ``needs_review``, ``background``, or ``abstained``.
    * ``event_created``: whether an event should exist afterward.
    * ``requires_human_review`` / ``severity``: the created event's fields, or ``None``
      when ``event_created`` is ``False``.
    """

    key: str
    label_type: str
    title: str
    text: str
    result: ExtractionResult
    expected: dict[str, Any]
    #: Whether this document is itself measured (an ``EvaluationExample`` row) or only
    #: sets up state for a later document in the same scenario (ADR-0006).
    scored: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        if self.label_type not in EVAL_LABEL_TYPES | {"__setup__"}:
            raise ValueError(f"{self.label_type!r} is not in EVAL_LABEL_TYPES")


@dataclass(frozen=True)
class EvalScenario:
    """A sequence of documents run through the pipeline in order, against one source.

    ``same_cluster`` selects the embedding provider: ``True`` uses
    :class:`~gri.processing.embed.SameVectorProvider` so every document in the sequence
    lands in one cluster (needed for ``update``/``duplicate``, which must be reports of
    the *same* event to mean anything); ``False`` uses
    :class:`~gri.processing.embed.DeterministicFakeProvider` so unrelated documents stay
    unrelated.
    """

    key: str
    documents: tuple[EvalDocument, ...]
    same_cluster: bool = False


def _event(
    *,
    event_type: str = "port_disruption",
    event_date_: date = date(2026, 9, 1),
    title: str,
    summary: str,
    severity: str,
    severity_rationale: str,
    quotes: list[tuple[str, str]],
    confidence: float,
) -> ExtractedEvent:
    return ExtractedEvent(
        event_type=event_type,
        event_date=event_date_,
        title=title,
        summary=summary,
        severity=severity,
        severity_rationale=severity_rationale,
        locations=[
            ExtractedLocation(
                name="Example Port", location_type="port", country_code="GB", precision="facility"
            )
        ],
        actors=[
            ExtractedActor(
                name="Example Port Authority",
                entity_type="port_authority",
                role="authority_issuing_notice",
            )
        ],
        affected_sectors=["ports_terminals", "maritime_shipping"],
        evidence=[EvidenceQuote(field_supported=f, quote=q) for f, q in quotes],
        confidence=confidence,
    )


def _result(event: ExtractedEvent) -> ExtractionResult:
    return ExtractionResult(in_niche=True, abstained=False, abstention_reason=None, event=event)


# -- new_event: three severities/confidences, to exercise both review triggers ----------

_CLOSURE_TEXT = normalise_text(
    "NOTICE TO MARINERS. The port of Example is closed to all traffic following a "
    "channel obstruction identified during a routine survey. Vessels should expect "
    "delays of up to 72 hours. The Example Port Authority will issue a further update "
    "at 0600 local time."
)
_CLOSURE_QUOTES = [
    ("summary", "closed to all traffic following a channel obstruction"),
    ("severity", "delays of up to 72 hours"),
]
_CLOSURE_RATIONALE = "The source reports a closure with delays of up to 72 hours."
_FOLLOW_UP_SUMMARY = (
    "The notice reports the closure continues, with delays now expected up to ten days."
)


def _closure(*, key: str, severity: str, confidence: float, outcome: str) -> EvalScenario:
    needs_review = outcome == "needs_review"
    return EvalScenario(
        key=key,
        documents=(
            EvalDocument(
                key=key,
                label_type="new_event",
                title="Example Port closed after channel obstruction",
                text=_CLOSURE_TEXT,
                result=_result(
                    _event(
                        title="Example Port closed after channel obstruction",
                        summary=(
                            "The notice reports Example Port closed to all traffic "
                            "following a channel obstruction, with delays of up to 72 "
                            "hours expected."
                        ),
                        severity=severity,
                        severity_rationale=_CLOSURE_RATIONALE,
                        quotes=_CLOSURE_QUOTES,
                        confidence=confidence,
                    )
                ),
                expected={
                    "relation": "new_event",
                    "outcome": outcome,
                    "event_created": True,
                    "requires_human_review": needs_review,
                    "severity": severity,
                },
            ),
        ),
    )


NEW_EVENT_MODERATE = _closure(
    key="new_event_moderate", severity="moderate", confidence=0.90, outcome="published"
)
NEW_EVENT_SEVERE = _closure(
    key="new_event_severe", severity="severe", confidence=0.90, outcome="needs_review"
)
NEW_EVENT_LOW_CONFIDENCE = _closure(
    key="new_event_low_confidence", severity="moderate", confidence=0.55, outcome="needs_review"
)


# -- update / duplicate: a second report against the first's event ----------------------

_FOLLOW_UP_TEXT = normalise_text(
    "Example Port remains closed to all traffic following the channel obstruction. "
    "The Example Port Authority now expects delays of up to ten days. Vessels are "
    "being held at Example Anchorage."
)

_SETUP = EvalDocument(
    key="_setup_closure",
    label_type="__setup__",
    title="Example Port closed after channel obstruction",
    text=_CLOSURE_TEXT,
    result=_result(
        _event(
            title="Example Port closed after channel obstruction",
            summary=(
                "The notice reports Example Port closed to all traffic following a "
                "channel obstruction, with delays of up to 72 hours expected."
            ),
            severity="moderate",
            severity_rationale=_CLOSURE_RATIONALE,
            quotes=_CLOSURE_QUOTES,
            confidence=0.90,
        )
    ),
    expected={
        "relation": "new_event",
        "outcome": "published",
        "event_created": True,
        "requires_human_review": False,
        "severity": "moderate",
    },
    scored=False,
    notes="Sets up the event the update/duplicate document reports against; not itself measured.",
)

UPDATE = EvalScenario(
    key="update",
    same_cluster=True,
    documents=(
        _SETUP,
        EvalDocument(
            key="update",
            label_type="update",
            title="Example Port closure extended",
            text=_FOLLOW_UP_TEXT,
            # Severity escalates from the first report's 'moderate' to 'high': a real
            # field change, which is what makes consolidation classify this as an update
            # rather than a duplicate (tests/test_consolidation_integration.py).
            result=_result(
                _event(
                    title="Example Port closure extended",
                    summary=_FOLLOW_UP_SUMMARY,
                    severity="high",
                    severity_rationale="The source now reports delays of up to ten days.",
                    quotes=[
                        ("summary", "Example Port remains closed to all traffic"),
                        ("severity", "now expects delays of up to ten days"),
                    ],
                    confidence=0.90,
                )
            ),
            expected={
                "relation": "update",
                "outcome": "needs_review",
                "event_created": True,
                "requires_human_review": True,
                "severity": "high",
            },
            notes="Trigger 4: severity escalates to 'high', which always routes to review.",
        ),
    ),
)

DUPLICATE = EvalScenario(
    key="duplicate",
    same_cluster=True,
    documents=(
        _SETUP,
        EvalDocument(
            key="duplicate",
            label_type="duplicate",
            title="Example Port closure corroborated",
            text=_FOLLOW_UP_TEXT,
            # Same severity as the setup document: no field changes, so this corroborates
            # rather than updates (tests/test_consolidation_integration.py).
            result=_result(
                _event(
                    title="Example Port closure extended",
                    summary=_FOLLOW_UP_SUMMARY,
                    severity="moderate",
                    severity_rationale=_CLOSURE_RATIONALE,
                    quotes=[("summary", "Example Port remains closed to all traffic")],
                    confidence=0.90,
                )
            ),
            expected={
                "relation": "duplicate",
                "outcome": "published",
                "event_created": True,
                "requires_human_review": False,
                "severity": "moderate",
            },
            notes="Corroborates the setup document's facts; changes nothing on the record.",
        ),
    ),
)


# -- background / insufficient_evidence: no event either way -----------------------------

_GRID_TEXT = normalise_text(
    "Scheduled maintenance on the Northern Grid interconnector will reduce transfer "
    "capacity by 400 MW between 3 and 5 October. Operators have been notified."
)

BACKGROUND = EvalScenario(
    key="background",
    documents=(
        EvalDocument(
            key="background",
            label_type="background",
            title="Unrelated grid maintenance notice",
            text=_GRID_TEXT,
            result=ExtractionResult(
                in_niche=False,
                abstained=False,
                abstention_reason="This is routine grid maintenance, not a reported disruption.",
                event=None,
            ),
            expected={
                "relation": None,
                "outcome": "background",
                "event_created": False,
                "requires_human_review": None,
                "severity": None,
            },
            notes="Out of niche: NG-1's boundary, not a disruption report at all.",
        ),
    ),
)

_THIN_TEXT = normalise_text(
    "Local reports suggest there may have been some disruption at a port in the region "
    "earlier this week. Officials have not confirmed whether operations were affected. "
    "Shipping sources indicated that conditions were being monitored."
)

INSUFFICIENT_EVIDENCE = EvalScenario(
    key="insufficient_evidence",
    documents=(
        EvalDocument(
            key="insufficient_evidence",
            label_type="insufficient_evidence",
            title="Thin, hedged report",
            text=_THIN_TEXT,
            result=ExtractionResult(
                in_niche=True,
                abstained=True,
                abstention_reason=(
                    "No confirmed facts: location, port, and impact are all unnamed or hedged."
                ),
                event=None,
            ),
            expected={
                "relation": None,
                "outcome": "abstained",
                "event_created": False,
                "requires_human_review": None,
                "severity": None,
            },
            notes=(
                "Correct abstention is a success (CONSTRAINTS.md Section 6), not a "
                "failure to measure down."
            ),
        ),
    ),
)


SCENARIOS: tuple[EvalScenario, ...] = (
    NEW_EVENT_MODERATE,
    NEW_EVENT_SEVERE,
    NEW_EVENT_LOW_CONFIDENCE,
    UPDATE,
    DUPLICATE,
    BACKGROUND,
    INSUFFICIENT_EVIDENCE,
)


def scored_documents() -> list[EvalDocument]:
    """Every document across every scenario that is itself measured."""
    return [doc for scenario in SCENARIOS for doc in scenario.documents if doc.scored]
