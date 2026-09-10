"""canonicalise check constraint names

The naming convention in ``gri.models.base`` renders CHECK constraints as
``ck_%(table_name)s_%(constraint_name)s``. The initial migration passed names that
*already* carried a ``ck_<table>_`` prefix, so every explicitly named CHECK ended up
doubled -- ``ck_sources_ck_sources_enabled_requires_terms_review`` -- and two were long
enough that SQLAlchemy truncated them and appended a hash.

That was cosmetic until it wasn't: a schema built from ``Base.metadata`` (what the tests
do) and a schema built from migrations (what production does) disagreed on the names, so
any future ``op.drop_constraint`` by model name would fail against a real database.

This renames them to the canonical form. Renaming a constraint does not revalidate it, so
this is metadata-only and cheap even on a large table.

Revision ID: d31f7a4b90c2
Revises: ce4c9dc8c0e8
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d31f7a4b90c2"
down_revision: str | None = "ce4c9dc8c0e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, old name, new name). The two entries whose old name ends in a hash were
#: truncated at Postgres's 63-character identifier limit; the canonical names fit.
RENAMES: tuple[tuple[str, str, str], ...] = (
    (
        "document_chunks",
        "ck_document_chunks_ck_document_chunks_embedding_records_model",
        "ck_document_chunks_embedding_records_model",
    ),
    (
        "document_chunks",
        "ck_document_chunks_ck_document_chunks_offsets_non_negative",
        "ck_document_chunks_offsets_non_negative",
    ),
    (
        "document_chunks",
        "ck_document_chunks_ck_document_chunks_offsets_ordered",
        "ck_document_chunks_offsets_ordered",
    ),
    (
        "event_evidence",
        "ck_event_evidence_ck_event_evidence_offsets_ordered",
        "ck_event_evidence_offsets_ordered",
    ),
    (
        "event_evidence",
        "ck_event_evidence_ck_event_evidence_quote_not_blank",
        "ck_event_evidence_quote_not_blank",
    ),
    (
        "event_evidence",
        "ck_event_evidence_ck_event_evidence_verified_records_method",
        "ck_event_evidence_verified_records_method",
    ),
    (
        "event_locations",
        "ck_event_locations_ck_event_locations_country_code_is_i_c988",
        "ck_event_locations_country_code_is_iso_alpha2",
    ),
    (
        "event_locations",
        "ck_event_locations_ck_event_locations_latitude_range",
        "ck_event_locations_latitude_range",
    ),
    (
        "event_locations",
        "ck_event_locations_ck_event_locations_longitude_range",
        "ck_event_locations_longitude_range",
    ),
    (
        "events",
        "ck_events_ck_events_confidence_in_unit_interval",
        "ck_events_confidence_in_unit_interval",
    ),
    (
        "events",
        "ck_events_ck_events_high_severity_requires_review",
        "ck_events_high_severity_requires_review",
    ),
    (
        "events",
        "ck_events_ck_events_low_confidence_requires_review",
        "ck_events_low_confidence_requires_review",
    ),
    (
        "extractions",
        "ck_extractions_ck_extractions_citations_non_negative",
        "ck_extractions_citations_non_negative",
    ),
    (
        "extractions",
        "ck_extractions_ck_extractions_failed_le_checked",
        "ck_extractions_failed_le_checked",
    ),
    (
        "extractions",
        "ck_extractions_ck_extractions_published_requires_valid__338a",
        "ck_extractions_published_requires_valid_schema_and_citations",
    ),
    (
        "raw_documents",
        "ck_raw_documents_ck_raw_documents_tombstoned_has_no_content",
        "ck_raw_documents_tombstoned_has_no_content",
    ),
    (
        "review_decisions",
        "ck_review_decisions_ck_review_decisions_reason_not_blank",
        "ck_review_decisions_reason_not_blank",
    ),
    (
        "sources",
        "ck_sources_ck_sources_enabled_requires_terms_review",
        "ck_sources_enabled_requires_terms_review",
    ),
    (
        "sources",
        "ck_sources_ck_sources_full_text_requires_terms_review",
        "ck_sources_full_text_requires_terms_review",
    ),
    (
        "sources",
        "ck_sources_ck_sources_poll_interval_30_to_60_min",
        "ck_sources_poll_interval_30_to_60_min",
    ),
)


def upgrade() -> None:
    for table, old, new in RENAMES:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}"')


def downgrade() -> None:
    for table, old, new in RENAMES:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{new}" TO "{old}"')
