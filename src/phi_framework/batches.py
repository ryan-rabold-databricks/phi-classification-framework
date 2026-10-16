"""Batch lifecycle helpers shared by all three jobs."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from phi_framework import constants as c
from phi_framework.runtime import Runtime, log
from phi_framework.sqlutil import in_list, lit
from phi_framework.validation import add_business_days


def due_at_utc(created_utc: datetime, business_days: int, tz_name: str) -> datetime:
    """Due at 17:00 local time, ``business_days`` business days after creation."""
    tz = ZoneInfo(tz_name)
    local = created_utc.replace(tzinfo=UTC).astimezone(tz)
    due_local_date = add_business_days(local.replace(tzinfo=None), business_days).date()
    due_local = datetime.combine(due_local_date, time(17, 0), tzinfo=tz)
    return due_local.astimezone(UTC).replace(tzinfo=None)


def close_completed_batches(rt: Runtime) -> None:
    """Close OPEN batches whose items are all terminal.

    The design assigns BATCH_CLOSED to the application job, but a batch resolved
    entirely by attestations or CLOSED_NO_ACTION never produces plan rows and so
    never triggers that job. Every job therefore calls this helper at the end of
    its run (see docs/design-notes.md).
    """
    terminal = in_list(c.TERMINAL_STATUSES)
    rt.sql(
        f"""MERGE INTO {rt.t(c.T_BATCH)} t
        USING (
          SELECT b.review_batch_id
          FROM {rt.t(c.T_BATCH)} b
          JOIN {rt.t(c.T_ITEM)} i ON i.review_batch_id = b.review_batch_id
          WHERE b.status = {lit(c.BATCH_OPEN)}
          GROUP BY b.review_batch_id
          HAVING count_if(i.status NOT IN ({terminal})) = 0
        ) s ON t.review_batch_id = s.review_batch_id
        WHEN MATCHED THEN UPDATE SET status = {lit(c.BATCH_CLOSED)}, closed_at = current_timestamp(), {rt.audit_update_set()}"""
    )
    rt.insert_events_from_query(
        f"""SELECT review_batch_id, CAST(NULL AS STRING) AS review_item_id, {lit(c.EV_BATCH_CLOSED)} AS event_type,
               CAST(NULL AS STRING) AS business_actor, CAST(NULL AS STRING) AS event_details_json,
               CAST(NULL AS STRING) AS discriminator
        FROM {rt.t(c.T_BATCH)}
        WHERE status = {lit(c.BATCH_CLOSED)} AND last_error IS NULL AND audit_updated_run_id = {lit(rt.s.run_id)}
          AND audit_updated_process = {lit(rt.s.process_name)}"""
    )
    log.info("Closed completed batches")
