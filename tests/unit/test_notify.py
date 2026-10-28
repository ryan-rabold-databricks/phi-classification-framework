from datetime import datetime

import pytest
from fakes import FakeRuntime, settings

from phi_framework import constants as c
from phi_framework.notify import Notifier, ReviewRequest, render

BATCH = {
    "review_batch_id": "BATCH-1",
    "scope_id": "SCOPE-CLINICAL-DEV",
    "due_at": datetime(2026, 11, 6, 22, 0),
    "catalog_name": "dev_clinical",
    "schema_name": "*",
    "steward_group": "clinical_stewards",
    "governance_owner": "owner@example.com",
    "item_count": 3,
}


class FakeGroups:
    def __init__(self, mapping, forbidden=()):
        self.mapping, self.forbidden = mapping, set(forbidden)

    def principals(self, value):
        if value in self.forbidden:
            raise PermissionError("Groups/123 is only accessible by admins")
        return frozenset(self.mapping.get(value, set()))


class FakeSmtp:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, msg):
        if self.fail:
            raise OSError("relay refused")
        self.sent.append(msg)

    def quit(self):
        pass


def notifier(batches, groups, smtp, **overrides):
    base = {"process_name": "dev_phi_classification_scan", "smtp_host": "smtp.example.com",
            "smtp_sender": "phi-review@example.com"}
    s = settings(**{**base, **overrides})
    rt = FakeRuntime(s, rules=[("FROM `dev_gov`.`data_governance`.`classification_review_batch` b", batches)])
    return rt, Notifier(rt, groups=groups, smtp_factory=lambda: smtp)


def test_sends_to_steward_group_members_and_copies_governance_owner():
    smtp = FakeSmtp()
    groups = FakeGroups({"clinical_stewards": {"a@example.com", "b@example.com", "owner@example.com"},
                         "owner@example.com": {"owner@example.com"}})
    rt, n = notifier([BATCH], groups, smtp)
    n.run()
    msg = smtp.sent[0]
    assert msg["To"] == "a@example.com, b@example.com, owner@example.com"
    assert msg["Cc"] is None  # the owner is already a recipient
    assert "BATCH-1" in msg["Subject"]
    assert [e["event_type"] for e in rt.events] == [c.EV_REVIEW_REQUEST_SENT]


def test_owner_only_scope_and_unresolvable_reviewers_do_not_fail_the_scan():
    smtp = FakeSmtp()
    groups = FakeGroups({"owner@example.com": {"owner@example.com"}})
    rt, n = notifier([BATCH, {**BATCH, "review_batch_id": "BATCH-2", "governance_owner": "nobody"}], groups, smtp)
    n.run()  # a configuration problem is recorded, not raised every week
    assert smtp.sent[0]["Cc"] == "owner@example.com"
    assert [e["event_type"] for e in rt.events] == [c.EV_REVIEW_REQUEST_SENT, c.EV_REVIEW_REQUEST_FAILED]
    assert rt.events[1]["details"]["kind"] == "RECIPIENTS"


def test_unreadable_group_is_isolated_per_batch():
    smtp = FakeSmtp()
    groups = FakeGroups({"owner@example.com": {"owner@example.com"}, "finance_stewards": {"f@example.com"}},
                        forbidden={"clinical_stewards"})
    batches = [BATCH, {**BATCH, "review_batch_id": "BATCH-2", "steward_group": "finance_stewards"}]
    rt, n = notifier(batches, groups, smtp)
    n.run()
    # The owner is still emailed for the unreadable group's batch, and the next batch is unaffected.
    assert [m["Cc"] for m in smtp.sent] == ["owner@example.com", "owner@example.com"]
    assert smtp.sent[0]["To"] == "phi-review@example.com" and smtp.sent[1]["To"] == "f@example.com"
    assert "clinical_stewards: PermissionError" in rt.events[0]["details"]["unresolved"]
    assert [e["event_type"] for e in rt.events] == [c.EV_REVIEW_REQUEST_SENT, c.EV_REVIEW_REQUEST_SENT]


def test_batches_with_unresolvable_recipients_are_not_retried():
    rt, n = notifier([], FakeGroups({}), FakeSmtp())
    n.run()
    query = rt.statements[0]
    assert "REVIEW_REQUEST_FAILED" in query and "'RECIPIENTS'" in query


def test_send_failure_is_recorded_and_fails_the_task():
    groups = FakeGroups({"clinical_stewards": {"a@example.com"}})
    rt, n = notifier([BATCH], groups, FakeSmtp(fail=True))
    with pytest.raises(RuntimeError):
        n.run()
    assert rt.events[0]["event_type"] == c.EV_REVIEW_REQUEST_FAILED and "relay refused" in rt.events[0]["details"]["error"]
    assert rt.events[0]["details"]["kind"] == "SEND"


def test_disabled_without_smtp_host():
    rt, n = notifier([BATCH], FakeGroups({}), FakeSmtp(), smtp_host="")
    n.run()
    assert rt.statements == [] and rt.events == []


def test_email_body_has_link_and_no_column_details():
    req = ReviewRequest("BATCH-1", "SCOPE-X", "dev_clinical", "clinical", "2026-11-06 22:00:00", 2,
                        ("a@example.com",), (), "https://app.example.com/?batch=BATCH-1")
    subject, body = render(req)
    assert "dev_clinical.clinical" in subject and "https://app.example.com/?batch=BATCH-1" in body
    assert "2 column(s)" in body
