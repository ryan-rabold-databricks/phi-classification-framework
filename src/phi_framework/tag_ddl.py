"""Render the exact tag DDL stored in classification_application_plan.rendered_ddl."""

from __future__ import annotations

from phi_framework import constants as c
from phi_framework.sqlutil import fqn, lit, qi

_OBJECT_KEYWORD = {
    "MANAGED": "TABLE",
    "EXTERNAL": "TABLE",
    "MATERIALIZED_VIEW": "MATERIALIZED VIEW",
    "STREAMING_TABLE": "STREAMING TABLE",
}


def render_tag_ddl(
    action_type: str,
    table_type: str,
    catalog: str,
    schema: str,
    table: str,
    column: str,
    tag_key: str,
    value: str | None,
) -> str:
    if table_type not in _OBJECT_KEYWORD:
        raise ValueError(f"Unsupported table_type {table_type!r}")
    target = f"ALTER {_OBJECT_KEYWORD[table_type]} {fqn(catalog, schema, table)} ALTER COLUMN {qi(column)}"
    if action_type == c.SET_PHI_TAG:
        if not value:
            raise ValueError("SET_PHI_TAG requires a value")
        return f"{target} SET TAGS ({lit(tag_key)} = {lit(value)})"
    if action_type == c.REMOVE_PHI_TAG:
        return f"{target} UNSET TAGS ({lit(tag_key)})"
    raise ValueError(f"Unsupported action_type {action_type!r}")
