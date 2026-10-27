"""Idempotent setup: schema, key function, tables, constraints, keys, and comments.

Runs before the first job run and on every deployment. Safe to rerun:
existing objects are reconciled, never dropped (except a CHECK constraint whose
expression changed, which is dropped and re-added and recorded in table history).
"""

from __future__ import annotations

import re

from phi_framework.config import Settings
from phi_framework.ddl import TableDef, create_table_sql, table_defs
from phi_framework.keys import make_key_function_ddl
from phi_framework.runtime import Runtime, log
from phi_framework.sqlutil import lit, qi


def _norm(expr: str) -> str:
    return re.sub(r"\s+", " ", expr.replace("`", "")).strip().lower()


class Setup:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s: Settings = rt.s

    def run(self) -> None:
        s = self.s
        self.rt.sql(
            f"CREATE SCHEMA IF NOT EXISTS {s.schema_fqn} COMMENT "
            f"{lit('PHI classification governance records (system of record for the classification audit process).')}"
        )
        self.rt.sql(make_key_function_ddl(s.schema_fqn))

        defs = table_defs(s.env, s.governed_catalog_prefix)
        for t in defs:
            self._ensure_table(t)
        # Keys after all tables exist so foreign keys can reference primary keys.
        for t in defs:
            self._ensure_keys(t)
        log.info("Setup complete for %s", s.schema_fqn)

    # ------------------------------------------------------------------
    def _ensure_table(self, t: TableDef) -> None:
        fq = f"{self.s.schema_fqn}.{qi(t.name)}"
        self.rt.sql(create_table_sql(self.s.schema_fqn, t))
        existing = {
            r["col_name"]: (r.get("comment") or "")
            for r in self.rt.rows(f"DESCRIBE TABLE {fq}")
            if r["col_name"] and not r["col_name"].startswith("#")
        }
        for col in t.columns:
            if col.name not in existing:
                # New columns on an existing table are added as nullable; NOT NULL is enforced for new tables.
                log.info("Adding column %s.%s", t.name, col.name)
                self.rt.sql(f"ALTER TABLE {fq} ADD COLUMNS ({qi(col.name)} {col.type} COMMENT {lit(col.comment)})")
            elif existing[col.name] != col.comment:
                self.rt.sql(f"ALTER TABLE {fq} ALTER COLUMN {qi(col.name)} COMMENT {lit(col.comment)}")
        self.rt.sql(f"COMMENT ON TABLE {fq} IS {lit(t.comment)}")
        props = {"delta.enableDeletionVectors": "true", "delta.enableRowTracking": "true"}
        if t.append_only:
            props["delta.appendOnly"] = "true"
        self.rt.sql(f"ALTER TABLE {fq} SET TBLPROPERTIES ({', '.join(f'{lit(k)} = {lit(v)}' for k, v in props.items())})")
        self._ensure_checks(fq, t)

    def _ensure_checks(self, fq: str, t: TableDef) -> None:
        current = {
            r["key"][len("delta.constraints.") :]: r["value"]
            for r in self.rt.rows(f"SHOW TBLPROPERTIES {fq}")
            if r["key"].startswith("delta.constraints.")
        }
        for name, expr in t.checks.items():
            if name in current and _norm(current[name]) == _norm(expr):
                continue
            if name in current:
                log.warning("CHECK %s on %s changed; dropping and re-adding (recorded in table history)", name, t.name)
                self.rt.sql(f"ALTER TABLE {fq} DROP CONSTRAINT {qi(name)}")
            self.rt.sql(f"ALTER TABLE {fq} ADD CONSTRAINT {qi(name)} CHECK ({expr})")

    def _ensure_keys(self, t: TableDef) -> None:
        fq = f"{self.s.schema_fqn}.{qi(t.name)}"
        cat, sch = self.s.governance_catalog, self.s.governance_schema
        names = {
            r["constraint_name"]
            for r in self.rt.rows(
                f"SELECT constraint_name FROM {qi(cat)}.information_schema.table_constraints "
                f"WHERE table_schema = {lit(sch)} AND table_name = {lit(t.name)}"
            )
        }
        pk_name = f"pk_{t.name}"
        if pk_name not in names:
            self.rt.sql(f"ALTER TABLE {fq} ADD CONSTRAINT {qi(pk_name)} PRIMARY KEY ({', '.join(qi(x) for x in t.pk)})")
        for fk_name, cols, ref_table, ref_cols in t.fks:
            if fk_name not in names:
                self.rt.sql(
                    f"ALTER TABLE {fq} ADD CONSTRAINT {qi(fk_name)} FOREIGN KEY ({', '.join(qi(x) for x in cols)}) "
                    f"REFERENCES {self.s.schema_fqn}.{qi(ref_table)} ({', '.join(qi(x) for x in ref_cols)})"
                )


def main(settings: Settings) -> None:
    settings.validate()
    Setup(Runtime(settings)).run()
