"""Closed vocabularies for the domain.

These are the single source of truth for the value sets used by database CHECK
constraints (see ``gri.models``) and, from Phase 2, by the extraction schema. They are
deliberately *closed*: an extraction that does not fit is ``background``, not a new
member (CONSTRAINTS.md NG-6).
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------

#: Closed event taxonomy. CONSTRAINTS.md Section 4.
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "port_disruption",
        "canal_transit_restriction",
        "strait_transit_restriction",
        "vessel_incident",
        "piracy_or_armed_robbery_report",
        "maritime_security_advisory",
        "pipeline_disruption",
        "refinery_outage",
        "lng_terminal_disruption",
        "power_grid_disruption",
        "energy_export_restriction",
        "subsea_cable_damage",
        "labor_action_transport",
        "customs_or_border_delay",
        "supply_chain_shortage_notice",
        "sanctions_or_regulatory_notice",
        "other_in_scope",
    }
)

#: Ordered least-to-most severe. Definitions live in CONSTRAINTS.md Section 5 and are
#: assigned only from cited evidence -- never inferred from tone or prominence.
SEVERITY_LEVELS: Final[tuple[str, ...]] = (
    "informational",
    "low",
    "moderate",
    "high",
    "severe",
)

#: Severities that always require a human, regardless of confidence.
#: CONSTRAINTS.md Section 6, trigger 4.
SEVERITY_ALWAYS_REVIEW: Final[frozenset[str]] = frozenset({"high", "severe"})

#: Affected sectors. Kept coarse on purpose: the niche is the differentiator, not a deep
#: sector ontology.
SECTORS: Final[frozenset[str]] = frozenset(
    {
        "maritime_shipping",
        "ports_terminals",
        "oil",
        "natural_gas",
        "lng",
        "refined_products",
        "electricity",
        "rail_freight",
        "road_freight",
        "air_cargo",
        "telecoms_subsea",
    }
)

#: Lifecycle of an event record.
EVENT_STATUSES: Final[frozenset[str]] = frozenset({"active", "resolved", "superseded", "merged"})

# --------------------------------------------------------------------------------------
# Entities -- NG-4 enforcement point
# --------------------------------------------------------------------------------------

#: Allowed actor types. ``person`` is absent BY DESIGN and must never be added.
#:
#: This is the schema-level enforcement of NG-4 ("no individual targeting, no threat
#: scoring of people"). Because a person cannot be stored as an entity, a person cannot
#: be linked to an event, scored, ranked, or served through the API. Adding ``person``
#: here would silently defeat that guarantee across the whole system.
ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {
        "state",
        "government_body",
        "regulatory_authority",
        "port_authority",
        "company",
        "industry_group",
        "infrastructure",  # a pipeline, terminal, cable, refinery
        "vessel",  # a hull, as named in an official notice -- not its operator's staff
        "vessel_class",
        "armed_group",  # organisation-level only, as named by the source
        "other_organisation",
    }
)

#: Guard against a well-meaning future edit re-introducing person-level records.
_FORBIDDEN_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {"person", "individual", "human", "crew_member", "official"}
)
assert not (ENTITY_TYPES & _FORBIDDEN_ENTITY_TYPES), (
    "NG-4 violation: person-level entity types are forbidden. See CONSTRAINTS.md."
)

#: How an entity relates to an event.
ACTOR_ROLES: Final[frozenset[str]] = frozenset(
    {
        "operator",
        "owner",
        "authority_issuing_notice",
        "affected_party",
        "claimed_responsible",  # only as attributed by the source, never as our finding
        "mentioned",
    }
)

# --------------------------------------------------------------------------------------
# Locations -- fixed features only, never a tracked position
# --------------------------------------------------------------------------------------

LOCATION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "port",
        "terminal",
        "canal",
        "strait",
        "shipping_lane",
        "pipeline",
        "refinery",
        "grid_region",
        "subsea_cable_segment",
        "eez",
        "country",
        "admin_area",
        "sea_area",
    }
)

#: Coarseness of the coordinate, so the dashboard can refuse to imply precision it does
#: not have. There is deliberately no "exact position" level.
LOCATION_PRECISIONS: Final[frozenset[str]] = frozenset(
    {"country", "admin_area", "sea_area", "facility", "approximate"}
)

# --------------------------------------------------------------------------------------
# Documents, ingestion, review
# --------------------------------------------------------------------------------------

RETRIEVAL_METHODS: Final[frozenset[str]] = frozenset({"rss", "api", "notice_html", "bulk_file"})

#: Classification of an incoming document against existing events (ADR-0001 D7).
DOCUMENT_RELATIONS: Final[frozenset[str]] = frozenset(
    {"new_event", "update", "duplicate", "background"}
)

INGESTION_RUN_STATUSES: Final[frozenset[str]] = frozenset(
    {"running", "succeeded", "failed", "skipped"}
)

REVIEW_STATUSES: Final[frozenset[str]] = frozenset(
    {"pending", "approved", "rejected", "edited", "not_required"}
)

REVIEW_DECISIONS: Final[frozenset[str]] = frozenset({"approve", "reject", "edit"})

#: Outcome of an extraction attempt. ``abstained`` is a first-class success, not an
#: error: CONSTRAINTS.md Section 6.
EXTRACTION_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"extracted", "abstained", "schema_invalid", "citation_invalid", "error"}
)

#: Labels used by the Phase 4 evaluation set.
EVAL_LABEL_TYPES: Final[frozenset[str]] = DOCUMENT_RELATIONS | frozenset({"insufficient_evidence"})


def sorted_values(values: frozenset[str] | tuple[str, ...]) -> list[str]:
    """Deterministic ordering, so generated CHECK constraints are stable across runs."""
    return sorted(values)
