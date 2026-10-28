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
NOW = datetime(2026, 10, 16, 12, 0)
AT = datetime(2026, 10, 14, 15, 40)


def is_allowed(v):
    return v in ALLOWED


def item(status=c.PENDING_STEWARD_REVIEW, suggested="name", owners=("owner@example.com",),
         stewards=("steward@example.com", "steward2@example.com")):
    return ItemContext("ITEM-1", "BATCH-1", status, suggested, datetime(2026, 10, 12, 8, 0), tuple(stewards), tuple(owners))


def sub(decision, corrected=None, comment=None, by="steward@example.com", at=AT, stype=c.SUB_REVIEW, batch="BATCH-1",
        seen="name", acting=ACTING_STEWARD, via=None):
    return Submission(decision, corrected, comment, by, at, stype, batch, seen, acting, via if via is not None else by)


def check(it, s):
    return validate_decision(it, s, is_allowed, "governance", NOW)


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


def test_reviewer_must_be_on_the_scope_email_lists():
    assert check(item(), sub(c.APPROVE_SUGGESTION, by="Steward2@Example.com")).ok  # case-insensitive
    out = check(item(), sub(c.APPROVE_SUGGESTION, by="someone@example.com"))
    assert not out.ok and any("not a reviewer" in e for e in out.errors)
    assert not check(item(), sub(c.APPROVE_SUGGESTION, acting=None)).ok
    # Data Governance membership alone does not authorize review decisions.
    assert not check(item(), sub(c.APPROVE_SUGGESTION, acting=ACTING_DATA_GOVERNANCE, via="governance")).ok


def test_removed_steward_is_rejected_after_a_registry_change():
    assert not check(item(stewards=("steward2@example.com",)), sub(c.APPROVE_SUGGESTION)).ok


def test_governance_owner_may_review_in_place_of_steward():
    out = check(item(), sub(c.APPROVE_SUGGESTION, by="Owner@Example.com", acting=ACTING_GOVERNANCE_OWNER))
    assert out.ok and out.reviewed_by == "owner@example.com" and out.acting_as == ACTING_GOVERNANCE_OWNER
    # An owner is not a steward unless listed as one.
    assert not check(item(), sub(c.APPROVE_SUGGESTION, by="owner@example.com", acting=ACTING_STEWARD)).ok


def test_time_rules():
    assert not check(item(), sub(c.APPROVE_SUGGESTION, at=datetime(2026, 10, 30, 15, 40))).ok  # future
    assert not check(item(), sub(c.APPROVE_SUGGESTION, at=datetime(2026, 10, 1, 15, 40))).ok  # before batch
    assert not check(item(), sub(c.APPROVE_SUGGESTION, at=None)).ok


def test_privacy_outcome_rules():
    it = item(status=c.NEEDS_PRIVACY_REVIEW)
    gov = {"acting": ACTING_DATA_GOVERNANCE, "via": "governance", "stype": c.SUB_PRIVACY}
    out = check(it, sub(c.CONFIRM_NOT_PHI, comment="Privacy: not PHI", by="gov@example.com", **gov))
    assert out.ok and out.acting_as == ACTING_DATA_GOVERNANCE
    owner = check(it, sub(c.CONFIRM_NOT_PHI, comment="Privacy: not PHI", by="owner@example.com", stype=c.SUB_PRIVACY,
                          acting=ACTING_GOVERNANCE_OWNER))
    assert owner.ok and owner.acting_as == ACTING_GOVERNANCE_OWNER
    steward = check(it, sub(c.CONFIRM_NOT_PHI, comment="x", stype=c.SUB_PRIVACY))  # stewards cannot record Privacy
    assert not steward.ok and any("Privacy outcome" in e for e in steward.errors)
    assert not check(it, sub(c.REQUEST_PRIVACY_REVIEW, comment="again", by="gov@example.com", **gov)).ok


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
