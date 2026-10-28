"""Load Data Governance-owned reference data: the scope registry and the suggestion mapping.

These are the only two governance tables that accept direct edits. Rows are
upserted with audit process MANUAL and the editor's identity, and nothing is
deleted: to retire a scope set scan_enabled=false or an expiration_date; to
retire a mapping set is_active=false.

Usage:
  python tools/load_reference_data.py -p <profile> --warehouse-id <id> \
      --catalog <governance_catalog> [--schema data_governance] \
      [--scopes reference_data/scope_registry.csv] [--mapping reference_data/suggestion_mapping.csv]
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from databricks.sdk import WorkspaceClient
from sqlexec import Sql

from phi_framework import constants as c
from phi_framework.sqlutil import fqn, lit

AUDIT_INSERT = (
    "audit_created_at, audit_created_by, audit_created_process, audit_created_run_id, "
    "audit_updated_at, audit_updated_by, audit_updated_process, audit_updated_run_id"
)
AUDIT_VALUES = "current_timestamp(), current_user(), 'MANUAL', NULL, current_timestamp(), current_user(), 'MANUAL', NULL"
AUDIT_UPDATE = (
    "audit_updated_at = current_timestamp(), audit_updated_by = current_user(), "
    "audit_updated_process = 'MANUAL', audit_updated_run_id = NULL"
)


def _bool(v: str) -> str:
    return "true" if str(v).strip().lower() in {"true", "1", "yes", "y"} else "false"


def _emails(v: str) -> str:
    """Semicolon-separated emails -> a lower-case ARRAY<STRING> literal."""
    emails = [e.strip().lower() for e in (v or "").split(";") if e.strip()]
    if not emails:
        raise ValueError("each scope needs at least one email in steward_emails and governance_owner_emails")
    return "array(" + ", ".join(lit(e) for e in emails) + ")"


def _date(v: str) -> str:
    v = (v or "").strip()
    return f"DATE{lit(v)}" if v else "CAST(NULL AS DATE)"


def load_scopes(sql: Sql, table: str, path: str) -> int:
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return 0
    values = ",\n".join(
        "("
        + ", ".join(
            [
                lit(r["scope_id"].strip()),
                lit(r["catalog_name"].strip()),
                lit(r["schema_name"].strip()),
                lit(r["environment"].strip()),
                lit(r["risk_tier"].strip().upper()),
                _emails(r["steward_emails"]),
                _emails(r["governance_owner_emails"]),
                _bool(r["scan_enabled"]),
                _date(r["effective_date"]),
                _date(r.get("expiration_date", "")),
            ]
        )
        + ")"
        for r in rows
    )
    cols = ["scope_id", "catalog_name", "schema_name", "environment", "risk_tier", "steward_emails",
            "governance_owner_emails", "scan_enabled", "effective_date", "expiration_date"]
    changed = " OR ".join(f"NOT (t.{x} <=> s.{x})" for x in cols[1:])
    sql.run(
        f"""MERGE INTO {table} t
        USING (SELECT * FROM VALUES {values} AS v({', '.join(cols)})) s ON t.scope_id = s.scope_id
        WHEN MATCHED AND ({changed}) THEN UPDATE SET {', '.join(f'{x} = s.{x}' for x in cols[1:])}, {AUDIT_UPDATE}
        WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}, {AUDIT_INSERT})
          VALUES ({', '.join('s.' + x for x in cols)}, {AUDIT_VALUES})"""
    )
    return len(rows)


def load_mapping(sql: Sql, table: str, path: str) -> int:
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return 0
    values = ",\n".join(
        f"({lit(r['class_tag'].strip())}, {lit(r['phi_tag_value'].strip())}, {_bool(r['is_active'])}, "
        f"{lit((r.get('notes') or '').strip() or None)})"
        for r in rows
    )
    cols = ["class_tag", "phi_tag_value", "is_active", "notes"]
    changed = " OR ".join(f"NOT (t.{x} <=> s.{x})" for x in cols[1:])
    sql.run(
        f"""MERGE INTO {table} t
        USING (SELECT * FROM VALUES {values} AS v({', '.join(cols)})) s ON t.class_tag = s.class_tag
        WHEN MATCHED AND ({changed}) THEN UPDATE SET {', '.join(f'{x} = s.{x}' for x in cols[1:])}, {AUDIT_UPDATE}
        WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}, {AUDIT_INSERT})
          VALUES ({', '.join('s.' + x for x in cols)}, {AUDIT_VALUES})"""
    )
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--profile", default=None)
    ap.add_argument("--warehouse-id", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", default="data_governance")
    ap.add_argument("--scopes")
    ap.add_argument("--mapping")
    a = ap.parse_args()
    w = WorkspaceClient(profile=a.profile) if a.profile else WorkspaceClient()
    sql = Sql(w, a.warehouse_id)
    if a.scopes:
        n = load_scopes(sql, fqn(a.catalog, a.schema, c.T_SCOPE), a.scopes)
        print(f"Upserted {n} scope row(s)")
    if a.mapping:
        n = load_mapping(sql, fqn(a.catalog, a.schema, c.T_MAPPING), a.mapping)
        print(f"Upserted {n} mapping row(s)")


if __name__ == "__main__":
    main()
