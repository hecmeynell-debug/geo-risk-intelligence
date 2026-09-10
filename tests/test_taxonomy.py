"""The closed vocabularies, and the NG-4 guarantee that depends on one of them."""

from __future__ import annotations

import pytest

from gri import taxonomy


class TestNoIndividualTargeting:
    """NG-4 is enforced by the *absence* of a person entity type. These tests exist so
    that a future edit re-introducing one fails loudly rather than silently widening what
    the system can store."""

    def test_entity_types_exclude_person(self) -> None:
        forbidden = {"person", "individual", "human", "crew_member", "official"}
        assert not (taxonomy.ENTITY_TYPES & forbidden), (
            "NG-4: person-level entity types must never be storable"
        )

    @pytest.mark.parametrize("term", ["person", "individual", "crew", "passenger"])
    def test_no_entity_type_contains_person_language(self, term: str) -> None:
        assert not [t for t in taxonomy.ENTITY_TYPES if term in t]

    def test_location_precisions_exclude_exact_position(self) -> None:
        # Locations describe fixed features, not tracked positions.
        assert "exact" not in taxonomy.LOCATION_PRECISIONS
        assert "gps" not in taxonomy.LOCATION_PRECISIONS


class TestClosedVocabularies:
    @pytest.mark.parametrize(
        "vocabulary",
        [
            taxonomy.EVENT_TYPES,
            taxonomy.SECTORS,
            taxonomy.ENTITY_TYPES,
            taxonomy.ACTOR_ROLES,
            taxonomy.LOCATION_TYPES,
            taxonomy.RETRIEVAL_METHODS,
            taxonomy.DOCUMENT_RELATIONS,
            taxonomy.REVIEW_STATUSES,
        ],
    )
    def test_non_empty(self, vocabulary: frozenset[str]) -> None:
        assert vocabulary

    def test_severity_is_ordered_least_to_most(self) -> None:
        assert taxonomy.SEVERITY_LEVELS[0] == "informational"
        assert taxonomy.SEVERITY_LEVELS[-1] == "severe"
        assert len(set(taxonomy.SEVERITY_LEVELS)) == len(taxonomy.SEVERITY_LEVELS)

    def test_high_severities_always_reviewed(self) -> None:
        assert taxonomy.SEVERITY_ALWAYS_REVIEW == {"high", "severe"}
        assert taxonomy.SEVERITY_ALWAYS_REVIEW <= set(taxonomy.SEVERITY_LEVELS)

    def test_document_relations_cover_the_four_cases(self) -> None:
        # ADR-0001 D7: new / update / duplicate / background.
        assert taxonomy.DOCUMENT_RELATIONS == {
            "new_event",
            "update",
            "duplicate",
            "background",
        }

    def test_abstention_is_a_tracked_outcome(self) -> None:
        assert "abstained" in taxonomy.EXTRACTION_OUTCOMES
        assert "insufficient_evidence" in taxonomy.EVAL_LABEL_TYPES

    def test_sorted_values_is_deterministic(self) -> None:
        assert taxonomy.sorted_values(frozenset({"b", "a"})) == ["a", "b"]
