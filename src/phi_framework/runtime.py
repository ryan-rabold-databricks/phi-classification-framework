"""Shared runtime context: Spark session, workspace client, settings, and write helpers.

Every governance-table write goes through ``Runtime`` so the standard audit
columns are populated the same way everywhere:

* on insert, audit_updated_* equal audit_created_*;
* on update, only audit_updated_* change.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from phi_framework import constants as c
from phi_framework.config import Settings
from phi_framework.keys import make_key
from phi_framework.sqlutil import lit, qi

log = logging.getLogger("phi_framework")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def utcnow() -> datetime:
    """Naive UTC timestamp (the Spark session time zone is pinned to UTC)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Runtime:
    def __init__(self, settings: Settings, spark=None, workspace_client=None):
        self.s = settings
        if spark is None:
            from pyspark.sql import SparkSession

            spark = SparkSession.builder.getOrCreate()
        self.spark = spark
        self.spark.conf.set("spark.sql.session.timeZone", "UTC")
        if workspace_client is None:
            from databricks.sdk import WorkspaceClient

            workspace_client = WorkspaceClient()
        self.w = workspace_client
        self._events: list[dict] = []

    # ---------- SQL ----------
    def sql(self, statement: str):
        log.debug("SQL: %s", statement)
        return self.spark.sql(statement)

    def rows(self, statement: str) -> list[dict]:
        return [r.asDict(recursive=True) for r in self.sql(statement).collect()]

    def t(self, name: str) -> str:
        return self.s.table(name)

    # ---------- audit column fragments ----------
    @property
    def _proc(self) -> str:
        return lit(self.s.process_name)

    @property
    def _run(self) -> str:
        return lit(self.s.run_id)

    def audit_insert_exprs(self, created_only: bool = False) -> dict[str, str]:
        exprs = {
            "audit_created_at": "current_timestamp()",
            "audit_created_by": "current_user()",
            "audit_created_process": self._proc,
            "audit_created_run_id": self._run,
        }
        if not created_only:
            exprs.update(
                {
                    "audit_updated_at": "current_timestamp()",
                    "audit_updated_by": "current_user()",
                    "audit_updated_process": self._proc,
                    "audit_updated_run_id": self._run,
                }
            )
        return exprs

    def audit_update_set(self) -> str:
        return (
            "audit_updated_at = current_timestamp(), audit_updated_by = current_user(), "
            f"audit_updated_process = {self._proc}, audit_updated_run_id = {self._run}"
        )

    def insert_values_clause(self, source_alias: str, columns: list[str], created_only: bool = False) -> str:
        """Build '(cols...) VALUES (src.cols..., audit...)' for MERGE ... WHEN NOT MATCHED THEN INSERT."""
        audit = self.audit_insert_exprs(created_only)
        names = columns + list(audit)
        values = [f"{source_alias}.{qi(col)}" for col in columns] + list(audit.values())
        return f"({', '.join(qi(n) for n in names)}) VALUES ({', '.join(values)})"

    # ---------- events ----------
    def event(
        self,
        event_type: str,
        review_batch_id: str,
        review_item_id: str | None = None,
        business_actor: str | None = None,
        details: dict | None = None,
        discriminator: str | None = None,
        event_timestamp: datetime | None = None,
    ) -> None:
        """Queue an event; ``flush_events`` writes the queue with an insert-only MERGE."""
        if event_type not in c.EVENT_TYPES:
            raise ValueError(f"Unknown event type {event_type}")
        self._events.append(
            {
                "event_id": make_key(
                    "EVT", [event_type, review_batch_id, review_item_id, self.s.run_id, discriminator]
                ),
                "review_batch_id": review_batch_id,
                "review_item_id": review_item_id,
                "event_type": event_type,
                "event_timestamp": event_timestamp or utcnow(),
                "business_actor": business_actor,
                "event_details_json": json.dumps(details, default=str, sort_keys=True) if details else None,
                "discriminator": discriminator,
            }
        )

    def flush_events(self) -> int:
        if not self._events:
            return 0
        from pyspark.sql.types import StringType, StructField, StructType, TimestampType

        schema = StructType(
            [
                StructField("event_id", StringType(), False),
                StructField("review_batch_id", StringType(), False),
                StructField("review_item_id", StringType(), True),
                StructField("event_type", StringType(), False),
                StructField("event_timestamp", TimestampType(), False),
                StructField("business_actor", StringType(), True),
                StructField("event_details_json", StringType(), True),
                StructField("discriminator", StringType(), True),
            ]
        )
        # De-duplicate within the queue (same deterministic key = same event).
        unique = {e["event_id"]: e for e in self._events}
        df = self.spark.createDataFrame(list(unique.values()), schema)
        view = f"_phi_events_{self.s.run_id}"
        df.createOrReplaceTempView(view)
        audit = self.audit_insert_exprs(created_only=True)
        cols = ["event_id", "review_batch_id", "review_item_id", "event_type", "event_timestamp", "business_actor", "event_details", "discriminator"]
        vals = [
            "s.event_id",
            "s.review_batch_id",
            "s.review_item_id",
            "s.event_type",
            "s.event_timestamp",
            "s.business_actor",
            "CASE WHEN s.event_details_json IS NULL THEN NULL ELSE parse_json(s.event_details_json) END",
            "s.discriminator",
        ]
        self.sql(
            f"""MERGE INTO {self.t(c.T_EVENT)} t USING {view} s ON t.event_id = s.event_id
            WHEN NOT MATCHED THEN INSERT ({', '.join(cols + list(audit))})
            VALUES ({', '.join(vals + list(audit.values()))})"""
        )
        n = len(unique)
        self._events.clear()
        log.info("Wrote %d events", n)
        return n

    def insert_events_from_query(self, query: str) -> None:
        """Insert events from a query returning review_batch_id, review_item_id, event_type,
        business_actor, event_details_json, discriminator. Insert-only, so reruns never duplicate."""
        from phi_framework.keys import make_key_sql

        key = make_key_sql("EVT", "q.event_type", "q.review_batch_id", "q.review_item_id", lit(self.s.run_id), "q.discriminator")
        audit = self.audit_insert_exprs(created_only=True)
        cols = ["event_id", "review_batch_id", "review_item_id", "event_type", "event_timestamp", "business_actor", "event_details", "discriminator"]
        self.sql(
            f"""MERGE INTO {self.t(c.T_EVENT)} t
            USING (SELECT {key} AS event_id, q.* FROM ({query}) q) s
            ON t.event_id = s.event_id
            WHEN NOT MATCHED THEN INSERT ({', '.join(cols + list(audit))})
            VALUES (s.event_id, s.review_batch_id, s.review_item_id, s.event_type, current_timestamp(),
                    s.business_actor,
                    CASE WHEN s.event_details_json IS NULL THEN NULL ELSE parse_json(s.event_details_json) END,
                    s.discriminator, {', '.join(audit.values())})"""
        )

    # ---------- misc ----------
    def temp_view(self, rows: Iterable[dict], schema, name: str) -> str:
        df = self.spark.createDataFrame(list(rows), schema)
        view = f"{name}_{self.s.run_id}"
        df.createOrReplaceTempView(view)
        return view
