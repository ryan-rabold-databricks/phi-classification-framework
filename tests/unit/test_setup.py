from fakes import FakeRuntime, settings

from phi_framework.ddl import table_defs
from phi_framework.setup import Setup


def _setup(current: dict[str, str]):
    rows = [{"key": f"delta.constraints.{k}", "value": v} for k, v in current.items()]
    rt = FakeRuntime(settings(), rules=[("SHOW TBLPROPERTIES", rows)])
    return rt, Setup(rt)


def _batch_def():
    return next(t for t in table_defs("dev", "dev_") if t.name == "classification_review_batch")


def test_matching_constraints_are_not_churned():
    t = _batch_def()
    # Delta may store the expression with different spacing / backticks
    stored = {k: v.replace(" IN (", "  IN  (") for k, v in t.checks.items()}
    rt, s = _setup(stored)
    s._ensure_checks("tbl", t)
    assert not any("DROP CONSTRAINT" in x or "ADD CONSTRAINT" in x for x in rt.statements)


def test_changed_constraint_is_dropped_and_readded():
    t = _batch_def()
    stored = dict(t.checks)
    stored["valid_batch_status"] = "status IN ('OPEN')"
    rt, s = _setup(stored)
    s._ensure_checks("tbl", t)
    assert any("DROP CONSTRAINT `valid_batch_status`" in x for x in rt.statements)
    assert any("ADD CONSTRAINT `valid_batch_status`" in x for x in rt.statements)


def test_missing_constraint_is_added_without_drop():
    t = _batch_def()
    rt, s = _setup({})
    s._ensure_checks("tbl", t)
    assert not any("DROP CONSTRAINT" in x for x in rt.statements)
    assert sum("ADD CONSTRAINT" in x for x in rt.statements) == len(t.checks)
