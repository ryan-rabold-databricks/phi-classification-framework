"""Deterministic key generation.

Every primary key except ``scope_id`` is a hash of the values that make the
row unique, so a retried job run rebuilds the same keys and an insert-only
MERGE skips rows that already exist. The Python implementation, the Spark
column expression, and the ``make_key`` SQL function all produce identical
output (verified by tests/integration/e2e.py).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

_SEP = chr(31)  # unit separator: cannot appear in Unity Catalog names
_NULL = chr(0)  # distinct placeholder for NULL parts


def make_key(prefix: str, parts: Iterable[str | None]) -> str:
    joined = _SEP.join(_NULL if p is None else str(p) for p in parts)
    return f"{prefix}-{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:16].upper()}"


def make_key_sql(prefix: str, *col_exprs: str) -> str:
    """SQL expression equivalent to make_key(prefix, [cols...]) without a UDF call."""
    arr = ", ".join(f"CAST({e} AS STRING)" for e in col_exprs)
    return (
        f"concat('{prefix}', '-', upper(substr(sha2(array_join(transform(array({arr}), "
        f"p -> coalesce(p, chr(0))), chr(31)), 256), 1, 16)))"
    )


MAKE_KEY_BODY = """concat(prefix, '-', upper(substr(
  sha2(array_join(transform(parts, p -> coalesce(p, chr(0))), chr(31)), 256), 1, 16)))"""


def make_key_function_ddl(schema_fqn: str) -> str:
    return f"""
CREATE OR REPLACE FUNCTION {schema_fqn}.make_key(prefix STRING, parts ARRAY<STRING>)
RETURNS STRING
COMMENT 'Deterministic framework key: <prefix>-<first 16 hex chars of SHA-256 of parts>.'
RETURN {MAKE_KEY_BODY}
"""


def fingerprint(catalog: str, schema: str, table: str, column: str, full_data_type: str, comment: str | None) -> str:
    """Python mirror of the scan's column fingerprint, for tests and tools.

    Spark ``trim`` removes only spaces, so this strips only spaces (not tabs or newlines).
    The jobs always compute fingerprints with ``fingerprint_sql`` so both sides use one engine.
    """
    parts = [
        catalog.strip(' ').lower(),
        schema.strip(' ').lower(),
        table.strip(' ').lower(),
        column.strip(' ').lower(),
        full_data_type.strip(' ').upper(),
        (comment or "").strip(' ').lower(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


FINGERPRINT_SQL = """sha2(concat_ws('|',
    lower(trim({cat})), lower(trim({sch})), lower(trim({tbl})), lower(trim({col})),
    upper(trim({typ})), lower(trim(coalesce({cmt}, '')))), 256)"""


def fingerprint_sql(cat: str, sch: str, tbl: str, col: str, typ: str, cmt: str) -> str:
    return FINGERPRINT_SQL.format(cat=cat, sch=sch, tbl=tbl, col=col, typ=typ, cmt=cmt)
