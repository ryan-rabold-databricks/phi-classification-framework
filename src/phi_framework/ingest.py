"""<env>_phi_decision_ingestion: turn review App submissions into validated governance actions.

Triggered when the review App adds rows to classification_review_submission.
Each App submit action writes one submission set per batch. Ingestion processes
every set that has no SUBMISSION_PROCESSED event yet, oldest first:

1. Validate each submission (reviewer authorization, decision rules, allowed values).
2. Produce plans, attestations, Privacy routing, VALIDATION_FAILED, or
   CHANGE_IGNORED_ALREADY_RESOLVED.
3. Write SUBMISSION_PROCESSED last, so a crashed run reprocesses the set and the
   deterministic keys make every write a no-op the second time.

Submissions are never changed; outcomes are recorded on the review item and in events.
"""

from __future__ import annotations

from dataclasses import dataclass

from phi_framework import constants as c
from phi_framework.batches import close_completed_batches
from phi_framework.config import Settings
from phi_framework.directory import GroupDirectory, TagPolicy
from phi_framework.keys import make_key
from phi_framework.runtime import Runtime, log, utcnow
from phi_framework.sqlutil import in_list, lit, qi
from phi_framework.tag_ddl import render_tag_ddl
from phi_framework.validation import ItemContext, Submission, submitted_differs, validate_decision

ITEM_UPDATE_SCHEMA = (
    "review_item_id string, status string, decision string, corrected_tag_value string, steward_comment string, "
    "reviewed_by string, reviewed_at timestamp"
)
PLAN_SCHEMA = (
    "application_plan_id string, review_batch_id string, review_item_id string, action_type string, "
    "catalog_name string, schema_name string, table_name string, table_type string, column_name string, "
    "column_fingerprint string, approved_tag_value string, reviewed_by string, reviewed_at timestamp, rendered_ddl string"
)
ATTESTATION_SCHEMA = (
    "attestation_id string, review_batch_id string, review_item_id string, catalog_name string, schema_name string, "
    "table_name string, column_name string, column_fingerprint string, attestation_type string, rationale string, "
    "attested_by string, risk_tier string"
)


@dataclass
class SetResult:
    counts: dict


