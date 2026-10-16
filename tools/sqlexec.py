"""Minimal SQL-warehouse client for operator tools (runs on a laptop or in CI)."""

from __future__ import annotations

import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DeadlineExceeded, TemporarilyUnavailable, TooManyRequests
from databricks.sdk.service.sql import Disposition, Format, StatementState

_TRANSIENT = (DeadlineExceeded, TemporarilyUnavailable, TooManyRequests)


class Sql:
    def __init__(self, w: WorkspaceClient, warehouse_id: str):
        self.w = w
        self.warehouse_id = warehouse_id

    def _submit(self, statement: str, attempts: int = 4):
        # Retries only when submission itself failed transiently. Operator statements are
        # idempotent (MERGE / CREATE IF NOT EXISTS / SELECT), so a duplicate submission is safe.
        for attempt in range(1, attempts + 1):
            try:
                return self.w.statement_execution.execute_statement(
                    statement=statement,
                    warehouse_id=self.warehouse_id,
                    wait_timeout="30s",
                    disposition=Disposition.INLINE,
                    format=Format.JSON_ARRAY,
                )
            except _TRANSIENT:
                if attempt == attempts:
                    raise
                time.sleep(10 * attempt)
        raise AssertionError("unreachable")

    def run(self, statement: str, timeout_s: int = 600) -> list[dict]:
        resp = self._submit(statement)
        deadline = time.time() + timeout_s
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            if time.time() > deadline:
                raise TimeoutError(statement[:200])
            time.sleep(2)
            resp = self.w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            err = resp.status.error.message if resp.status.error else resp.status.state
            raise RuntimeError(f"SQL failed: {err}\n{statement[:1000]}")
        if not resp.manifest or not resp.manifest.schema or not resp.manifest.schema.columns:
            return []
        cols = [c.name for c in resp.manifest.schema.columns]
        rows = list(resp.result.data_array or []) if resp.result else []
        chunk = resp.result.next_chunk_index if resp.result else None
        while chunk is not None:
            part = self.w.statement_execution.get_statement_result_chunk_n(resp.statement_id, chunk)
            rows.extend(part.data_array or [])
            chunk = part.next_chunk_index
        return [dict(zip(cols, r)) for r in rows]

    def scalar(self, statement: str):
        rows = self.run(statement)
        return next(iter(rows[0].values())) if rows else None
