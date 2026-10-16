"""<env>_phi_classification_application: apply and verify validated tag actions.

Triggered by new rows in classification_application_plan. For every plan whose
item is PLANNED (or APPLICATION_IN_PROGRESS after a crash, or
APPLICATION_FAILED when ``retry_failed`` is set), the job:

1. marks the items APPLICATION_IN_PROGRESS (one statement);
2. rechecks each live column: it exists, its object type and fingerprint match
   the plan, the catalog is inside this environment, and a SET value is still
   allowed by the governed-tag policy (one query per catalog);
3. executes each plan's rendered_ddl exactly as stored (one statement per column);
4. verifies the resulting tag state in information_schema.column_tags (one query per catalog);
5. records TAG_VERIFIED / ATTESTED_NOT_PHI, or APPLICATION_FAILED with the reason (one statement).

Plan rows are never changed; progress is recorded on the item and in events.
Tag DDL is idempotent, so a run interrupted after step 3 is safely redone.
"""

from __future__ import annotations

from phi_framework import constants as c
from phi_framework.batches import close_completed_batches
from phi_framework.config import Settings
from phi_framework.directory import TagPolicy
from phi_framework.keys import fingerprint
from phi_framework.runtime import Runtime, log
from phi_framework.sqlutil import in_list, lit, qi

RESULT_SCHEMA = "review_item_id string, status string, application_error string, applied boolean"


def _ident(p: dict) -> tuple[str, str, str]:
    return (p["schema_name"], p["table_name"], p["column_name"])


