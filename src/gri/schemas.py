"""The strict extraction schema.

This is the contract the model is *constrained* to (ADR-0002 D3) — generation is
restricted to this shape by the API, so a schema-invalid response is not something we
parse defensively around.

Two design points carry the project's thesis:

* **Every claim carries its quote.** ``EvidenceQuote`` pairs a field name with the exact
  source text supporting it. A record whose quotes cannot be found in the stored document
  does not get published, whatever the model asserted.
* **Abstention is representable.** :class:`ExtractionResult` can come back with
  ``in_niche=False`` or ``abstained=True`` and no event at all. That is a success path,
  not an error path, and it is measured as one.

The vocabularies are imported from :mod:`gri.taxonomy` rather than restated, so the
schema, the database CHECK constraints, and the prompt cannot drift apart.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gri.taxonomy import (
    ACTOR_ROLES,
    ENTITY_TYPES,
    EVENT_TYPES,
    LOCATION_PRECISIONS,
    LOCATION_TYPES,
    SECTORS,
    SEVERITY_LEVELS,
    sorted_values,
)

# Literal types generated from the closed vocabularies in gri.taxonomy.
#
# Building these dynamically keeps ONE source of truth: the schema, the database CHECK
# constraints, and the prompt all read the same frozensets, so they cannot drift apart.
# The cost is that mypy cannot see a computed Literal as a type, so under TYPE_CHECKING
# these are plain `str`. Nothing is lost at runtime -- Pydantic still emits the full enum
# into the JSON schema, which is what actually constrains generation -- and the values are
# validated twice more: by Pydantic on parse, and by the database CHECK constraints.
if TYPE_CHECKING:
    EventType = str
    Severity = str
    Sector = str
    EntityType = str
    ActorRole = str
    LocationType = str
    LocationPrecision = str
else:
    EventType = Literal[tuple(sorted_values(EVENT_TYPES))]
    Severity = Literal[tuple(SEVERITY_LEVELS)]
    Sector = Literal[tuple(sorted_values(SECTORS))]
    EntityType = Literal[tuple(sorted_values(ENTITY_TYPES))]
    ActorRole = Literal[tuple(sorted_values(ACTOR_ROLES))]
    LocationType = Literal[tuple(sorted_values(LOCATION_TYPES))]
    LocationPrecision = Literal[tuple(sorted_values(LOCATION_PRECISIONS))]


class StrictModel(BaseModel):
    """Base with the settings the structured-output API needs.

    ``extra="forbid"`` becomes ``additionalProperties: false`` in the emitted JSON schema,
    which the strict-output path requires.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceQuote(StrictModel):
    """A verbatim quote from the source, and the field it supports.

    ``quote`` must be copied exactly from the document. It is checked against the stored
    text by deterministic code (ADR-0002 D4); a paraphrase will not verify, and a record
    whose quotes do not verify is not published.
    """

    field_supported: str = Field(
        description=(
            "Which field of the event this quote supports, e.g. 'event_date', 'severity',"
            " 'locations', 'summary'."
        ),
        min_length=1,
        max_length=64,
    )
    quote: str = Field(
        description=(
            "The exact text from the source document supporting this field. Copy it"
            " verbatim. Do not paraphrase, summarise, correct, or join separated passages."
        ),
        min_length=8,
        max_length=1000,
    )


class ExtractedLocation(StrictModel):
    """A fixed feature or administrative area. Never a tracked position (NG-4)."""

    name: str = Field(description="Name as given in the source.", min_length=1, max_length=200)
    location_type: LocationType = Field(description="What kind of place this is.")
    country_code: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 code in upper case, or null if not stated.",
        min_length=2,
        max_length=2,
    )
    precision: LocationPrecision = Field(
        description="How precisely the source locates this. Do not imply precision it lacks."
    )

    @field_validator("country_code")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class ExtractedActor(StrictModel):
    """An organisation, state, facility, or vessel named by the source.

    There is no person type, by design (NG-4). If the source names an individual, do not
    record them: the database cannot store a person and the extraction must not try.
    """

    name: str = Field(description="Canonical name as given.", min_length=1, max_length=200)
    entity_type: EntityType = Field(
        description=(
            "Organisation, state, facility, or vessel. Never a person. If the only actor"
            " is an individual, omit them entirely."
        )
    )
    role: ActorRole = Field(description="How this entity relates to the event.")


