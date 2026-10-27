"""Pure decision-validation rules (no Spark), applied by ingestion to every App submission."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from phi_framework import constants as c

# Roles a reviewer can act in; recorded on DECISION_RECEIVED events.
ACTING_STEWARD = "STEWARD"
ACTING_GOVERNANCE_OWNER = "GOVERNANCE_OWNER"
ACTING_DATA_GOVERNANCE = "DATA_GOVERNANCE"


@dataclass(frozen=True)
class ItemContext:
    review_item_id: str
    review_batch_id: str
    status: str
    suggested_tag_value: str | None
    batch_created_at: datetime  # naive UTC
    steward_group: str  # one user email or one group name
    governance_owner: str  # one user email or one group name


@dataclass(frozen=True)
class Submission:
    decision: str | None
    corrected_tag_value: str | None
    steward_comment: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None  # naive UTC, captured by the App server
    submission_type: str  # REVIEW or PRIVACY
    review_batch_id: str
    suggested_tag_value_seen: str | None = None


@dataclass(frozen=True)
class Outcome:
    ok: bool
    errors: tuple[str, ...]
    decision: str | None = None
    approved_value: str | None = None  # value for SET_PHI_TAG
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    comment: str | None = None
    corrected_tag_value: str | None = None
    acting_as: str | None = None


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def validate_decision(
    item: ItemContext,
    sub: Submission,
    is_allowed_value: Callable[[str | None], bool],
    includes: Callable[[str | None, str], bool],
    governance_group: str,
    now_utc: datetime,
) -> Outcome:
    """Validate one submission. ``includes(user, value)`` is true when the user is the user, or a
    member of the group, named by a scope reviewer value (see GroupDirectory.includes)."""
    errors: list[str] = []
    decision = _clean(sub.decision)
    decision = decision.upper() if decision else None
    corrected = _clean(sub.corrected_tag_value)
    comment = _clean(sub.steward_comment)
    reviewer = _clean(sub.reviewed_by)
    reviewer = reviewer.lower() if reviewer else None
    privacy_outcome = item.status == c.NEEDS_PRIVACY_REVIEW

    if sub.review_batch_id != item.review_batch_id:
        errors.append(f"Submission batch {sub.review_batch_id} does not match the item's batch {item.review_batch_id}")
    if decision not in c.DECISIONS:
        errors.append(f"decision must be one of {', '.join(c.DECISIONS)}")

    # Reviewer authorization: stewards review; the governance owner may review in their place.
    acting_as = None
    if not reviewer:
        errors.append("reviewed_by is required")
    elif privacy_outcome:
        if includes(reviewer, governance_group):
            acting_as = ACTING_DATA_GOVERNANCE
        elif includes(reviewer, item.governance_owner):
            acting_as = ACTING_GOVERNANCE_OWNER
        else:
            errors.append(
                f"{reviewer} must be in {governance_group} or be the scope's governance owner to record a Privacy outcome"
            )
    elif includes(reviewer, item.steward_group):
        acting_as = ACTING_STEWARD
    elif includes(reviewer, item.governance_owner):
        acting_as = ACTING_GOVERNANCE_OWNER
    else:
        errors.append(f"{reviewer} is not a reviewer for this scope (steward group or governance owner)")

    # Decision time (captured by the App server)
    reviewed_at = sub.reviewed_at
    if reviewed_at is None:
        errors.append("reviewed_at is required")
    else:
        if reviewed_at > now_utc + timedelta(minutes=5):
            errors.append("reviewed_at is in the future")
        if reviewed_at < item.batch_created_at.replace(second=0, microsecond=0):
            errors.append("reviewed_at is earlier than the batch was created")

    approved = None
    if decision == c.APPROVE_SUGGESTION:
        if not is_allowed_value(item.suggested_tag_value):
            errors.append("APPROVE_SUGGESTION requires a suggested value that is an allowed governed-tag value")
        elif _clean(sub.suggested_tag_value_seen) != _clean(item.suggested_tag_value):
            errors.append("The suggested value changed after the item was opened for review; review it again")
        approved = item.suggested_tag_value
        if corrected:
            errors.append("corrected_tag_value must be empty for APPROVE_SUGGESTION")
    elif decision == c.CORRECT_CLASSIFICATION:
        if not is_allowed_value(corrected):
            errors.append(f"corrected_tag_value {corrected!r} is not an allowed governed-tag value")
        approved = corrected
    elif decision == c.CONFIRM_NOT_PHI:
        if not comment:
            errors.append("CONFIRM_NOT_PHI requires a comment explaining why the column is not PHI")
        if corrected:
            errors.append("corrected_tag_value must be empty for CONFIRM_NOT_PHI")
    elif decision == c.REQUEST_PRIVACY_REVIEW:
        if privacy_outcome:
            errors.append("REQUEST_PRIVACY_REVIEW is not a valid outcome for an item already in Privacy review")
        if not comment:
            errors.append("REQUEST_PRIVACY_REVIEW requires a comment explaining the question")

    return Outcome(
        ok=not errors,
        errors=tuple(errors),
        decision=decision,
        approved_value=approved,
        reviewed_by=reviewer,
        reviewed_at=reviewed_at,
        comment=comment,
        corrected_tag_value=corrected,
        acting_as=acting_as,
    )


def submitted_differs(recorded: dict, sub: Submission) -> bool:
    """True when a submission changes the decision fields already recorded on an accepted item."""

    def norm(v, upper=False):
        v = _clean(v)
        return (v.upper() if upper else v) if v else None

    if norm(sub.decision, True) is None and not any(
        norm(x) for x in (sub.corrected_tag_value, sub.steward_comment)
    ):
        return False  # empty row: nothing submitted
    return (
        norm(sub.decision, True) != norm(recorded.get("decision"), True)
        or norm(sub.corrected_tag_value) != norm(recorded.get("corrected_tag_value"))
        or norm(sub.steward_comment) != norm(recorded.get("steward_comment"))
    )


def add_business_days(start_local: datetime, days: int) -> datetime:
    """Add business days (Mon-Fri). Holidays are not modeled."""
    d = start_local
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d
