"""Pure decision-validation rules (no Spark), shared by the Excel pilot and the Phase 2 App."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from phi_framework import constants as c

REVIEWED_AT_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}) ([+-])(\d{2}):(\d{2})$")


def parse_reviewed_at(raw: str | None) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM ±HH:MM' into a naive UTC datetime. Raises ValueError when invalid."""
    if raw is None:
        raise ValueError("reviewed_at is required")
    text = str(raw).strip()
    m = REVIEWED_AT_PATTERN.match(text)
    if not m:
        raise ValueError(f"reviewed_at {text!r} must be 'YYYY-MM-DD HH:MM ±HH:MM', for example 2026-10-14 11:40 -04:00")
    date_part, hh, mm, sign, oh, om = m.groups()
    if int(oh) > 14 or int(om) >= 60:
        raise ValueError(f"reviewed_at {text!r} has an invalid UTC offset")
    offset = timedelta(hours=int(oh), minutes=int(om)) * (1 if sign == "+" else -1)
    local = datetime.strptime(f"{date_part} {hh}:{mm}", "%Y-%m-%d %H:%M")
    return local.replace(tzinfo=timezone(offset)).astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ItemContext:
    review_item_id: str
    review_batch_id: str
    status: str
    suggested_tag_value: str | None
    batch_created_at: datetime  # naive UTC
    steward_group: str


@dataclass(frozen=True)
class Submission:
    decision: str | None
    corrected_tag_value: str | None
    steward_comment: str | None
    reviewed_by: str | None
    reviewed_at_raw: str | None
    submission_type: str  # REVIEW or PRIVACY
    review_batch_id: str


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


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def validate_decision(
    item: ItemContext,
    sub: Submission,
    is_allowed_value: Callable[[str | None], bool],
    is_member: Callable[[str | None, str], bool],
    governance_group: str,
    now_utc: datetime,
) -> Outcome:
    errors: list[str] = []
    decision = _clean(sub.decision)
    decision = decision.upper() if decision else None
    corrected = _clean(sub.corrected_tag_value)
    comment = _clean(sub.steward_comment)
    reviewer = _clean(sub.reviewed_by)
    reviewer = reviewer.lower() if reviewer else None
    privacy_outcome = item.status == c.NEEDS_PRIVACY_REVIEW

    if sub.review_batch_id != item.review_batch_id:
        errors.append(f"Workbook batch {sub.review_batch_id} does not match the item's batch {item.review_batch_id}")
    if decision not in c.DECISIONS:
        errors.append(f"decision must be one of {', '.join(c.DECISIONS)}")

    # Reviewer authorization
    if not reviewer:
        errors.append("reviewed_by is required")
    elif privacy_outcome:
        if not is_member(reviewer, governance_group):
            errors.append(f"reviewed_by {reviewer} must be a member of {governance_group} to record a Privacy outcome")
    elif not is_member(reviewer, item.steward_group):
        errors.append(f"reviewed_by {reviewer} is not a member of steward group {item.steward_group}")

    # Decision time
    reviewed_at = None
    try:
        reviewed_at = parse_reviewed_at(sub.reviewed_at_raw)
        if reviewed_at > now_utc + timedelta(minutes=5):
            errors.append("reviewed_at is in the future")
        if reviewed_at < item.batch_created_at.replace(second=0, microsecond=0):
            errors.append("reviewed_at is earlier than the batch was created")
    except ValueError as exc:
        errors.append(str(exc))

    approved = None
    if decision == c.APPROVE_SUGGESTION:
        if not is_allowed_value(item.suggested_tag_value):
            errors.append("APPROVE_SUGGESTION requires a suggested value that is an allowed governed-tag value")
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
