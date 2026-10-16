import pytest
from fakes import FakePolicy, FakeRuntime, settings

from phi_framework import constants as c
from phi_framework.apply import Application

LIVE_FP = "fp-live"


def plan(iid, **kw):
    p = {"application_plan_id": f"PLAN-{iid}", "review_batch_id": "B", "review_item_id": iid,
         "action_type": c.SET_PHI_TAG, "catalog_name": "dev_cat", "schema_name": "s", "table_name": "t",
         "table_type": "MANAGED", "column_name": iid, "column_fingerprint": LIVE_FP, "approved_tag_value": "name",
         "reviewed_by": "r@example.com", "rendered_ddl": f"DDL {iid}"}
    p.update(kw)
    return p


def live_rows(_):
    return [
        {"table_schema": "s", "table_name": "t", "column_name": col, "table_type": ttype, "column_fingerprint": LIVE_FP}
        for col, ttype in [("ok", "MANAGED"), ("typ", "MATERIALIZED_VIEW"), ("fp", "MANAGED"), ("val", "MANAGED")]
    ]


def test_precheck_rejects_every_unsafe_case():
    rt = FakeRuntime(settings(), rules=[("information_schema.columns", live_rows)])
    app = Application(rt)
    plans = [
        plan("ok"),
        plan("env", catalog_name="prod_cat"),
        plan("missing"),
        plan("typ"),
        plan("fp", column_fingerprint="stale"),
        plan("val", approved_tag_value="retired"),
    ]
    errors = {}
    app._precheck(plans, FakePolicy({"name"}), errors)
    assert "ok" not in errors
    assert errors["env"].startswith("PermissionError")
    assert errors["missing"].startswith("LookupError")
    assert errors["typ"].startswith("ValueError: object type")
    assert errors["fp"].startswith("ValueError: column metadata changed")
    assert errors["val"].startswith("ValueError") and "no longer an allowed value" in errors["val"]


def _run(monkeypatch, s, plans, failed_count, sql_error_for=()):
    rules = [
        ("FROM `dev_gov`.`data_governance`.`classification_application_plan` p", plans),
        ("information_schema.columns", live_rows),
        ("information_schema.column_tags",
         lambda _: [{"schema_name": "s", "table_name": "t", "column_name": "ok", "tag_value": "name"}]),
        ("count(*) AS n", [{"n": failed_count}]),
    ]
    rt = FakeRuntime(s, rules=rules, sql_error_for=set(sql_error_for))
    monkeypatch.setattr("phi_framework.apply.TagPolicy", lambda *a, **k: FakePolicy({"name"}))
    monkeypatch.setattr("phi_framework.apply.close_completed_batches", lambda rt: None)
    return rt, Application(rt)


def test_failure_raises_and_records(monkeypatch):
    rt, app = _run(monkeypatch, settings(), [plan("ok"), plan("fp", column_fingerprint="stale")], failed_count=1)
    with pytest.raises(RuntimeError):
        app.run()
    results = {r["review_item_id"]: r["status"] for r in rt.views["phi_apply_results"]}
    assert results == {"ok": c.TAG_VERIFIED, "fp": c.APPLICATION_FAILED}
    assert "DDL fp" not in rt.statements  # never executed after a failed precheck


def test_retry_attempt_with_nothing_left_still_fails(monkeypatch):
    # A task retry reuses the run id: no plans remain, but failures recorded earlier in the run must fail it.
    _, app = _run(monkeypatch, settings(), [], failed_count=2)
    with pytest.raises(RuntimeError):
        app.run()


def test_retry_failed_selects_failed_items(monkeypatch):
    rt, app = _run(monkeypatch, settings(retry_failed=True), [], failed_count=0)
    app.run()
    query = next(s for s in rt.statements if "classification_application_plan` p" in s)
    assert "'APPLICATION_FAILED'" in query


def test_verified_set_supersedes_attestations(monkeypatch):
    rt, app = _run(monkeypatch, settings(), [plan("ok")], failed_count=0)
    app.run()
    assert rt.views["phi_apply_cols"][0]["column_name"] == "ok"
    assert any("classification_attestation" in s and "superseded_at = current_timestamp()" in s for s in rt.statements)