class Application:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s: Settings = rt.s

    def run(self) -> None:
        policy = TagPolicy(self.rt.w, self.s.tag_key, self.s.allowed_values_override)
        statuses = [c.PLANNED, c.APPLICATION_IN_PROGRESS] + ([c.APPLICATION_FAILED] if self.s.retry_failed else [])
        plans = self.rt.rows(
            f"""SELECT p.* FROM {self.rt.t(c.T_PLAN)} p
            JOIN {self.rt.t(c.T_ITEM)} i ON i.review_item_id = p.review_item_id
            WHERE i.status IN ({in_list(statuses)})
            ORDER BY p.catalog_name, p.schema_name, p.table_name, p.column_name"""
        )
        log.info("%d plan row(s) to apply", len(plans))
        if plans:
            self._mark_in_progress([p["review_item_id"] for p in plans])
            errors: dict[str, str] = {}
            self._precheck(plans, policy, errors)
            for p in plans:
                if p["review_item_id"] in errors:
                    continue
                try:
                    self.rt.sql(p["rendered_ddl"])
                except Exception as exc:  # record per-item failure and continue
                    errors[p["review_item_id"]] = f"{type(exc).__name__}: {exc}"[:4000]
            verified = self._verify([p for p in plans if p["review_item_id"] not in errors], errors)
            self._record(plans, errors, verified)
        close_completed_batches(self.rt)
        self.rt.flush_events()
        failed = sum(1 for p in plans if p["review_item_id"] in errors) if plans else 0
        log.info("Applied %d, failed %d", len(plans) - failed, failed)
        if failed:
            # Fail the run so job failure notifications reach Data Platform.
            raise RuntimeError(f"{failed} plan row(s) failed; see APPLICATION_FAILED items")

    # ------------------------------------------------------------------
    def _mark_in_progress(self, item_ids: list[str]) -> None:
        view = self.rt.temp_view([{"review_item_id": i} for i in item_ids], "review_item_id string", "phi_apply_ids")
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_ITEM)} t USING {view} s ON t.review_item_id = s.review_item_id
            WHEN MATCHED THEN UPDATE SET status = {lit(c.APPLICATION_IN_PROGRESS)}, application_error = NULL,
              {self.rt.audit_update_set()}"""
        )

    def _by_catalog(self, plans: list[dict]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for p in plans:
            out.setdefault(p["catalog_name"], []).append(p)
        return out

    @staticmethod
    def _pred(plans: list[dict], alias: str = "") -> str:
        a = f"{alias}." if alias else ""
        return " OR ".join(
            f"({a}table_schema = {lit(p['schema_name'])} AND {a}table_name = {lit(p['table_name'])} "
            f"AND {a}column_name = {lit(p['column_name'])})"
            for p in plans
        )

    def _precheck(self, plans: list[dict], policy: TagPolicy, errors: dict[str, str]) -> None:
        for cat, cat_plans in self._by_catalog(plans).items():
            if not cat.startswith(self.s.governed_catalog_prefix):
                for p in cat_plans:
                    errors[p["review_item_id"]] = f"PermissionError: catalog {cat} is outside this environment"
                continue
            live = {
                (r["table_schema"], r["table_name"], r["column_name"]): r
                for r in self.rt.rows(
                    f"""SELECT c.table_schema, c.table_name, c.column_name, c.full_data_type, c.comment, t.table_type
                    FROM {qi(cat)}.information_schema.columns c
                    JOIN {qi(cat)}.information_schema.tables t
                      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                    WHERE {self._pred(cat_plans, 'c')}"""
                )
            }
            for p in cat_plans:
                col = live.get(_ident(p))
                err = None
                if col is None:
                    err = "LookupError: column no longer exists (stale metadata); return to Data Governance"
                elif col["table_type"] != p["table_type"]:
                    err = f"ValueError: object type changed from {p['table_type']} to {col['table_type']}"
                elif fingerprint(cat, *_ident(p), col["full_data_type"], col["comment"]) != p["column_fingerprint"]:
                    err = "ValueError: column metadata changed since approval (fingerprint mismatch); the next scan reopens it"
                elif p["action_type"] == c.SET_PHI_TAG and not policy.is_allowed(p["approved_tag_value"]):
                    err = f"ValueError: {p['approved_tag_value']!r} is no longer an allowed value for {self.s.tag_key}"
                if err:
                    errors[p["review_item_id"]] = err

    def _verify(self, plans: list[dict], errors: dict[str, str]) -> dict[str, str | None]:
        verified: dict[str, str | None] = {}
        for cat, cat_plans in self._by_catalog(plans).items():
            tags = {
                (r["schema_name"], r["table_name"], r["column_name"]): r["tag_value"]
                for r in self.rt.rows(
                    f"""SELECT schema_name, table_name, column_name, tag_value
                    FROM {qi(cat)}.information_schema.column_tags
                    WHERE tag_name = {lit(self.s.tag_key)}
                      AND ({self._pred(cat_plans).replace('table_schema', 'schema_name')})"""
                )
            }
            for p in cat_plans:
                actual = tags.get(_ident(p))
                if p["action_type"] == c.SET_PHI_TAG and actual != p["approved_tag_value"]:
                    errors[p["review_item_id"]] = (
                        f"AssertionError: verification expected {p['approved_tag_value']!r}, found {actual!r}"
                    )
                elif p["action_type"] == c.REMOVE_PHI_TAG and actual is not None:
                    errors[p["review_item_id"]] = f"AssertionError: tag still present with value {actual!r}"
                else:
                    verified[p["review_item_id"]] = actual
        return verified

    def _record(self, plans: list[dict], errors: dict[str, str], verified: dict) -> None:
        results = []
        for p in plans:
            iid = p["review_item_id"]
            details = {"application_plan_id": p["application_plan_id"]}
            if iid in errors:
                results.append({"review_item_id": iid, "status": c.APPLICATION_FAILED,
                                "application_error": errors[iid], "applied": False})
                self.rt.event(c.EV_APPLICATION_FAILED, p["review_batch_id"], iid, business_actor=p["reviewed_by"],
                              details={**details, "error": errors[iid]})
                log.error("Plan %s failed: %s", p["application_plan_id"], errors[iid])
            elif p["action_type"] == c.SET_PHI_TAG:
                results.append({"review_item_id": iid, "status": c.TAG_VERIFIED, "application_error": None, "applied": True})
                self.rt.event(c.EV_TAG_VERIFIED, p["review_batch_id"], iid, business_actor=p["reviewed_by"],
                              details={**details, "verified_value": verified.get(iid)})
            else:
                results.append({"review_item_id": iid, "status": c.ATTESTED_NOT_PHI, "application_error": None, "applied": True})
                self.rt.event(c.EV_TAG_REMOVED, p["review_batch_id"], iid, business_actor=p["reviewed_by"], details=details)
        view = self.rt.temp_view(results, RESULT_SCHEMA, "phi_apply_results")
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_ITEM)} t USING {view} s ON t.review_item_id = s.review_item_id
            WHEN MATCHED AND t.status = {lit(c.APPLICATION_IN_PROGRESS)} THEN UPDATE SET
              status = s.status, application_error = s.application_error,
              applied_at = CASE WHEN s.applied THEN current_timestamp() ELSE t.applied_at END,
              {self.rt.audit_update_set()}"""
        )


def main(settings: Settings) -> None:
    settings.validate(expected_role=c.ROLE_APPLICATION)
    Application(Runtime(settings)).run()
