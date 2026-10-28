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


def _fn_setup(stored_body):
    rows = [] if stored_body is None else [{"routine_definition": stored_body}]
    rt = FakeRuntime(settings(), rules=[("information_schema.routines", rows)])
    return rt, Setup(rt)


def test_unchanged_key_function_is_not_replaced():
    from phi_framework.keys import MAKE_KEY_BODY

    # Unity Catalog stores the body with its own line breaks; replacing would drop the App's EXECUTE grant.
    rt, s = _fn_setup(MAKE_KEY_BODY.replace("\n  ", "\n"))
    s._ensure_key_function()
    assert not any("CREATE OR REPLACE FUNCTION" in x for x in rt.statements)


def test_missing_or_changed_key_function_is_created():
    for stored in (None, "concat(prefix, 'x')"):
        rt, s = _fn_setup(stored)
        s._ensure_key_function()
        assert any("CREATE OR REPLACE FUNCTION" in x for x in rt.statements)