class ExtractedEvent(StrictModel):
    """One disruption event, as reported by one source document."""

    event_type: EventType = Field(description="Closest match from the closed taxonomy.")
    event_date: date = Field(
        description=(
            "Date the disruption occurred or takes effect, as stated by the source."
            " Not the publication date, unless the source gives no other date."
        )
    )
    title: str = Field(
        description="One short factual line naming what happened and where.",
        min_length=8,
        max_length=200,
    )
    summary: str = Field(
        description=(
            "Two to four sentences, assembled only from what the cited quotes support."
            " Attribute claims to the source. Do not add background knowledge, do not"
            " speculate about cause, and do not state consequences the source does not."
        ),
        min_length=20,
        max_length=1200,
    )
    severity: Severity = Field(
        description=(
            "Reported operational disruption only. If the source reports no impact,"
            " use 'informational'. Never infer severity from tone or prominence."
        )
    )
    severity_rationale: str = Field(
        description="Why this severity, referring to the evidence that supports it.",
        min_length=10,
        max_length=600,
    )
    locations: Annotated[list[ExtractedLocation], Field(max_length=20)] = Field(
        default_factory=list, description="Places named by the source."
    )
    actors: Annotated[list[ExtractedActor], Field(max_length=20)] = Field(
        default_factory=list, description="Organisations, states, facilities, or vessels."
    )
    affected_sectors: Annotated[list[Sector], Field(max_length=12)] = Field(
        default_factory=list, description="Sectors the source says are affected."
    )
    evidence: Annotated[list[EvidenceQuote], Field(min_length=1, max_length=25)] = Field(
        description=(
            "At least one verbatim quote. Every material field must be supported by a"
            " quote; a claim with no quote will be rejected."
        )
    )
    confidence: float = Field(
        description=(
            "0.0-1.0: how well the cited text supports this record. NOT how likely the"
            " event is to be real. Below 0.60 means weak, partial, or conflicting"
            " evidence."
        ),
        ge=0.0,
        le=1.0,
    )


class ExtractionResult(StrictModel):
    """What the model returns for one document.

    The three-way outcome is deliberate: in-niche with an event, in-niche but not
    extractable, and out of niche. Collapsing "I cannot support this" into "no event"
    would make correct abstention indistinguishable from a miss, and Phase 4 needs to
    measure them separately.
    """

    in_niche: bool = Field(
        description=(
            "True only if this document reports disruption to maritime shipping, energy"
            " production or transport, or physical supply chains. Political, financial,"
            " and general news reporting is out of niche."
        )
    )
    abstained: bool = Field(
        description=(
            "True if the document is in niche but the evidence is too weak, partial, or"
            " contradictory to support a record. Abstaining is correct behaviour and is"
            " preferred over guessing."
        )
    )
    abstention_reason: str | None = Field(
        default=None,
        description="If abstained or out of niche, one sentence on why.",
        max_length=500,
    )
    event: ExtractedEvent | None = Field(
        default=None, description="The event, or null when out of niche or abstaining."
    )

    @model_validator(mode="after")
    def _outcome_is_internally_consistent(self) -> ExtractionResult:
        """Reject results that claim one outcome and carry another.

        Constrained generation guarantees the shape, not the coherence: nothing stops the
        model from setting ``abstained=True`` and also filling in an event. Catching that
        here means the pipeline never has to guess which half to believe.
        """
        extracting = self.in_niche and not self.abstained

        if extracting and self.event is None:
            raise ValueError("in_niche and not abstained, but no event was supplied")
        if not extracting and self.event is not None:
            reason = "out of niche" if not self.in_niche else "abstaining"
            raise ValueError(f"{reason}, so event must be null")
        if not extracting and not self.abstention_reason:
            raise ValueError("abstaining or out of niche requires an abstention_reason")
        return self

    @property
    def produced_event(self) -> bool:
        return self.in_niche and not self.abstained and self.event is not None
