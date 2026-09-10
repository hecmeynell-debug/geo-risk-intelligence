"""Schema shape assertions that need no database.

These are cheap and run everywhere, including in CI without a Postgres service.
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint

from gri.models import Base

EXPECTED_TABLES = {
    "sources",
    "ingestion_runs",
    "raw_documents",
    "document_chunks",
    "event_clusters",
    "events",
    "event_locations",
    "entities",
    "event_actors",
    "event_sectors",
    "event_documents",
    "event_evidence",
    "review_decisions",
    "prompts",
    "extractions",
    "evaluation_datasets",
    "evaluation_examples",
    "evaluation_runs",
    "evaluation_results",
}


def _check_sql(table_name: str) -> str:
    table = Base.metadata.tables[table_name]
    return " ".join(str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint))


def test_all_expected_tables_exist() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_no_unexpected_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


class TestProvenance:
    def test_published_and_retrieved_times_are_separate_columns(self) -> None:
        columns = Base.metadata.tables["raw_documents"].columns
        assert "published_at" in columns
        assert "retrieved_at" in columns
        # Retrieval time is always known; publication time may not be stated.
        assert columns["retrieved_at"].nullable is False

    @pytest.mark.parametrize(
        "column",
        ["source_id", "url", "retrieval_method", "raw_hash", "content_hash", "retrieved_at"],
    )
    def test_provenance_columns_are_mandatory(self, column: str) -> None:
        assert Base.metadata.tables["raw_documents"].columns[column].nullable is False

    def test_document_dedup_key_is_source_plus_content_hash(self) -> None:
        table = Base.metadata.tables["raw_documents"]
        uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
        assert any(
            {col.name for col in c.columns} == {"source_id", "content_hash"} for c in uniques
        )


class TestNonGoalsAreEnforcedInTheSchema:
    def test_entities_check_excludes_person(self) -> None:
        sql = _check_sql("entities")
        assert "entity_type" in sql
        assert "'person'" not in sql

    def test_source_cannot_be_enabled_without_terms_review(self) -> None:
        sql = _check_sql("sources")
        assert "terms_reviewed_at IS NOT NULL" in sql
        assert "NOT enabled" in sql

    def test_full_text_storage_requires_terms_review(self) -> None:
        assert "NOT store_full_text" in _check_sql("sources")

    def test_poll_interval_bounded_in_the_database(self) -> None:
        assert "poll_interval_seconds BETWEEN 1800 AND 3600" in _check_sql("sources")

    def test_high_severity_requires_human_review(self) -> None:
        sql = _check_sql("events")
        assert "requires_human_review" in sql
        assert "'high'" in sql and "'severe'" in sql

    def test_low_confidence_requires_human_review(self) -> None:
        assert "0.60" in _check_sql("events")

    def test_events_default_to_requiring_review(self) -> None:
        column = Base.metadata.tables["events"].columns["requires_human_review"]
        assert column.default is not None
        assert column.default.arg is True

    def test_extraction_cannot_publish_an_event_without_passing_both_gates(self) -> None:
        sql = _check_sql("extractions")
        assert "schema_valid" in sql and "citation_valid" in sql

    def test_verified_evidence_must_record_how_it_was_verified(self) -> None:
        assert "verification_method IS NOT NULL" in _check_sql("event_evidence")

    def test_review_decision_requires_a_reason(self) -> None:
        assert Base.metadata.tables["review_decisions"].columns["reason"].nullable is False


class TestEvidenceModel:
    def test_evidence_links_an_event_to_a_document_and_a_field(self) -> None:
        columns = Base.metadata.tables["event_evidence"].columns
        for required in ("event_id", "document_id", "field_supported", "quote"):
            assert columns[required].nullable is False

    def test_evidence_document_cannot_be_deleted_out_from_under_a_citation(self) -> None:
        fks = list(Base.metadata.tables["event_evidence"].columns["document_id"].foreign_keys)
        assert fks and fks[0].ondelete == "RESTRICT"

    def test_extraction_records_prompt_and_model_version(self) -> None:
        columns = Base.metadata.tables["extractions"].columns
        assert columns["prompt_version"].nullable is False
        assert columns["model_name"].nullable is False
        for telemetry in ("input_tokens", "output_tokens", "cost_usd", "latency_ms"):
            assert telemetry in columns
