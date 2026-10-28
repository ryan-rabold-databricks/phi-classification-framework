from fakes import FakeRuntime, settings

from phi_framework import constants as c
from phi_framework.ingest import Ingestion

SUBS = [
    {"submission_id": "SUB-1", "submission_set_id": "SET-1", "submission_type": c.SUB_REVIEW, "review_batch_id": "BATCH-1",
     "review_item_id": "ITEM-1", "decision": c.CONFIRM_NOT_PHI, "reviewed_by": "s@example.com"},
    {"submission_id": "SUB-2", "submission_set_id": "SET-1", "submission_type": c.SUB_REVIEW, "review_batch_id": "BATCH-2",
     "review_item_id": "ITEM-2", "decision": c.CONFIRM_NOT_PHI, "reviewed_by": "s@example.com"},
]


def _ingestion(set_row):
    s = settings(process_name="dev_phi_decision_ingestion", allowed_values_override="name")
    rt = FakeRuntime(s, rules=[
        ("count(DISTINCT review_batch_id) AS batches", [set_row]),
        ("WHERE submission_set_id = 'SET-1' ORDER BY review_item_id", SUBS),
    ])
    return rt, Ingestion(rt)


def test_set_spanning_batches_is_rejected_whole_and_marked_processed():
    rt, ing = _ingestion({"submission_set_id": "SET-1", "submission_type": c.SUB_REVIEW, "review_batch_id": "BATCH-1",
                          "n": 2, "batches": 2, "types": 1})
    ing.run()
    rejected = [e for e in rt.events if e["event_type"] == c.EV_VALIDATION_FAILED]
    assert {e["review_item_id"] for e in rejected} == {"ITEM-1", "ITEM-2"}
    assert all("mixes batches" in " ".join(e["details"]["errors"]) for e in rejected)
    processed = [e for e in rt.events if e["event_type"] == c.EV_SUBMISSION_PROCESSED]
    assert len(processed) == 1 and processed[0]["details"]["rejected_set"] == 2 and processed[0]["discriminator"] == "SET-1"
    # Nothing was validated: no plans, attestations, or item updates were written.
    assert not any(n in rt.views for n in ("phi_plans", "phi_atts", "phi_item_updates"))


def test_set_mixing_submission_types_is_rejected():
    rt, ing = _ingestion({"submission_set_id": "SET-1", "submission_type": c.SUB_REVIEW, "review_batch_id": "BATCH-1",
                          "n": 2, "batches": 1, "types": 2})
    ing.run()
    assert sum(e["event_type"] == c.EV_VALIDATION_FAILED for e in rt.events) == 2
