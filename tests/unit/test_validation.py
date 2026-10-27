from datetime import datetime

from phi_framework import constants as c
from phi_framework.validation import (
    ACTING_DATA_GOVERNANCE,
    ACTING_GOVERNANCE_OWNER,
    ACTING_STEWARD,
    ItemContext,
    Submission,
    add_business_days,
    submitted_differs,
    validate_decision,
)

ALLOWED = {"name", "mrn", "ssn"}
GROUPS = {"clinical_stewards": {"steward@example.com"}, "governance": {"gov@example.com"}}
NOW = datetime(2026, 10, 16, 12, 0)
AT = datetime(2026, 10, 14, 15, 40)


def is_allowed(v):
    return v in ALLOWED


def includes(user, value):
    """Mirror of GroupDirectory.includes: a group name or a single user email."""
    if not user:
        return False
    if value in GROUPS:
        return user.lower() in GROUPS[value]
    return "@" in (value or "") and user.lower() == value.lower()


def item(status=c.PENDING_STEWARD_REVIEW, suggested="name", owner="owner@example.com", stewards="clinical_stewards"):
    return ItemContext("ITEM-1", "BATCH-1", status, suggested, datetime(2026, 10, 12, 8, 0), stewards, owner)


def sub(decision, corrected=None, comment=None, by="steward@example.com", at=AT, stype=c.SUB_REVIEW, batch="BATCH-1",
        seen="name"):
    return Submission(decision, corrected, comment, by, at, stype, batch, seen)


def check(it, s):
    return validate_decision(it, s, is_allowed, includes, "governance", NOW)


def test_approve_suggestion_ok():
    out = check(item(), sub(c.APPROVE_SUGGESTION))
    assert out.ok and out.approved_value == "name" and out.reviewed_at == AT and out.acting_as == ACTING_STEWARD


def test_approve_rejected_when_suggestion_changed_after_review_opened():
    out = check(item(suggested="mrn"), sub(c.APPROVE_SUGGESTION, seen="name"))
    assert not out.ok and any("changed" in e for e in out.errors)


def test_approve_requires_allowed_suggestion():
    assert not check(item(suggested=None), sub(c.APPROVE_SUGGESTION)).ok
    assert not check(item(suggested="bogus"), sub(c.APPROVE_SUGGESTION)).ok


def test_correct_requires_allowed_value():
    assert check(item(), sub(c.CORRECT_CLASSIFICATION, corrected="mrn")).ok
    assert not check(item(), sub(c.CORRECT_CLASSIFICATION, corrected="not_a_value")).ok


def test_not_phi_requires_comment_and_no_corrected_value():
    assert not check(item(), sub(c.CONFIRM_NOT_PHI)).ok
    assert not check(item(), sub(c.CONFIRM_NOT_PHI, corrected="name", comment="x")).ok
    assert check(item(), sub(c.CONFIRM_NOT_PHI, comment="technical code")).ok


def test_reviewer_must_be_steward_or_governance_owner():
    out = check(item(), sub(c.APPROVE_SUGGESTION, by="someone@example.com"))
    assert not out.ok and any("not a reviewer" in e for e in out.errors)


def test_governance_owner_may_review_in_place_of_steward():
    out = check(item(), sub(c.APPROVE_SUGGESTION, by="Owner@Example.com"))
    assert out.ok and out.reviewed_by == "owner@example.com" and out.acting_as == ACTING_GOVERNANCE_OWNER
    out = check(item(owner="governance"), sub(c.CONFIRM_NOT_PHI, comment="code", by="gov@example.com"))
    assert out.ok and out.acting_as == ACTING_GOVERNANCE_OWNER


def test_steward_group_may_be_a_single_email():
    it = item(stewards="steward@example.com")
    assert check(it, sub(c.APPROVE_SUGGESTION)).acting_as == ACTING_STEWARD
    assert not check(it, sub(c.APPROVE_SUGGESTION, by="other@example.com")).ok


def test_time_rules():
    assert not check(item(), sub(c.APPROVE_SUGGESTION, at=datetime(2026, 10, 30, 15, 40))).ok  # future
    assert not check(item(), sub(c.APPROVE_SUGGESTION, at=datetime(2026, 10, 1, 15, 40))).ok  # before batch
    assert not check(item(), sub(c.APPROVE_SUGGESTION, at=None)).ok


def test_privacy_outcome_rules():
    it = item(status=c.NEEDS_PRIVACY_REVIEW)
    out = check(it, sub(c.CONFIRM_NOT_PHI, comment="Privacy: not PHI", by="gov@example.com", stype=c.SUB_PRIVACY))
    assert out.ok and out.acting_as == ACTING_DATA_GOVERNANCE
    owner = check(it, sub(c.CONFIRM_NOT_PHI, comment="Privacy: not PHI", by="owner@example.com", stype=c.SUB_PRIVACY))
    assert owner.ok and owner.acting_as == ACTING_GOVERNANCE_OWNER
    assert not check(it, sub(c.CONFIRM_NOT_PHI, comment="x", by="steward@example.com", stype=c.SUB_PRIVACY)).ok
    assert not check(it, sub(c.REQUEST_PRIVACY_REVIEW, comment="again", by="gov@example.com", stype=c.SUB_PRIVACY)).ok


def test_batch_mismatch_rejected():
    assert not check(item(), sub(c.APPROVE_SUGGESTION, batch="BATCH-OTHER")).ok


def test_submitted_differs():
    recorded = {"decision": "APPROVE_SUGGESTION", "corrected_tag_value": None, "steward_comment": None}
    assert not submitted_differs(recorded, sub("approve_suggestion"))
    assert submitted_differs(recorded, sub(c.CORRECT_CLASSIFICATION, corrected="mrn"))
    assert not submitted_differs(recorded, sub(None))


def test_business_days_skip_weekends():
    friday = datetime(2026, 10, 16, 9, 0)
    assert add_business_days(friday, 1) == datetime(2026, 10, 19, 9, 0)
    assert add_business_days(friday, 10) == datetime(2026, 10, 30, 9, 0)
