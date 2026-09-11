"""The human review path: applying a decision, and what a decision may not change.

CONSTRAINTS.md Section 6 routes weak, conflicting, and high-impact records to a human.
This module is what that human's decision does to the record, and it is deliberately
narrow:

* **A decision without a reason is refused.** ``review_decisions.reason`` is NOT NULL
  because an unexplained decision is not auditable; this module refuses blank reasons
  before the database has to.
* **Everything is validated before anything is applied.** Validating and assigning in
  one pass would leave earlier fields written to the ORM object when a later one is
  rejected -- a half-applied edit with no decision row behind it.
* **A decision cannot override a database guarantee.** Approving a ``severe`` or
  low-confidence record records the approval, but the review flag stays set, because
  ``ck_events_high_severity_requires_review`` and
  ``ck_events_low_confidence_requires_review`` say so. The result says why, rather than
  letting the approval appear to have done more than it did.
* **The summary is not editable.** It is assembled from cited evidence (NG-5). A
  reviewer who thinks it is wrong should reject the record, not rewrite it into prose
  that no source supports.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gri.models import Event, ReviewDecision
from gri.processing.confidence import REVIEW_THRESHOLD, flag_must_stay
from gri.taxonomy import EVENT_TYPES, REVIEW_DECISIONS, SEVERITY_ALWAYS_REVIEW, SEVERITY_LEVELS

#: Fields a reviewer may correct. ``summary`` is absent on purpose -- see module docstring.
EDITABLE_FIELDS: frozenset[str] = frozenset({"title", "event_type", "event_date", "severity"})

TITLE_MIN, TITLE_MAX = 8, 200
REVIEWER_MAX = 200


class ReviewError(ValueError):
    """A decision that cannot be applied. Nothing has been changed when this is raised."""


@dataclass
class DecisionResult:
    """What a decision did, and what it could not do."""

    decision_id: uuid.UUID
    event_id: uuid.UUID
    decision: str
    review_status: str
    requires_human_review: bool
    changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Why the review flag stayed set after an approval or edit, if it did.
    flag_kept_because: list[str] = field(default_factory=list)


def review_reasons(event: Event) -> list[str]:
    """Why a record needs, or needed, a human -- derived from its current fields.

    The pipeline's assessment reasons are not persisted, but the triggers that can hold
    a *published* record in review are recoverable from the record itself: a failed
    citation never reaches ``events`` at all.
    """
    reasons: list[str] = []
    if event.severity in SEVERITY_ALWAYS_REVIEW:
        reasons.append(
            f"severity is '{event.severity}': high-impact claims always require review"
            " (CONSTRAINTS.md Section 6, trigger 4)"
        )
    if event.confidence is not None and float(event.confidence) < REVIEW_THRESHOLD:
        reasons.append(
            f"confidence {float(event.confidence):.2f} is below the {REVIEW_THRESHOLD}"
            " review threshold (trigger 1)"
        )
    if not reasons and event.requires_human_review:
        reasons.append("flagged for review by the pipeline")
    return reasons


def _validate_edits(event: Event, edits: Mapping[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Coerce and check every edit. Returns ``{field: (before, after)}`` for real changes.

    Pure: touches nothing on ``event``. Raises :class:`ReviewError` on the first problem.
    """
    unknown = set(edits) - EDITABLE_FIELDS
    if unknown:
        extra = (
            " The summary is not editable; reject the record instead."
            if "summary" in unknown
            else ""
        )
        raise ReviewError(
            f"cannot edit {sorted(unknown)}; editable fields are {sorted(EDITABLE_FIELDS)}.{extra}"
        )

    changes: dict[str, tuple[Any, Any]] = {}

    for name, raw in edits.items():
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # An empty form field means "leave unchanged", not "blank it out".
            continue

        if name == "title":
            value: Any = str(raw).strip()
            if not TITLE_MIN <= len(value) <= TITLE_MAX:
                raise ReviewError(f"title must be {TITLE_MIN}-{TITLE_MAX} characters")
        elif name == "event_type":
            value = str(raw).strip()
            if value not in EVENT_TYPES:
                raise ReviewError(f"event_type {value!r} is not in the closed taxonomy")
        elif name == "severity":
            value = str(raw).strip()
            if value not in SEVERITY_LEVELS:
                raise ReviewError(f"severity must be one of {list(SEVERITY_LEVELS)}")
        elif name == "event_date":
            if isinstance(raw, date):
                value = raw
            else:
                try:
                    value = date.fromisoformat(str(raw).strip())
                except ValueError as exc:
                    raise ReviewError("event_date must be an ISO date, e.g. 2026-09-01") from exc
        else:  # pragma: no cover - guarded by the unknown-field check above
            raise ReviewError(f"cannot edit {name!r}")

        before = getattr(event, name)
        if before != value:
            changes[name] = (before, value)

    return changes


