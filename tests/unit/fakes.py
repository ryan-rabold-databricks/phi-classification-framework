"""Minimal stand-ins for Runtime and WorkspaceClient used by unit tests."""

from __future__ import annotations

from types import SimpleNamespace

from phi_framework.config import Settings


def settings(**overrides) -> Settings:
    base = {
        "env": "dev",
        "process_name": "dev_phi_classification_application",
        "run_id": "123",
        "governance_catalog": "dev_gov",
        "governance_schema": "data_governance",
        "governed_catalog_prefix": "dev_",
        "tag_key": "phi_classification",
        "governance_group": "governance",
    }
    base.update(overrides)
    return Settings(**base)


class FakeRuntime:
    """Records SQL; answers rows() from a list of (substring, rows) rules."""

    def __init__(self, s: Settings, rules=None, sql_error_for=None):
        self.s = s
        self.w = None
        self.rules = rules or []
        self.statements: list[str] = []
        self.events: list[dict] = []
        self.views: dict[str, list[dict]] = {}
        self.sql_error_for = sql_error_for or set()

    def t(self, name):
        return f"`{self.s.governance_catalog}`.`{self.s.governance_schema}`.`{name}`"

    def sql(self, statement):
        self.statements.append(statement)
        if statement in self.sql_error_for:
            raise RuntimeError("simulated DDL failure")
        return SimpleNamespace(collect=list)

    def rows(self, statement):
        self.statements.append(statement)
        for needle, rows in self.rules:
            if needle in statement:
                return rows(statement) if callable(rows) else rows
        return []

    def temp_view(self, rows, schema, name):
        self.views[name] = list(rows)
        return name

    def audit_update_set(self):
        return "audit_updated_at = current_timestamp()"

    def event(self, event_type, review_batch_id, review_item_id=None, **kw):
        self.events.append({"event_type": event_type, "review_item_id": review_item_id, **kw})

    def flush_events(self):
        return len(self.events)

    def insert_events_from_query(self, query):
        self.statements.append(query)


class FakePolicy:
    def __init__(self, allowed):
        self.allowed = frozenset(allowed)

    def is_allowed(self, v):
        return v in self.allowed
