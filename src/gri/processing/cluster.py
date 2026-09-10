"""Grouping documents into events by similarity and time.

ADR-0002 D5. Two documents describe the same event when their document-level centroids
are close *and* they are near each other in time.

The time window is not decoration. In this domain the same chokepoint produces
near-identical notices months apart — "draft restriction at X" recurs seasonally — so
cosine similarity alone would happily merge this September's restriction into last
February's event.

Both constants are provisional and CI-gated: Phase 4's labelled set contains duplicate
and near-duplicate cases precisely so they can be tuned against data rather than
intuition.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from gri.logging import get_logger
from gri.models import DocumentChunk, EventCluster
from gri.processing.embed import centroid, cosine_similarity

log = get_logger(__name__)

#: Cosine similarity at or above which two documents are treated as the same event.
#:
#: Provisional, and lowered from 0.82 on measurement: with bge-small, two synthetic
#: notices about the same port closure scored 0.8196 while an unrelated grid notice
#: scored 0.4881. A 0.82 threshold would have failed to merge the genuinely related
#: pair. 0.78 sits well inside the observed gap. This is one measurement, not a tuning
#: exercise -- Phase 4 fits it against the labelled set (ADR-0002 D5).
SIMILARITY_THRESHOLD = 0.78

#: Maximum gap between a candidate document and a cluster's most recent member.
TIME_WINDOW = timedelta(days=14)

#: How many nearest clusters to pull back before applying the time gate. Small, because
#: the corpus is small and the gate is cheap to apply in Python.
CANDIDATE_LIMIT = 10


@dataclass(frozen=True)
class ClusterMatch:
    """A candidate cluster and why it did or did not match."""

    cluster_id: uuid.UUID | None
    similarity: float
    within_window: bool
    is_new: bool

    @property
    def matched(self) -> bool:
        return self.cluster_id is not None and not self.is_new


def document_centroid(session: Session, document_id: uuid.UUID) -> list[float]:
    """Mean of a document's chunk embeddings, normalised.

    Compares whole notices rather than individual passages, so two documents do not match
    merely because they share a boilerplate sentence.
    """
    vectors = list(
        session.scalars(
            select(DocumentChunk.embedding)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.embedding.is_not(None))
            .order_by(DocumentChunk.chunk_index)
        )
    )
    usable = [list(map(float, v)) for v in vectors if v is not None]
    return centroid(usable) if usable else []


def find_matching_cluster(
    session: Session,
    vector: list[float],
    published_at: datetime | None,
    *,
    threshold: float = SIMILARITY_THRESHOLD,
    window: timedelta = TIME_WINDOW,
) -> ClusterMatch:
    """Find the nearest cluster that is both similar enough and recent enough.

    The vector search is done by pgvector; the time gate is applied afterwards in Python,
    because a cluster that is similar but outside the window is a *rejection* we want to
    be able to see and log, not a row we silently never fetched.
    """
    if not vector:
        return ClusterMatch(cluster_id=None, similarity=0.0, within_window=False, is_new=True)

    candidates = list(
        session.scalars(
            select(EventCluster)
            .where(EventCluster.centroid.is_not(None))
            .order_by(EventCluster.centroid.cosine_distance(vector))
            .limit(CANDIDATE_LIMIT)
        )
    )

    reference = published_at or datetime.now(UTC)
    best: tuple[float, EventCluster] | None = None

    for candidate in candidates:
        if candidate.centroid is None:
            continue
        similarity = cosine_similarity(vector, list(map(float, candidate.centroid)))
        if similarity < threshold:
            continue

        last_seen = candidate.last_updated_at or candidate.first_seen_at
        if last_seen is not None and abs(reference - last_seen) > window:
            log.info(
                "cluster_rejected_out_of_window",
                cluster_id=str(candidate.id),
                similarity=round(similarity, 4),
                gap_days=abs((reference - last_seen).days),
            )
            continue

        if best is None or similarity > best[0]:
            best = (similarity, candidate)

    if best is None:
        return ClusterMatch(cluster_id=None, similarity=0.0, within_window=False, is_new=True)

    similarity, cluster = best
    return ClusterMatch(
        cluster_id=cluster.id, similarity=similarity, within_window=True, is_new=False
    )


def assign_to_cluster(
    session: Session,
    vector: list[float],
    published_at: datetime | None,
    *,
    label: str | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
    window: timedelta = TIME_WINDOW,
) -> tuple[EventCluster, ClusterMatch]:
    """Attach a document to the best matching cluster, or start a new one."""
    match = find_matching_cluster(session, vector, published_at, threshold=threshold, window=window)
    now = published_at or datetime.now(UTC)

    if match.matched and match.cluster_id is not None:
        cluster = session.get(EventCluster, match.cluster_id)
        if cluster is not None:
            cluster.member_count += 1
            cluster.last_updated_at = max(cluster.last_updated_at or now, now)
            # Move the centroid towards the new member, weighted by how many documents
            # the cluster already holds, so one outlier cannot drag it.
            if cluster.centroid is not None and vector:
                weight = 1.0 / cluster.member_count
                existing = list(map(float, cluster.centroid))
                cluster.centroid = centroid(
                    [[v * (1 - weight) for v in existing], [v * weight for v in vector]]
                )
            session.flush()
            log.info(
                "cluster_matched",
                cluster_id=str(cluster.id),
                similarity=round(match.similarity, 4),
                members=cluster.member_count,
            )
            return cluster, match

    cluster = EventCluster(
        label=label,
        centroid=vector or None,
        similarity_threshold=threshold,
        member_count=1,
        first_seen_at=now,
        last_updated_at=now,
    )
    session.add(cluster)
    session.flush()
    log.info("cluster_created", cluster_id=str(cluster.id))
    return cluster, ClusterMatch(
        cluster_id=cluster.id, similarity=0.0, within_window=True, is_new=True
    )
