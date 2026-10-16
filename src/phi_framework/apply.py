"""<env>_phi_classification_application: apply and verify validated tag actions.

Triggered by new rows in classification_application_plan. For each plan whose
item is PLANNED (or APPLICATION_IN_PROGRESS after a crash, or
APPLICATION_FAILED when ``retry_failed`` is set), the job:

1. marks the item APPLICATION_IN_PROGRESS;
2. rechecks the live column: it exists, its object type and fingerprint match
   the plan, the catalog is inside this environment, and a SET value is still
   allowed by the governed-tag policy;
3. executes the plan's rendered_ddl exactly as stored;
4. verifies the resulting tag state in information_schema.column_tags;
5. records TAG_VERIFIED / ATTESTED_NOT_PHI, or APPLICATION_FAILED with the reason.

Plan rows are never changed; progress is recorded on the item and in events.
"""

from __future__ import annotations

from phi_framework import constants as c
from phi_framework.batches import close_completed_batches
from phi_framework.config import Settings
from phi_framework.directory import TagPolicy
from phi_framework.keys import fingerprint
from phi_framework.runtime import Runtime, log
from phi_framework.sqlutil import in_list, lit, qi


class Application:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s: Settings = rt.s

    def run(self) -> None:
        policy = TagPolicy(self.rt.w, self.s.tag_key)
        statuses = [c.PLANNED, c.APPLICATION_IN_PROGRESS] + ([c.APPLICATION_FAILED] if self.s.retry_failed else [])
        plans = self.rt.rows(
            f"""SELECT p.*, i.status AS item_status
            FROM {self.rt.t(c.T_PLAN)} p JOIN {self.rt.t(c.T_ITEM)} i ON i.review_item_id = p.review_item_id
            WHERE i.status IN ({in_list(statuses)})
            ORDER BY p.catalog_name, p.schema_name, p.table_name, p.column_name"""
        )
        log.info("%d plan row(s) to apply", len(plans))
        ok = failed = 0
        for p in plans:
            self._set_status(p["review_item_id"], c.APPLICATION_IN_PROGRESS, error=None)
            try:
                self._precheck(p, policy)
                self.rt.sql(p["rendered_ddl"])
                verified = self._verify(p)
                if p["action_type"] == c.SET_PHI_TAG:
                    self._set_status(p["review_item_id"], c.TAG_VERIFIED, error=None, applied=True)
                    self.rt.event(c.EV_TAG_VERIFIED, p["review_batch_id"], p["review_item_id"],
                                  business_actor=p["reviewed_by"],
                                  details={"application_plan_id": p["application_plan_id"], "verified_value": verified})
                else:
                    self._set_status(p["review_item_id"], c.ATTESTED_NOT_PHI, error=None, applied=True)
                    self.rt.event(c.EV_TAG_REMOVED, p["review_batch_id"], p["review_item_id"],
                                  business_actor=p["reviewed_by"],
                                  details={"application_plan_id": p["application_plan_id"]})
                ok += 1
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"[:4000]
                log.error("Plan %s failed: %s", p["application_plan_id"], msg)
                self._set_status(p["review_item_id"], c.APPLICATION_FAILED, error=msg)
                self.rt.event(c.EV_APPLICATION_FAILED, p["review_batch_id"], p["review_item_id"],
                              business_actor=p["reviewed_by"],
                              details={"application_plan_id": p["application_plan_id"], "error": msg})
                failed += 1
            self.rt.flush_events()
        close_completed_batches(self.rt)
        self.rt.flush_events()
        log.info("Applied %d, failed %d", ok, failed)
        if failed:
            # Fail the run so job failure notifications reach Data Platform.
            raise RuntimeError(f"{failed} plan row(s) failed; see APPLICATION_FAILED items")

    def _set_status(self, item_id: str, status: str, error: str | None, applied: bool = False) -> None:
        applied_sql = ", applied_at = current_timestamp()" if applied else ""
        self.rt.sql(
            f"""UPDATE {self.rt.t(c.T_ITEM)} SET status = {lit(status)}, application_error = {lit(error)}{applied_sql},
            {self.rt.audit_update_set()} WHERE review_item_id = {lit(item_id)}"""
        )

    def _precheck(self, p: dict, policy: TagPolicy) -> None:
        if not p["catalog_name"].startswith(self.s.governed_catalog_prefix):
            raise PermissionError(f"Catalog {p['catalog_name']} is outside this environment")
        cat = p["catalog_name"]
        rows = self.rt.rows(
            f"""SELECT c.full_data_type, c.comment, t.table_type
            FROM {qi(cat)}.information_schema.columns c
            JOIN {qi(cat)}.information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = {lit(p['schema_name'])} AND c.table_name = {lit(p['table_name'])}
              AND c.column_name = {lit(p['column_name'])}"""
        )
        if not rows:
            raise LookupError("Column no longer exists (stale metadata); return to Data Governance")
        live = rows[0]
        if live["table_type"] != p["table_type"]:
            raise ValueError(f"Object type changed from {p['table_type']} to {live['table_type']}")
        fp = fingerprint(cat, p["schema_name"], p["table_name"], p["column_name"], live["full_data_type"], live["comment"])
        if fp != p["column_fingerprint"]:
            raise ValueError("Column metadata changed since approval (fingerprint mismatch); the next scan will reopen it")
        if p["action_type"] == c.SET_PHI_TAG and not policy.is_allowed(p["approved_tag_value"]):
            raise ValueError(f"{p['approved_tag_value']!r} is no longer an allowed value for {self.s.tag_key}")

    def _verify(self, p: dict) -> str | None:
        rows = self.rt.rows(
            f"""SELECT tag_value FROM {qi(p['catalog_name'])}.information_schema.column_tags
            WHERE schema_name = {lit(p['schema_name'])} AND table_name = {lit(p['table_name'])}
              AND column_name = {lit(p['column_name'])} AND tag_name = {lit(self.s.tag_key)}"""
        )
        if p["action_type"] == c.SET_PHI_TAG:
            if not rows or rows[0]["tag_value"] != p["approved_tag_value"]:
                raise AssertionError(f"Verification failed: expected {p['approved_tag_value']!r}, found {rows[0]['tag_value'] if rows else None!r}")
            return rows[0]["tag_value"]
        if rows:
            raise AssertionError(f"Verification failed: tag still present with value {rows[0]['tag_value']!r}")
        return None


def main(settings: Settings) -> None:
    settings.validate(expected_role=c.ROLE_APPLICATION)
    Application(Runtime(settings)).run()
