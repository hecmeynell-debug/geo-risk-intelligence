"""The extraction schema, confidence scoring, and the escalation decision."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gri.processing.confidence import (
    FAILED_CITATION_CEILING,
    REVIEW_THRESHOLD,
    assess,
)
from gri.processing.extract import (
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    ExtractionCall,
    compute_cost_usd,
    should_escalate,
)
from gri.processing.verify import verify_evidence
from gri.schemas import ExtractedActor, ExtractionResult
from gri.taxonomy import ENTITY_TYPES, EVENT_TYPES
from tests import factories


class TestSchemaEnforcesTheVocabularies:
    def test_event_type_must_be_in_the_closed_taxonomy(self) -> None:
        with pytest.raises(ValidationError):
            factories.make_event(event_type="general_geopolitical_risk")

    def test_person_is_not_a_valid_actor_type(self) -> None:
        """NG-4 at the schema level, matching the database CHECK constraint."""
        with pytest.raises(ValidationError):
            ExtractedActor(name="Someone", entity_type="person", role="operator")

    def test_schema_vocabularies_match_the_taxonomy_module(self) -> None:
        """One source of truth: schema, database, and prompt read the same sets."""
        schema = ExtractionResult.model_json_schema()
        defs = schema["$defs"]
        assert set(defs["ExtractedEvent"]["properties"]["event_type"]["enum"]) == EVENT_TYPES
        assert set(defs["ExtractedActor"]["properties"]["entity_type"]["enum"]) == ENTITY_TYPES

    def test_json_schema_forbids_additional_properties(self) -> None:
        """Required by the constrained-output path."""
        schema = ExtractionResult.model_json_schema()
        assert schema["additionalProperties"] is False

    def test_confidence_must_be_in_the_unit_interval(self) -> None:
        with pytest.raises(ValidationError):
            factories.make_event(confidence=1.4)

    def test_an_event_needs_at_least_one_quote(self) -> None:
        with pytest.raises(ValidationError):
            factories.make_event(quotes=[])


class TestOutcomeConsistency:
    """Constrained generation guarantees the shape, not the coherence."""

    def test_abstaining_with_an_event_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="event must be null"):
            ExtractionResult(
                in_niche=True,
                abstained=True,
                abstention_reason="thin",
                event=factories.make_event(),
            )

    def test_out_of_niche_with_an_event_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="event must be null"):
            ExtractionResult(
                in_niche=False,
                abstained=False,
                abstention_reason="not in scope",
                event=factories.make_event(),
            )

    def test_extracting_without_an_event_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no event was supplied"):
            ExtractionResult(in_niche=True, abstained=False, event=None)

    def test_abstaining_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="requires an abstention_reason"):
            ExtractionResult(in_niche=True, abstained=True, abstention_reason=None, event=None)

    def test_valid_abstention(self) -> None:
        result = factories.make_abstention()
        assert not result.produced_event


class TestConfidenceScoring:
    def test_well_evidenced_record_keeps_its_score(self) -> None:
        event = factories.make_event(confidence=0.92)
        report = verify_evidence(event.evidence, factories.NOTICE_TEXT)
        outcome = assess(event, report)

        assert report.all_verified
        assert outcome.confidence == pytest.approx(0.92)
        assert not outcome.requires_human_review
        assert outcome.band == "well_evidenced"

    def test_failed_citation_caps_confidence_and_forces_review(self) -> None:
        event = factories.make_result_with_fabricated_quote().event
        assert event is not None
        report = verify_evidence(event.evidence, factories.NOTICE_TEXT)
        outcome = assess(event, report)

        assert report.failed == 1
        assert outcome.confidence <= FAILED_CITATION_CEILING
        assert outcome.requires_human_review
        assert any("citations failed" in r for r in outcome.reasons)

    @pytest.mark.parametrize("severity", ["high", "severe"])
    def test_high_severity_always_requires_review(self, severity: str) -> None:
        """Trigger 4: a well-evidenced severe claim still gets a human."""
        event = factories.make_event(severity=severity, confidence=0.98)
        report = verify_evidence(event.evidence, factories.NOTICE_TEXT)
        outcome = assess(event, report)

        assert report.all_verified
        assert outcome.confidence >= 0.85
        assert outcome.requires_human_review

    def test_low_confidence_requires_review(self) -> None:
        event = factories.make_event(confidence=0.4)
        report = verify_evidence(event.evidence, factories.NOTICE_TEXT)
        assert assess(event, report).requires_human_review

    def test_conflicting_sources_force_review(self) -> None:
        event = factories.make_event(confidence=0.95)
        report = verify_evidence(event.evidence, factories.NOTICE_TEXT)
        outcome = assess(event, report, conflicting_sources=True)

        assert outcome.confidence < REVIEW_THRESHOLD
        assert outcome.requires_human_review

    def test_metadata_only_source_is_capped(self) -> None:
        event = factories.make_event(confidence=0.95)
        report = verify_evidence(event.evidence, factories.NOTICE_TEXT)
        outcome = assess(event, report, source_stores_full_text=False)

        assert outcome.confidence <= 0.5
        assert any("metadata only" in r for r in outcome.reasons)

    def test_missing_fields_reduce_confidence(self) -> None:
        full = factories.make_event(confidence=0.9)
        sparse = factories.make_event(
            confidence=0.9, with_locations=False, with_actors=False, with_sectors=False
        )
        full_report = verify_evidence(full.evidence, factories.NOTICE_TEXT)
        sparse_report = verify_evidence(sparse.evidence, factories.NOTICE_TEXT)

        assert assess(sparse, sparse_report).confidence < assess(full, full_report).confidence

    def test_confidence_is_never_raised_above_the_model_score(self) -> None:
        """Evidence can disprove optimism; it cannot corroborate it."""
        event = factories.make_event(confidence=0.7)
        report = verify_evidence(event.evidence, factories.NOTICE_TEXT)
        assert assess(event, report).confidence <= 0.7


class TestEscalationDecision:
    def _call(self, result: ExtractionResult | None, error: str | None = None) -> ExtractionCall:
        return ExtractionCall(
            model=DEFAULT_MODEL,
            prompt_name="extract_event",
            prompt_version="v1",
            result=result,
            error=error,
        )

    def test_no_escalation_when_the_first_pass_is_strong(self) -> None:
        assert should_escalate(self._call(factories.make_result(confidence=0.9)), 0) is None

    def test_escalates_on_failed_citations(self) -> None:
        reason = should_escalate(self._call(factories.make_result()), citation_failures=2)
        assert reason and "could not be found" in reason

    def test_escalates_on_low_confidence(self) -> None:
        reason = should_escalate(self._call(factories.make_result(confidence=0.3)), 0)
        assert reason and "below" in reason

    def test_escalates_on_an_in_niche_abstention(self) -> None:
        reason = should_escalate(self._call(factories.make_abstention()), 0)
        assert reason and "abstained" in reason

    def test_does_not_escalate_an_out_of_niche_judgement(self) -> None:
        """Cheap and reliable; spending Opus on it would waste the cascade."""
        assert should_escalate(self._call(factories.make_out_of_niche()), 0) is None

    def test_escalates_on_error(self) -> None:
        reason = should_escalate(self._call(None, error="api_status_error 500"), 0)
        assert reason and "failed" in reason


class TestCostAccounting:
    def test_sonnet_pricing(self) -> None:
        # 1M input + 1M output at $2 / $10.
        assert compute_cost_usd(DEFAULT_MODEL, 1_000_000, 1_000_000) == pytest.approx(12.0)

    def test_opus_pricing(self) -> None:
        assert compute_cost_usd(ESCALATION_MODEL, 1_000_000, 1_000_000) == pytest.approx(30.0)

    def test_escalation_is_more_expensive_for_the_same_tokens(self) -> None:
        assert compute_cost_usd(ESCALATION_MODEL, 10_000, 2_000) > compute_cost_usd(
            DEFAULT_MODEL, 10_000, 2_000
        )

    def test_unknown_model_costs_zero_rather_than_guessing(self) -> None:
        assert compute_cost_usd("some-future-model", 1000, 1000) == 0.0

    def test_zero_tokens_cost_nothing(self) -> None:
        assert compute_cost_usd(DEFAULT_MODEL, 0, 0) == 0.0