def apply_decision(
    session: Session,
    event: Event,
    *,
    reviewer: str,
    decision: str,
    reason: str,
    edits: Mapping[str, Any] | None = None,
    decided_at: datetime | None = None,
) -> DecisionResult:
    """Record a human decision and update the record's review state.

    Rejection never clears the review flag: a record a human rejected must not reappear
    in the feed or a briefing. Approval and edit clear it only where the database allows.
    """
    reviewer = (reviewer or "").strip()
    reason = (reason or "").strip()

    if decision not in REVIEW_DECISIONS:
        raise ReviewError(f"decision must be one of {sorted(REVIEW_DECISIONS)}")
    if not reviewer:
        raise ReviewError("reviewer must not be blank: a decision has to be attributable")
    if len(reviewer) > REVIEWER_MAX:
        raise ReviewError(f"reviewer must be at most {REVIEWER_MAX} characters")
    if not reason:
        raise ReviewError("reason must not be blank: a decision without one is not auditable")

    changes: dict[str, tuple[Any, Any]] = {}
    if decision == "edit":
        changes = _validate_edits(event, edits or {})
        if not changes:
            raise ReviewError("an edit must change at least one field; use approve instead")
    elif edits:
        raise ReviewError("edits are only accepted with decision 'edit'")

    decided_at = decided_at or datetime.now(UTC)

    # -- Everything is validated. From here on, apply. ------------------------------------
    for name, (_before, after) in changes.items():
        setattr(event, name, after)
    if "severity" in changes:
        # The model's rationale argued for the old level; keep the record honest about
        # who set the new one and why.
        event.severity_rationale = (
            f"Revised by reviewer {reviewer} on {decided_at.date().isoformat()}: {reason}"
        )

    kept: list[str] = []
    if decision == "reject":
        event.review_status = "rejected"
        event.requires_human_review = True
    else:
        event.review_status = "approved" if decision == "approve" else "edited"
        confidence = float(event.confidence) if event.confidence is not None else None
        kept = flag_must_stay(event.severity, confidence)
        event.requires_human_review = bool(kept)

    event.last_updated_at = decided_at

    serialisable = {
        name: {"before": _plain(before), "after": _plain(after)}
        for name, (before, after) in changes.items()
    }
    row = ReviewDecision(
        event_id=event.event_id,
        reviewer=reviewer,
        decision=decision,
        reason=reason,
        changes=serialisable,
        decided_at=decided_at,
    )
    session.add(row)
    session.flush()

    return DecisionResult(
        decision_id=row.id,
        event_id=event.event_id,
        decision=decision,
        review_status=event.review_status,
        requires_human_review=event.requires_human_review,
        changes=serialisable,
        flag_kept_because=kept,
    )


def decision_history(session: Session, event_id: uuid.UUID) -> list[ReviewDecision]:
    """Every decision on a record, oldest first. Append-only by design."""
    return list(
        session.scalars(
            select(ReviewDecision)
            .where(ReviewDecision.event_id == event_id)
            .order_by(ReviewDecision.decided_at, ReviewDecision.created_at)
        )
    )


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value
