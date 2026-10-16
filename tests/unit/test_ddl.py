from phi_framework import constants as c
from phi_framework.ddl import create_table_sql, table_defs


def test_every_table_defined_with_pk_and_audit_columns():
    defs = {t.name: t for t in table_defs("prod", "prod_")}
    assert set(defs) == set(c.ALL_TABLES)
    for t in defs.values():
        names = [col.name for col in t.columns]
        assert t.pk and all(p in names for p in t.pk)
        assert "audit_created_process" in names
        assert t.append_only == (t.name in c.APPEND_ONLY_TABLES)
        if t.append_only:
            assert "audit_updated_at" not in names


def test_process_checks_allow_manual_only_on_reference_tables():
    defs = {t.name: t for t in table_defs("prod", "prod_")}
    for name, t in defs.items():
        expr = t.checks["valid_audit_created_process"]
        assert "'prod_phi_classification_scan'" in expr
        assert ("'MANUAL'" in expr) == (name in c.MANUAL_EDIT_TABLES)


def test_environment_guardrail_constraint():
    scope = next(t for t in table_defs("qa", "qa_hc_") if t.name == c.T_SCOPE)
    assert scope.checks["catalog_matches_environment"] == "environment = 'qa' AND startswith(catalog_name, 'qa_hc_')"


def test_create_sql_marks_append_only():
    plan = next(t for t in table_defs("dev", "dev_") if t.name == c.T_PLAN)
    sql = create_table_sql("`c`.`s`", plan)
    assert "'delta.appendOnly' = 'true'" in sql and "NOT NULL" in sql
