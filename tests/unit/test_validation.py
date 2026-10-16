from datetime import datetime

import pytest

from phi_framework import constants as c
from phi_framework.validation import (
    ItemContext,
    Submission,
    add_business_days,
    parse_reviewed_at,
    submitted_differs,
    validate_decision,
)

ALLOWED = {"name", "mrn", "ssn"}
STEWARDS = {"clinical_stewards": {"steward@example.com"}, "governance": {"gov@example.com"}}
NOW = datetime(2026, 10, 16, 12, 0)


def is_allowed(v):
    return v in ALLOWED


def is_member(user, group):
    return bool(user) and user.lower() in STEWARDS.get(group, set())


def item(status=c.PENDING_STEWARD_REVIEW, suggested="name"):
    return ItemContext("ITEM-1", "BATCH-1", status, suggested, datetime(2026, 10, 12, 8, 0), "clinical_stewards")


def sub(decision, corrected=None, comment=None, by="steward@example.com", at="2026-10-14 11:40 -04:00", wtype=c.WB_REVIEW, batch="BATCH-1"):
    return Submission(decision, corrected, comment, by, at, wtype, batch)


def check(it, s):
    return validate_decision(it, s, is_allowed, is_member, "governance", NOW)


def test_reviewed_at_parsing_converts_to_utc():
    assert parse_reviewed_at("2026-10-14 11:40 -04:00") == datetime(2026, 10, 14, 15, 40)
    assert parse_reviewed_at("2026-10-14 11:40 +05:30") == datetime(2026, 10, 14, 6, 10)
    for bad in ["2026-10-14 11:40", "10/14/2026 11:40 -04:00", "2026-10-14T11:40-04:00", "2026-10-14 11:40 -15:00", None]:
        with pytest.raises(ValueError):
            parse_reviewed_at(bad)


def test_approve_suggestion_ok():
    out = check(item(), sub(c.APPROVE_SUGGESTION))
    assert out.ok and out.approved_value == "name" and out.reviewed_at == datetime(2026, 10, 14, 15, 40)


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


def test_reviewer_must_be_in_steward_group():
    out = check(item(), sub(c.APPROVE_SUGGESTION, by="someone@example.com"))
    assert not out.ok and any("steward group" in e for e in out.errors)


def test_time_rules():
    assert not check(item(), sub(c.APPROVE_SUGGESTION, at="2026-10-30 11:40 -04:00")).ok  # future
    assert not check(item(), sub(c.APPROVE_SUGGESTION, at="2026-10-01 11:40 -04:00")).ok  # before batch


def test_privacy_outcome_rules():
    it = item(status=c.NEEDS_PRIVACY_REVIEW)
    assert check(it, sub(c.CONFIRM_NOT_PHI, comment="Privacy: not PHI", by="gov@example.com", wtype=c.WB_PRIVACY)).ok
    assert not check(it, sub(c.CONFIRM_NOT_PHI, comment="x", by="steward@example.com", wtype=c.WB_PRIVACY)).ok
    assert not check(it, sub(c.REQUEST_PRIVACY_REVIEW, comment="again", by="gov@example.com", wtype=c.WB_PRIVACY)).ok


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
