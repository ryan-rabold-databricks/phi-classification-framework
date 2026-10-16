"""Export review workbooks to the governed Volume (``outbound/<review_batch_id>/``).

Three workbook types:

* ASSIGNMENT -- one per OPEN batch with unassigned items; Data Governance fills
  in assigned_steward and returns it to ``inbound/``.
* REVIEW -- one per batch and assigned steward with items awaiting a decision
  (PENDING_STEWARD_REVIEW or VALIDATION_FAILED).
* PRIVACY -- one per batch with items in NEEDS_PRIVACY_REVIEW; Data
  Governance records Privacy's outcome.

Exports write no governance rows. A file name carries a hash of its content,
so an unchanged export is never written twice and an earlier export is never
overwritten.
"""

from __future__ import annotations

import io
import json
import re

from phi_framework import constants as c
from phi_framework.config import Settings
from phi_framework.directory import TagPolicy
from phi_framework.runtime import Runtime, log, utcnow
from phi_framework.sqlutil import in_list, lit
from phi_framework.workbook import ASSIGNMENT_COLUMNS, REVIEW_COLUMNS, build_workbook, content_hash


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value.split("@")[0]).strip("_").lower() or "unknown"


class Export:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s: Settings = rt.s
        self.created: list[str] = []

    def run(self) -> list[str]:
        policy = TagPolicy(self.rt.w, self.s.tag_key, self.s.allowed_values_override)
        allowed = sorted(policy.allowed)
        items = self.rt.rows(
            f"""WITH last_error AS (
                  SELECT review_item_id,
                         max_by(CAST(event_details:errors AS STRING), event_timestamp) AS last_validation_error
                  FROM {self.rt.t(c.T_EVENT)}
                  WHERE event_type = {lit(c.EV_VALIDATION_FAILED)} AND review_item_id IS NOT NULL
                  GROUP BY review_item_id)
                SELECT i.*, b.scope_id, s.steward_group, e.last_validation_error
                FROM {self.rt.t(c.T_ITEM)} i
                JOIN {self.rt.t(c.T_BATCH)} b ON b.review_batch_id = i.review_batch_id
                JOIN {self.rt.t(c.T_SCOPE)} s ON s.scope_id = b.scope_id
                LEFT JOIN last_error e ON e.review_item_id = i.review_item_id
                WHERE b.status = {lit(c.BATCH_OPEN)}
                  AND i.status IN ({in_list([c.PENDING_STEWARD_REVIEW, c.VALIDATION_FAILED, c.NEEDS_PRIVACY_REVIEW])})
                ORDER BY i.catalog_name, i.schema_name, i.table_name, i.column_name"""
        )
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for it in items:
            if it["status"] == c.NEEDS_PRIVACY_REVIEW:
                key = (c.WB_PRIVACY, it["review_batch_id"], "governance")
            elif it["assigned_steward"] is None:
                if it["status"] != c.PENDING_STEWARD_REVIEW:
                    continue
                key = (c.WB_ASSIGNMENT, it["review_batch_id"], "governance")
            else:
                key = (c.WB_REVIEW, it["review_batch_id"], it["assigned_steward"])
            groups.setdefault(key, []).append(it)

        for (wtype, batch_id, audience), rows in sorted(groups.items()):
            self._write(wtype, batch_id, audience, [self._row(wtype, r) for r in rows], allowed)
        log.info("Export created %d workbook(s)", len(self.created))
        self._report()
        return self.created

    def _row(self, wtype: str, it: dict) -> dict:
        if wtype == c.WB_ASSIGNMENT:
            return {h: it.get(h) for h, _, _ in ASSIGNMENT_COLUMNS}
        row = {h: it.get(h) for h, _, _ in REVIEW_COLUMNS}
        if it["status"] == c.VALIDATION_FAILED:
            # Pre-fill the rejected submission so the reviewer corrects only what failed.
            row["reviewed_at"] = None
        else:
            for k in ("decision", "corrected_tag_value", "steward_comment", "reviewed_at"):
                row[k] = None
        if wtype == c.WB_PRIVACY:
            row["reviewed_by"] = None  # the Data Governance user records the Privacy outcome
        else:
            row["reviewed_by"] = it.get("reviewed_by") or it.get("assigned_steward")
        err = it.get("last_validation_error")
        if err and it["status"] == c.VALIDATION_FAILED:
            try:
                row["last_validation_error"] = "; ".join(json.loads(err))
            except (TypeError, ValueError):
                row["last_validation_error"] = err
        else:
            row["last_validation_error"] = None
        return row

    def _write(self, wtype: str, batch_id: str, audience: str, rows: list[dict], allowed: list[str]) -> None:
        digest = content_hash(wtype, batch_id, audience, rows)
        name = f"{batch_id}__{wtype}__{_slug(audience)}__{digest[:12]}.xlsx"
        path = f"{self.s.outbound_path}/{batch_id}/{name}"
        if self._exists(path):
            log.info("Unchanged export already exists: %s", path)
            return
        data = build_workbook(
            wtype,
            batch_id,
            audience,
            rows,
            allowed,
            meta={
                "env": self.s.env,
                "tag_key": self.s.tag_key,
                "content_hash": digest,
                "exported_at_utc": utcnow().isoformat(timespec="seconds"),
                "export_run_id": self.s.run_id,
                "framework_version": c.FRAMEWORK_VERSION,
            },
            password=self.s.workbook_protection_password,
        )
        self.rt.w.files.upload(path, io.BytesIO(data), overwrite=False)
        self.created.append(path)
        log.info("Exported %s (%d rows)", path, len(rows))

    def _exists(self, path: str) -> bool:
        try:
            self.rt.w.files.get_metadata(path)
            return True
        except Exception:  # NotFound
            return False

    def _report(self) -> None:
        try:
            from databricks.sdk.runtime import dbutils  # type: ignore

            dbutils.jobs.taskValues.set(key="exported_files", value=self.created[:200])
        except Exception as exc:  # task values are informational only
            log.debug("Could not set task value: %s", exc)


def main(settings: Settings) -> None:
    settings.validate()
    Export(Runtime(settings)).run()
