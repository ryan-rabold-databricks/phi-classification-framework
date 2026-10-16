"""<env>_phi_decision_ingestion: turn returned workbooks into validated governance actions.

Flow per returned workbook (processed oldest first):

1. Skip the file when a WORKBOOK_INGESTED event already carries its SHA-256.
2. Parse it into normalized submission records (classification_review_submission).
3. Process the submission set:
   * ASSIGNMENT rows set assigned_steward (STEWARD_ASSIGNED);
   * REVIEW / PRIVACY rows are validated, then produce plans, attestations,
     Privacy routing, VALIDATION_FAILED, or CHANGE_IGNORED_ALREADY_RESOLVED.
4. Write WORKBOOK_INGESTED last, so a crashed run reprocesses the file and the
   deterministic keys make every write a no-op the second time.

``process_submission_set`` does not know where submissions came from; the
Phase 2 App writes the same records and reuses it.
"""

from __future__ import annotations

import hashlib
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
from phi_framework.workbook import parse_workbook

SUBMISSION_SCHEMA = (
    "submission_id string, submission_set_id string, source_type string, source_path string, "
    "submission_type string, review_batch_id string, review_item_id string, source_row_number int, "
    "assigned_steward string, decision string, corrected_tag_value string, steward_comment string, "
    "reviewed_by string, reviewed_at_raw string, submitted_at timestamp"
)
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
        self.policy = TagPolicy(rt.w, self.s.tag_key)
        self.groups = GroupDirectory(rt.w)
        self.failures: list[str] = []

    # ------------------------------------------------------------------
    def run(self) -> None:
        files = self._list_inbound()
        seen = {
            r["discriminator"]
            for r in self.rt.rows(
                f"SELECT DISTINCT discriminator FROM {self.rt.t(c.T_EVENT)} "
                f"WHERE event_type = {lit(c.EV_WORKBOOK_INGESTED)} AND discriminator IS NOT NULL"
            )
        }
        processed = 0
        for path in files:
            data = self.rt.w.files.download(path).contents.read()
            sha = hashlib.sha256(data).hexdigest()
            if sha in seen:
                continue
            try:
                parsed = parse_workbook(data)
            except Exception as exc:
                msg = f"{path}: {exc}"
                log.error("Rejected workbook %s", msg)
                self.failures.append(msg)
                continue
            self._write_submissions(sha, path, parsed)
            result = self.process_submission_set(sha, parsed.workbook_type, parsed.review_batch_id)
            self.rt.event(
                c.EV_WORKBOOK_INGESTED,
                parsed.review_batch_id,
                details={
                    "path": path,
                    "workbook_type": parsed.workbook_type,
                    "audience": parsed.meta.get("audience"),
                    "export_content_hash": parsed.meta.get("content_hash"),
                    "rows": len(parsed.rows),
                    **result.counts,
                },
                discriminator=sha,
            )
            self.rt.flush_events()
            seen.add(sha)
            processed += 1
        close_completed_batches(self.rt)
        self.rt.flush_events()
        log.info("Ingested %d new workbook(s)", processed)
        if self.failures:
            raise RuntimeError("Some workbooks were rejected:\n" + "\n".join(self.failures))

    def _list_inbound(self) -> list[str]:
        found: list[tuple] = []
        stack = [self.s.inbound_path]
        while stack:
            d = stack.pop()
            for e in self.rt.w.files.list_directory_contents(d):
                if e.is_directory:
                    stack.append(e.path)
                elif e.path.lower().endswith(".xlsx") and not e.name.startswith("~$"):
                    found.append((e.last_modified or 0, e.path))
        return [p for _, p in sorted(found)]

    def _write_submissions(self, sha: str, path: str, parsed) -> None:
        now = utcnow()
        rows = []
        for r in parsed.rows:
            v = r.values
            rows.append(
                {
                    "submission_id": make_key("SUB", [sha, r.review_item_id]),
                    "submission_set_id": sha,
                    "source_type": c.SOURCE_WORKBOOK,
                    "source_path": path,
                    "submission_type": parsed.workbook_type,
                    "review_batch_id": parsed.review_batch_id,
                    "review_item_id": r.review_item_id,
                    "source_row_number": r.row_number,
                    "assigned_steward": (v.get("assigned_steward") or None) and v["assigned_steward"].lower(),
                    "decision": v.get("decision"),
                    "corrected_tag_value": v.get("corrected_tag_value"),
                    "steward_comment": v.get("steward_comment"),
                    "reviewed_by": v.get("reviewed_by"),
                    "reviewed_at_raw": v.get("reviewed_at"),
                    "submitted_at": now,
                }
            )
        if not rows:
            return
        view = self.rt.temp_view(rows, SUBMISSION_SCHEMA, "phi_submissions")
        cols = [f.split()[0] for f in SUBMISSION_SCHEMA.split(", ")]
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_SUBMISSION)} t USING {view} s ON t.submission_id = s.submission_id
            WHEN NOT MATCHED THEN INSERT {self.rt.insert_values_clause('s', cols, created_only=True)}"""
        )

    # ------------------------------------------------------------------
    def process_submission_set(self, set_id: str, submission_type: str, batch_id: str) -> SetResult:
        subs = self.rt.rows(
            f"SELECT * FROM {self.rt.t(c.T_SUBMISSION)} WHERE submission_set_id = {lit(set_id)} ORDER BY source_row_number"
        )
        items = {
            r["review_item_id"]: r
            for r in self.rt.rows(
                f"""SELECT i.*, b.created_at AS batch_created_at, s.steward_group, s.risk_tier
                FROM {self.rt.t(c.T_ITEM)} i
                JOIN {self.rt.t(c.T_BATCH)} b ON b.review_batch_id = i.review_batch_id
                JOIN {self.rt.t(c.T_SCOPE)} s ON s.scope_id = b.scope_id
                JOIN (SELECT DISTINCT review_item_id FROM {self.rt.t(c.T_SUBMISSION)}
                      WHERE submission_set_id = {lit(set_id)}) x ON x.review_item_id = i.review_item_id"""
            )
        }
        if submission_type == c.WB_ASSIGNMENT:
            return self._process_assignments(set_id, subs, items)
        return self._process_decisions(set_id, subs, items)

    def _reject(self, set_id: str, sub: dict, errors: list[str], kind: str) -> None:
        self.rt.event(
            c.EV_VALIDATION_FAILED,
            sub["review_batch_id"],
            sub["review_item_id"],
            business_actor=(sub.get("reviewed_by") or "").lower() or None,
            details={"kind": kind, "errors": errors, "submitted": _submitted(sub)},
            discriminator=set_id,
        )

    # ---------- assignments ----------
    def _process_assignments(self, set_id: str, subs: list[dict], items: dict) -> SetResult:
        updates: list[tuple[str, str]] = []
        counts = {"assigned": 0, "unchanged": 0, "rejected": 0, "blank": 0}
        for sub in subs:
            steward = sub.get("assigned_steward")
            if not steward:
                counts["blank"] += 1
                continue
            it = items.get(sub["review_item_id"])
            errors = []
            if it is None or it["review_batch_id"] != sub["review_batch_id"]:
                errors.append("Unknown review item for this batch")
            elif it["status"] not in (c.PENDING_STEWARD_REVIEW, c.VALIDATION_FAILED):
                errors.append(f"Item status {it['status']} does not accept assignment")
            elif not self.groups.is_member(steward, it["steward_group"]):
                errors.append(f"{steward} is not a member of steward group {it['steward_group']}")
            if errors:
                counts["rejected"] += 1
                self._reject(set_id, sub, errors, "ASSIGNMENT")
                continue
            if (it["assigned_steward"] or "").lower() == steward:
                counts["unchanged"] += 1
                continue
            updates.append((it["review_item_id"], steward))
            counts["assigned"] += 1
            self.rt.event(
                c.EV_STEWARD_ASSIGNED,
                it["review_batch_id"],
                it["review_item_id"],
                details={"previous_steward": it["assigned_steward"], "assigned_steward": steward},
                discriminator=set_id,
            )
        if updates:
            view = self.rt.temp_view(
                [{"review_item_id": i, "assigned_steward": s} for i, s in updates],
                "review_item_id string, assigned_steward string",
                "phi_assign",
            )
            self.rt.sql(
                f"""MERGE INTO {self.rt.t(c.T_ITEM)} t USING {view} s ON t.review_item_id = s.review_item_id
                WHEN MATCHED AND t.status IN ({in_list([c.PENDING_STEWARD_REVIEW, c.VALIDATION_FAILED])})
                THEN UPDATE SET assigned_steward = s.assigned_steward, {self.rt.audit_update_set()}"""
            )
        return SetResult(counts)

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
                reviewed_at_raw=sub.get("reviewed_at_raw"),
                submission_type=sub["submission_type"],
                review_batch_id=sub["review_batch_id"],
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
            if privacy_item != (s.submission_type == c.WB_PRIVACY):
                counts["failed"] += 1
                reason = (
                    "Item is in Privacy review; only a PRIVACY workbook can record its outcome"
                    if privacy_item
                    else "Item is not in Privacy review; record the decision in a REVIEW workbook"
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
            )
            out = validate_decision(
                ctx, s, self.policy.is_allowed, self.groups.is_member, self.s.governance_group, now
            )
            if not out.ok:
                counts["failed"] += 1
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
                details={"decision": out.decision, "corrected_tag_value": out.corrected_tag_value},
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
    return {k: sub.get(k) for k in ("decision", "corrected_tag_value", "steward_comment", "reviewed_by", "reviewed_at_raw", "source_row_number")}


def _recorded(it: dict) -> dict:
    return {k: it.get(k) for k in ("decision", "corrected_tag_value", "steward_comment", "reviewed_by", "reviewed_at")}


def main(settings: Settings) -> None:
    settings.validate(expected_role=c.ROLE_INGESTION)
    Ingestion(Runtime(settings)).run()
