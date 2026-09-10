"""SQLAlchemy models.

Importing this package registers every mapped class, which both resolves the string
forward references between modules and gives Alembic autogenerate a complete
``Base.metadata`` to diff against.
"""

from __future__ import annotations

from gri.models.base import Base
from gri.models.document import EMBEDDING_DIM, DocumentChunk, RawDocument
from gri.models.evaluation import (
    EvaluationDataset,
    EvaluationExample,
    EvaluationResult,
    EvaluationRun,
)
from gri.models.event import (
    Entity,
    Event,
    EventActor,
    EventCluster,
    EventDocument,
    EventEvidence,
    EventLocation,
    EventSector,
    ReviewDecision,
)
from gri.models.llmops import Extraction, Prompt
from gri.models.source import IngestionRun, Source

__all__ = [
    "EMBEDDING_DIM",
    "Base",
    "DocumentChunk",
    "Entity",
    "EvaluationDataset",
    "EvaluationExample",
    "EvaluationResult",
    "EvaluationRun",
    "Event",
    "EventActor",
    "EventCluster",
    "EventDocument",
    "EventEvidence",
    "EventLocation",
    "EventSector",
    "Extraction",
    "IngestionRun",
    "Prompt",
    "RawDocument",
    "ReviewDecision",
    "Source",
]
