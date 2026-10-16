"""Small SQL rendering helpers. All identifiers and literals pass through here."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime


def qi(name: str) -> str:
    """Quote a single identifier part."""
    return "`" + str(name).replace("`", "``") + "`"


def fqn(*parts: str) -> str:
    return ".".join(qi(p) for p in parts)


def lit(value) -> str:
    """Render a Python value as a Spark SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"TIMESTAMP'{value.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
    s = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


def in_list(values: Iterable[str]) -> str:
    return ", ".join(lit(v) for v in values)