class Ingestion:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s: Settings = rt.s
        self.policy = TagPolicy(rt.w, self.s.tag_key, self.s.allowed_values_override)
        self.groups = GroupDirectory(rt.w)

    # ------------------------------------------------------------------
    def run(self) -> None:
        sets = self.rt.rows(
            f"""SELECT submission_set_id, first(submission_type) AS submission_type,
                   first(review_batch_id) AS review_batch_id, count(*) AS n,
                   count(DISTINCT review_batch_id) AS batches, count(DISTINCT submission_type) AS types
            FROM {self.rt.t(c.T_SUBMISSION)} s
            WHERE NOT EXISTS (
              SELECT 1 FROM {self.rt.t(c.T_EVENT)} e
              WHERE e.event_type = {lit(c.EV_SUBMISSION_PROCESSED)} AND e.discriminator = s.submission_set_id)
            GROUP BY submission_set_id
            ORDER BY min(audit_created_at), submission_set_id"""
        )
        for st in sets:
            set_id = st["submission_set_id"]
            if st["batches"] != 1 or st["types"] != 1:
                # The App writes one set per batch and submission type; anything else is rejected whole.
                log.error("Submission set %s mixes batches or types; rejected", set_id)
                for sub in self._set_rows(set_id):
                    self._reject(set_id, sub, ["Submission set mixes batches or submission types"], "DECISION")
                result = SetResult({"rejected_set": st["n"]})
            else:
                result = self.process_submission_set(set_id)
            self.rt.event(
                c.EV_SUBMISSION_PROCESSED,
                st["review_batch_id"],
                details={"submission_type": st["submission_type"], "rows": st["n"], **result.counts},
                discriminator=set_id,
            )
            self.rt.flush_events()
        close_completed_batches(self.rt)
        self.rt.flush_events()
        log.info("Processed %d submission set(s)", len(sets))

    def _set_rows(self, set_id: str) -> list[dict]:
        return self.rt.rows(
            f"SELECT * FROM {self.rt.t(c.T_SUBMISSION)} WHERE submission_set_id = {lit(set_id)} ORDER BY review_item_id"
        )

    # ------------------------------------------------------------------
    def process_submission_set(self, set_id: str) -> SetResult:
        subs = self._set_rows(set_id)
        items = {
            r["review_item_id"]: r
            for r in self.rt.rows(
                f"""SELECT i.*, b.created_at AS batch_created_at, s.steward_group, s.governance_owner, s.risk_tier
                FROM {self.rt.t(c.T_ITEM)} i
                JOIN {self.rt.t(c.T_BATCH)} b ON b.review_batch_id = i.review_batch_id
                JOIN {self.rt.t(c.T_SCOPE)} s ON s.scope_id = b.scope_id
                JOIN (SELECT DISTINCT review_item_id FROM {self.rt.t(c.T_SUBMISSION)}
                      WHERE submission_set_id = {lit(set_id)}) x ON x.review_item_id = i.review_item_id"""
            )
        }
        return self._process_decisions(set_id, subs, items)

    def _reject(self, set_id: str, sub: dict, errors: list[str], kind: str) -> None:
        self.rt.event(
            c.EV_VALIDATION_FAILED,
            sub["review_batch_id"],
            sub["review_item_id"],
            business_actor=(sub.get("reviewed_by") or "").lower() or None,
            details={"kind": kind, "submission_type": sub.get("submission_type"), "errors": errors,
                     "submitted": _submitted(sub)},
            discriminator=set_id,
        )

    # ---------- decisions ----------
    def _process_decisions(self, set_id: str, subs: list[dict], items: dict) -> SetResult:
        now = utcnow()
        item_updates, plans, attestations = [], [], []
        counts = {k: 0 for k in ("planned", "attested", "privacy", "failed", "ignored_change", "blank", "unchanged")}
        not_phi_items = []
        for sub in subs:
            s = Submission(
                decision=sub.get("decision"),
                corrected_tag_value=sub.get("corrected_tag_value"),
                steward_comment=sub.get("steward_comment"),
                reviewed_by=sub.get("reviewed_by"),
                reviewed_at=sub.get("reviewed_at"),
                submission_type=sub["submission_type"],
                review_batch_id=sub["review_batch_id"],
                suggested_tag_value_seen=sub.get("suggested_tag_value_seen"),
            )
            if not any((s.decision, s.corrected_tag_value, s.steward_comment)):
                counts["blank"] += 1
                continue
            it = items.get(sub["review_item_id"])
            if it is None or it["review_batch_id"] != sub["review_batch_id"]:
                counts["failed"] += 1
                self._reject(set_id, sub, ["Unknown review item for this batch"], "DECISION")
                continue
            status = it["status"]
            if status in c.DECISION_ACCEPTED_STATUSES:
                if submitted_differs(it, s):
                    counts["ignored_change"] += 1
                    self.rt.event(
                        c.EV_CHANGE_IGNORED,
                        it["review_batch_id"],
                        it["review_item_id"],
                        business_actor=(s.reviewed_by or "").lower() or None,
                        details={"status": status, "submitted": _submitted(sub), "recorded": _recorded(it)},
                        discriminator=set_id,
                    )
                else:
                    counts["unchanged"] += 1
                continue
            if status not in c.DECISION_ELIGIBLE_STATUSES:
                counts["unchanged"] += 1
                continue
            privacy_item = status == c.NEEDS_PRIVACY_REVIEW
            if privacy_item and (s.decision or "").strip().upper() == c.REQUEST_PRIVACY_REVIEW and not submitted_differs(it, s):
                counts["unchanged"] += 1  # resubmitted copy of the original request
                continue
            if privacy_item != (s.submission_type == c.SUB_PRIVACY):
                counts["failed"] += 1
                reason = (
                    "Item is in Privacy review; record its outcome from the Privacy review queue"
                    if privacy_item
                    else "Item is not in Privacy review; record the decision from the review queue"
                )
                self._reject(set_id, sub, [reason], "DECISION")  # status unchanged
                continue

            ctx = ItemContext(
                review_item_id=it["review_item_id"],
                review_batch_id=it["review_batch_id"],
                status=status,
                suggested_tag_value=it["suggested_tag_value"],
                batch_created_at=it["batch_created_at"],
                steward_group=it["steward_group"],
                governance_owner=it["governance_owner"],
            )
            out = validate_decision(
                ctx, s, self.policy.is_allowed, self.groups.includes, self.s.governance_group, now
            )
            if not out.ok:
                counts["failed"] += 1
                if privacy_item:
                    # A rejected Privacy outcome must not take the item out of Privacy review;
                    # record the rejection only (the App shows the reason on the Privacy queue).
                    self._reject(set_id, sub, list(out.errors), "DECISION")
                    continue
                item_updates.append(
                    {
                        "review_item_id": it["review_item_id"],
                        "status": c.VALIDATION_FAILED,
                        "decision": out.decision if out.decision in c.DECISIONS else None,
                        "corrected_tag_value": out.corrected_tag_value,
                        "steward_comment": out.comment,
                        "reviewed_by": out.reviewed_by,
                        "reviewed_at": out.reviewed_at,
                    }
                )
                self._reject(set_id, sub, list(out.errors), "DECISION")
                continue

            self.rt.event(
                c.EV_DECISION_RECEIVED,
                it["review_batch_id"],
                it["review_item_id"],
                business_actor=out.reviewed_by,
                details={"decision": out.decision, "corrected_tag_value": out.corrected_tag_value,
                         "acting_as": out.acting_as},
                discriminator=set_id,
            )
            update = {
                "review_item_id": it["review_item_id"],
                "decision": out.decision,
                "corrected_tag_value": out.corrected_tag_value,
                "steward_comment": out.comment,
                "reviewed_by": out.reviewed_by,
                "reviewed_at": out.reviewed_at,
            }
            if out.decision in (c.APPROVE_SUGGESTION, c.CORRECT_CLASSIFICATION):
                plans.append(self._plan(it, c.SET_PHI_TAG, out.approved_value, out))
                item_updates.append({**update, "status": c.PLANNED})
                counts["planned"] += 1
            elif out.decision == c.REQUEST_PRIVACY_REVIEW:
                item_updates.append({**update, "status": c.NEEDS_PRIVACY_REVIEW})
                counts["privacy"] += 1
                self.rt.event(
                    c.EV_PRIVACY_REVIEW_REQUESTED,
                    it["review_batch_id"],
                    it["review_item_id"],
                    business_actor=out.reviewed_by,
                    details={"question": out.comment},
                    discriminator=set_id,
                )
            else:  # CONFIRM_NOT_PHI
                not_phi_items.append((it, out, update))

        if not_phi_items:
            tagged = self._currently_tagged([it for it, _, _ in not_phi_items])
            for it, out, update in not_phi_items:
                attestations.append(
                    {
                        "attestation_id": make_key("ATT", [it["review_item_id"]]),
                        "review_batch_id": it["review_batch_id"],
                        "review_item_id": it["review_item_id"],
                        "catalog_name": it["catalog_name"],
                        "schema_name": it["schema_name"],
                        "table_name": it["table_name"],
                        "column_name": it["column_name"],
                        "column_fingerprint": it["column_fingerprint"],
                        "attestation_type": c.CONFIRMED_NOT_PHI,
                        "rationale": out.comment,
                        "attested_by": out.reviewed_by,
                        "risk_tier": it["risk_tier"],
                    }
                )
                ident = (it["catalog_name"], it["schema_name"], it["table_name"], it["column_name"])
                if ident in tagged:
                    plans.append(self._plan(it, c.REMOVE_PHI_TAG, None, out))
                    item_updates.append({**update, "status": c.PLANNED})
                    counts["planned"] += 1
                else:
                    item_updates.append({**update, "status": c.ATTESTED_NOT_PHI})
                counts["attested"] += 1

        # Write order: plans and attestations (insert-only) before item status, events last.
        self._write_plans(plans, set_id)
        self._write_attestations(attestations, set_id)
        self._write_item_updates(item_updates)
        return SetResult(counts)

    def _plan(self, it: dict, action: str, value: str | None, out) -> dict:
        ddl = render_tag_ddl(
            action, it["table_type"], it["catalog_name"], it["schema_name"], it["table_name"], it["column_name"],
            self.s.tag_key, value,
        )
        return {
            "application_plan_id": make_key("PLAN", [it["review_item_id"]]),
            "review_batch_id": it["review_batch_id"],
            "review_item_id": it["review_item_id"],
            "action_type": action,
            "catalog_name": it["catalog_name"],
            "schema_name": it["schema_name"],
            "table_name": it["table_name"],
            "table_type": it["table_type"],
            "column_name": it["column_name"],
            "column_fingerprint": it["column_fingerprint"],
            "approved_tag_value": value,
            "reviewed_by": out.reviewed_by,
            "reviewed_at": out.reviewed_at,
            "rendered_ddl": ddl,
        }

    def _currently_tagged(self, items: list[dict]) -> set[tuple]:
        result: set[tuple] = set()
        by_cat: dict[str, list[dict]] = {}
        for it in items:
            by_cat.setdefault(it["catalog_name"], []).append(it)
        for cat, its in by_cat.items():
            preds = " OR ".join(
                f"(schema_name = {lit(i['schema_name'])} AND table_name = {lit(i['table_name'])} AND column_name = {lit(i['column_name'])})"
                for i in its
            )
            for r in self.rt.rows(
                f"""SELECT catalog_name, schema_name, table_name, column_name FROM {qi(cat)}.information_schema.column_tags
                WHERE tag_name = {lit(self.s.tag_key)} AND ({preds})"""
            ):
                result.add((r["catalog_name"], r["schema_name"], r["table_name"], r["column_name"]))
        return result

    def _write_plans(self, plans: list[dict], set_id: str) -> None:
        if not plans:
            return
        view = self.rt.temp_view(plans, PLAN_SCHEMA, "phi_plans")
        cols = [f.split()[0] for f in PLAN_SCHEMA.split(", ")]
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_PLAN)} t USING {view} s ON t.application_plan_id = s.application_plan_id
            WHEN NOT MATCHED THEN INSERT {self.rt.insert_values_clause('s', cols, created_only=True)}"""
        )
        for p in plans:
            self.rt.event(
                c.EV_PLAN_CREATED,
                p["review_batch_id"],
                p["review_item_id"],
                business_actor=p["reviewed_by"],
                details={"application_plan_id": p["application_plan_id"], "action_type": p["action_type"],
                         "approved_tag_value": p["approved_tag_value"]},
                discriminator=set_id,
            )

    def _write_attestations(self, atts: list[dict], set_id: str) -> None:
        if not atts:
            return
        view = self.rt.temp_view(atts, ATTESTATION_SCHEMA, "phi_atts")
        months = f"CASE n.risk_tier WHEN 'HIGH' THEN {c.RISK_TIERS['HIGH']} ELSE {c.RISK_TIERS['STANDARD']} END"
        audit = self.rt.audit_insert_exprs()
        # One MERGE inserts the new attestation and supersedes the column's prior active one,
        # so each column has at most one active attestation (single-table transaction).
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_ATTESTATION)} t
            USING (
              SELECT n.attestation_id AS merge_id, 'insert' AS action, n.*, current_timestamp() AS attested_at,
                     timestampadd(MONTH, {months}, current_timestamp()) AS expires_at
              FROM {view} n
              UNION ALL
              SELECT a.attestation_id AS merge_id, 'supersede' AS action, n.*, current_timestamp() AS attested_at,
                     CAST(NULL AS TIMESTAMP) AS expires_at
              FROM {view} n
              JOIN {self.rt.t(c.T_ATTESTATION)} a
                ON a.catalog_name = n.catalog_name AND a.schema_name = n.schema_name
               AND a.table_name = n.table_name AND a.column_name = n.column_name
               AND a.superseded_at IS NULL AND a.attestation_id <> n.attestation_id
            ) s ON t.attestation_id = s.merge_id
            WHEN MATCHED AND s.action = 'supersede' AND t.superseded_at IS NULL
              THEN UPDATE SET superseded_at = s.attested_at, {self.rt.audit_update_set()}
            WHEN NOT MATCHED AND s.action = 'insert' THEN INSERT
              (attestation_id, review_batch_id, review_item_id, catalog_name, schema_name, table_name, column_name,
               column_fingerprint, attestation_type, rationale, attested_by, attested_at, expires_at, superseded_at,
               {', '.join(audit)})
              VALUES (s.attestation_id, s.review_batch_id, s.review_item_id, s.catalog_name, s.schema_name,
               s.table_name, s.column_name, s.column_fingerprint, s.attestation_type, s.rationale, s.attested_by,
               s.attested_at, s.expires_at, NULL, {', '.join(audit.values())})"""
        )
        for a in atts:
            self.rt.event(
                c.EV_ATTESTATION_RECORDED,
                a["review_batch_id"],
                a["review_item_id"],
                business_actor=a["attested_by"],
                details={"attestation_id": a["attestation_id"], "rationale": a["rationale"]},
                discriminator=set_id,
            )

    def _write_item_updates(self, updates: list[dict]) -> None:
        if not updates:
            return
        view = self.rt.temp_view(updates, ITEM_UPDATE_SCHEMA, "phi_item_updates")
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_ITEM)} t USING {view} s ON t.review_item_id = s.review_item_id
            WHEN MATCHED AND t.status IN ({in_list(c.DECISION_ELIGIBLE_STATUSES)}) THEN UPDATE SET
              status = s.status, decision = s.decision, corrected_tag_value = s.corrected_tag_value,
              steward_comment = s.steward_comment, reviewed_by = s.reviewed_by, reviewed_at = s.reviewed_at,
              {self.rt.audit_update_set()}"""
        )


def _submitted(sub: dict) -> dict:
    return {k: sub.get(k) for k in ("submission_id", "decision", "corrected_tag_value", "steward_comment",
                                     "suggested_tag_value_seen", "reviewed_by", "reviewed_at")}


def _recorded(it: dict) -> dict:
    return {k: it.get(k) for k in ("decision", "corrected_tag_value", "steward_comment", "reviewed_by", "reviewed_at")}


def main(settings: Settings) -> None:
    settings.validate(expected_role=c.ROLE_INGESTION)
    Ingestion(Runtime(settings)).run()
