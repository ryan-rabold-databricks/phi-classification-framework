"""<env>_phi_classification_scan: find actionable classification issues and open review items.

The scan reads only metadata (information_schema, tag policy, Data
Classification results) and the governance tables. It never reads table data.

Retry safety: every key is derived from the job run id, findings are staged
once per run in ``scan_findings_stage``, all inserts are insert-only MERGEs,
and events are derived from rows this run wrote.
"""

from __future__ import annotations

from phi_framework import constants as c
from phi_framework.batches import close_completed_batches, due_at_utc
from phi_framework.config import Settings
from phi_framework.directory import TagPolicy
from phi_framework.keys import fingerprint_sql, make_key, make_key_sql
from phi_framework.runtime import Runtime, log, utcnow
from phi_framework.sqlutil import in_list, lit, qi

STAGE_TABLE = "scan_findings_stage"
IDENTITY = ["catalog_name", "schema_name", "table_name", "column_name"]


class ScopeConflictError(RuntimeError):
    pass


def _on(a: str, b: str) -> str:
    return " AND ".join(f"{a}.{k} = {b}.{k}" for k in IDENTITY)


class Scan:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s: Settings = rt.s
        self.now = utcnow()
        self.correction: tuple[str, str, str, str] | None = None

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._parse_correction()
        policy = TagPolicy(self.rt.w, self.s.tag_key, self.s.allowed_values_override)
        scopes = self._active_scopes()
        if not scopes:
            log.warning("No active scopes for environment %s; nothing to scan", self.s.env)
            return
        self._check_conflicts(scopes)
        self._register_reference_views(scopes, policy)
        if not self.correction:
            self._close_superseded_failed_items()
        self._stage_findings(scopes)
        if self.correction and not self._prepare_correction():
            return
        self._update_open_items()
        self._open_new_items()
        if not self.correction:
            self._close_out_of_scope_items()
        close_completed_batches(self.rt)
        self.rt.flush_events()

    # ------------------------------------------------------------------
    def _parse_correction(self) -> None:
        if not self.s.correction_column:
            return
        parts = [p.strip().strip("`") for p in self.s.correction_column.split(".")]
        if len(parts) != 4 or not all(parts):
            raise ValueError("correction_column must be catalog.schema.table.column")
        if not self.s.correction_reason:
            raise ValueError("correction_reason is required for a correction request")
        # The review App verifies the requester is in Data Governance (from their own groups) before it
        # starts this run; only Data Governance, Data Platform, and the App can run the scan job.
        if "@" not in self.s.correction_requested_by:
            raise ValueError("correction_requested_by must be the requesting user's email")
        self.correction = tuple(parts)  # type: ignore[assignment]
        log.info("Correction request for %s by %s", ".".join(parts), self.s.correction_requested_by)

    def _active_scopes(self) -> list[dict]:
        rows = self.rt.rows(
            f"""SELECT * FROM {self.rt.t(c.T_SCOPE)}
            WHERE environment = {lit(self.s.env)} AND scan_enabled = true
              AND current_date() BETWEEN effective_date AND coalesce(expiration_date, DATE'9999-12-31')"""
        )
        for r in rows:  # environment guardrail (also enforced by a CHECK constraint)
            if not r["catalog_name"].startswith(self.s.governed_catalog_prefix):
                raise ValueError(
                    f"Scope {r['scope_id']} targets {r['catalog_name']}, which is outside this environment "
                    f"(prefix {self.s.governed_catalog_prefix})"
                )
        if self.correction:
            cat, sch = self.correction[0], self.correction[1]
            rows = [r for r in rows if r["catalog_name"] == cat and r["schema_name"] in ("*", sch)]
            if not rows:
                raise ValueError(f"{'.'.join(self.correction)} is not in an active scope")
        return rows

    def _check_conflicts(self, scopes: list[dict]) -> None:
        groups: dict[tuple[str, str], list[str]] = {}
        for r in scopes:
            groups.setdefault((r["catalog_name"], r["schema_name"]), []).append(r["scope_id"])
        conflicts = {k: v for k, v in groups.items() if len(v) > 1}
        if not conflicts:
            return
        msg = "; ".join(f"{cat}.{sch}: {', '.join(sorted(ids))}" for (cat, sch), ids in sorted(conflicts.items()))
        message = f"Scope registry conflict, scan stopped without opening items: {msg}"
        log.error(message)
        due = due_at_utc(self.now, self.s.review_due_business_days, self.s.business_timezone)
        ids = sorted({i for v in conflicts.values() for i in v})
        values = ", ".join(
            f"({lit(sid)}, {make_key_sql('BATCH', lit(self.s.run_id), lit(sid))})" for sid in ids
        )
        audit = self.rt.audit_insert_exprs()
        # Error batches are recorded CLOSED (no items) so they never hold the SLA metrics open.
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_BATCH)} t
            USING (SELECT col1 AS scope_id, col2 AS review_batch_id FROM VALUES {values}) s
            ON t.review_batch_id = s.review_batch_id
            WHEN MATCHED THEN UPDATE SET last_error = {lit(message)}, {self.rt.audit_update_set()}
            WHEN NOT MATCHED THEN INSERT (review_batch_id, scope_id, scan_run_id, status, created_at, due_at, closed_at,
                last_error, {', '.join(audit)})
            VALUES (s.review_batch_id, s.scope_id, {lit(self.s.run_id)}, {lit(c.BATCH_CLOSED)}, {lit(self.now)},
                {lit(due)}, {lit(self.now)}, {lit(message)}, {', '.join(audit.values())})"""
        )
        for sid in ids:
            self.rt.event(c.EV_BATCH_ERROR, make_key("BATCH", [self.s.run_id, sid]), details={"error": message})
        self.rt.flush_events()
        raise ScopeConflictError(message)

    # ------------------------------------------------------------------
    def _register_reference_views(self, scopes: list[dict], policy: TagPolicy) -> None:
        sp = self.rt.spark
        run = self.s.run_id
        from pyspark.sql.types import StringType, StructField, StructType

        sp.createDataFrame([(v,) for v in sorted(policy.allowed)], StructType([StructField("value", StringType(), False)])).createOrReplaceTempView("phi_allowed_values")
        scope_rows = [
            (r["scope_id"], r["catalog_name"], r["schema_name"], r["risk_tier"], r["steward_group"]) for r in scopes
        ]
        sp.createDataFrame(
            scope_rows,
            "scope_id string, catalog_name string, schema_name string, risk_tier string, steward_group string",
        ).createOrReplaceTempView("phi_scopes")

        gov_cat, gov_sch = self.s.governance_catalog, self.s.governance_schema
        excluded = f"AND NOT c.table_name RLIKE {lit(self.s.excluded_table_regex)}" if self.s.excluded_table_regex else ""
        col_parts, tag_parts = [], []
        for cat in sorted({r["catalog_name"] for r in scopes}):
            cat_scopes = [r for r in scopes if r["catalog_name"] == cat]
            schema_filter = ""
            if not any(r["schema_name"] == "*" for r in cat_scopes):
                schema_filter = f"AND c.table_schema IN ({in_list(r['schema_name'] for r in cat_scopes)})"
            if self.correction:
                schema_filter += (
                    f" AND c.table_schema = {lit(self.correction[1])} AND c.table_name = {lit(self.correction[2])}"
                    f" AND c.column_name = {lit(self.correction[3])}"
                )
            fp = fingerprint_sql("c.table_catalog", "c.table_schema", "c.table_name", "c.column_name", "c.full_data_type", "c.comment")
            col_parts.append(
                f"""SELECT s.scope_id, s.risk_tier, s.steward_group,
                    c.table_catalog AS catalog_name, c.table_schema AS schema_name, c.table_name, t.table_type,
                    c.column_name, c.full_data_type, c.comment AS column_comment, {fp} AS column_fingerprint
                FROM {qi(cat)}.information_schema.columns c
                JOIN {qi(cat)}.information_schema.tables t
                  ON t.table_catalog = c.table_catalog AND t.table_schema = c.table_schema AND t.table_name = c.table_name
                JOIN phi_scopes s
                  ON s.catalog_name = c.table_catalog AND (s.schema_name = c.table_schema OR s.schema_name = '*')
                WHERE c.table_schema <> 'information_schema'
                  AND NOT (c.table_catalog = {lit(gov_cat)} AND c.table_schema = {lit(gov_sch)})
                  AND t.table_type IN ({in_list(c.TABLE_TYPES)})
                  {excluded}
                  {schema_filter}
                QUALIFY row_number() OVER (
                  PARTITION BY c.table_catalog, c.table_schema, c.table_name, c.column_name
                  ORDER BY CASE WHEN s.schema_name = '*' THEN 1 ELSE 0 END) = 1"""
            )
            tag_parts.append(
                f"""SELECT catalog_name, schema_name, table_name, column_name, tag_value
                FROM {qi(cat)}.information_schema.column_tags WHERE tag_name = {lit(self.s.tag_key)}"""
            )
        sp.sql(" UNION ALL ".join(col_parts)).createOrReplaceTempView("phi_in_scope_columns")
        sp.sql(" UNION ALL ".join(tag_parts)).createOrReplaceTempView("phi_direct_tags")

        # Data Classification suggestions: HIGH confidence, latest detection only, one unambiguous mapped value.
        catalogs = in_list(sorted({r["catalog_name"] for r in scopes}))
        if self.s.data_classification_table:
            sp.sql(
                f"""WITH latest AS (
                  SELECT catalog_name, schema_name, table_name, column_name, trim(class_tag) AS class_tag,
                         confidence, frequency, latest_detected_time
                  FROM {self.s.data_classification_table}
                  WHERE catalog_name IN ({catalogs}) AND confidence = 'HIGH'
                  QUALIFY rank() OVER (PARTITION BY catalog_name, schema_name, table_name, column_name
                                       ORDER BY latest_detected_time DESC) = 1
                ), mapped AS (
                  SELECT l.*, a.value AS phi_tag_value
                  FROM latest l
                  LEFT JOIN {self.rt.t(c.T_MAPPING)} m ON m.class_tag = l.class_tag AND m.is_active
                  LEFT JOIN phi_allowed_values a ON a.value = m.phi_tag_value
                )
                SELECT catalog_name, schema_name, table_name, column_name,
                       first(phi_tag_value) AS suggested_tag_value,
                       array_join(collect_list(concat_ws('; ', class_tag, confidence,
                           format_number(frequency, 2), CAST(latest_detected_time AS STRING))), ' | ') AS suggestion_reason
                FROM mapped
                GROUP BY catalog_name, schema_name, table_name, column_name
                -- Every latest HIGH detection must map to the same allowed value; an unmapped detection blocks it.
                HAVING count_if(phi_tag_value IS NULL) = 0 AND count(DISTINCT phi_tag_value) = 1"""
                # The samples column is never selected.
            ).createOrReplaceTempView("phi_suggestions")
        else:
            sp.sql(
                "SELECT CAST(NULL AS STRING) catalog_name, CAST(NULL AS STRING) schema_name, CAST(NULL AS STRING) table_name, "
                "CAST(NULL AS STRING) column_name, CAST(NULL AS STRING) suggested_tag_value, CAST(NULL AS STRING) suggestion_reason "
                "WHERE false"
            ).createOrReplaceTempView("phi_suggestions")

        # Active attestations and the latest verified tag evidence per column.
        sp.sql(
            f"""SELECT * FROM {self.rt.t(c.T_ATTESTATION)}
            WHERE attestation_type = {lit(c.CONFIRMED_NOT_PHI)} AND superseded_at IS NULL"""
        ).createOrReplaceTempView("phi_active_attestations")
        sp.sql(
            f"""SELECT i.catalog_name, i.schema_name, i.table_name, i.column_name, i.review_item_id,
                   i.reviewed_at, p.column_fingerprint, p.approved_tag_value
            FROM {self.rt.t(c.T_ITEM)} i
            JOIN {self.rt.t(c.T_PLAN)} p ON p.review_item_id = i.review_item_id AND p.action_type = {lit(c.SET_PHI_TAG)}
            WHERE i.status = {lit(c.TAG_VERIFIED)}
            QUALIFY row_number() OVER (PARTITION BY i.catalog_name, i.schema_name, i.table_name, i.column_name
                                       ORDER BY i.applied_at DESC, i.reviewed_at DESC) = 1"""
        ).createOrReplaceTempView("phi_verified_evidence")
        log.info("Reference views registered for run %s", run)

    def _close_superseded_failed_items(self) -> None:
        """Close APPLICATION_FAILED items whose column changed after approval.

        The application job refuses a plan whose fingerprint no longer matches the
        column. Because a column may have only one open item, the failed item is
        closed (CLOSED_NO_ACTION) so this scan can open a fresh item for review.
        """
        run, proc = lit(self.s.run_id), lit(self.s.process_name)
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_ITEM)} t
            USING (
              SELECT i.review_item_id
              FROM {self.rt.t(c.T_ITEM)} i
              JOIN {self.rt.t(c.T_PLAN)} p ON p.review_item_id = i.review_item_id
              JOIN phi_in_scope_columns col ON {_on('col', 'i')}
              WHERE i.status = {lit(c.APPLICATION_FAILED)} AND p.column_fingerprint <> col.column_fingerprint
            ) s ON t.review_item_id = s.review_item_id
            WHEN MATCHED AND t.status = {lit(c.APPLICATION_FAILED)}
              THEN UPDATE SET status = {lit(c.CLOSED_NO_ACTION)}, {self.rt.audit_update_set()}"""
        )
        self.rt.insert_events_from_query(
            f"""SELECT review_batch_id, review_item_id, {lit(c.EV_ITEM_CLOSED)} AS event_type,
                   CAST(NULL AS STRING) AS business_actor,
                   to_json(named_struct('reason', 'Column metadata changed after approval; reopened for review'))
                     AS event_details_json,
                   CAST(NULL AS STRING) AS discriminator
            FROM {self.rt.t(c.T_ITEM)}
            WHERE status = {lit(c.CLOSED_NO_ACTION)} AND application_error IS NOT NULL
              AND audit_updated_run_id = {run} AND audit_updated_process = {proc}"""
        )

    def _stage_findings(self, scopes: list[dict]) -> None:
        corr = self.correction
        corr_pred = (
            "false"
            if not corr
            else " AND ".join(f"col.{k} = {lit(v)}" for k, v in zip(IDENTITY, corr))
        )
        orphan = "true" if self.s.detect_orphaned_tags else "false"
        months = f"CASE col.risk_tier WHEN 'HIGH' THEN {c.RISK_TIERS['HIGH']} ELSE {c.RISK_TIERS['STANDARD']} END"
        issue = f"""CASE
          WHEN {corr_pred} THEN '{c.CORRECTION_REQUESTED}'
          WHEN tg.tag_value IS NOT NULL AND av.value IS NULL THEN '{c.INVALID_TAG_VALUE}'
          WHEN tg.tag_value IS NOT NULL AND ev.review_item_id IS NOT NULL
               AND ev.column_fingerprint <> col.column_fingerprint THEN '{c.STALE_PHI_REVIEW}'
          WHEN tg.tag_value IS NULL AND att.attestation_id IS NOT NULL
               AND att.column_fingerprint <> col.column_fingerprint THEN '{c.STALE_NON_PHI_ATTESTATION}'
          WHEN {orphan} AND tg.tag_value IS NOT NULL
               AND (ev.review_item_id IS NULL OR ev.approved_tag_value <> tg.tag_value OR att.attestation_id IS NOT NULL)
               THEN '{c.ORPHANED_TAG}'
          WHEN tg.tag_value IS NOT NULL AND ev.reviewed_at IS NOT NULL
               AND timestampadd(MONTH, {months}, ev.reviewed_at) <= current_timestamp() THEN '{c.RECERTIFICATION_DUE}'
          WHEN tg.tag_value IS NULL AND att.attestation_id IS NOT NULL
               AND att.column_fingerprint = col.column_fingerprint AND att.expires_at <= current_timestamp()
               THEN '{c.RECERTIFICATION_DUE}'
          WHEN tg.tag_value IS NULL AND att.attestation_id IS NULL THEN '{c.MISSING_CLASSIFICATION}'
        END"""
        stage = f"{self.s.schema_fqn}.{qi(STAGE_TABLE)}"
        self.rt.sql(
            f"""CREATE OR REPLACE TABLE {stage}
            COMMENT 'Scratch: in-scope columns and findings for the most recent scan run. Overwritten every run.'
            AS
            WITH base AS (
              SELECT col.*, tg.tag_value AS current_tag_value, {issue} AS issue_type,
                     sg.suggested_tag_value AS dc_suggestion, sg.suggestion_reason AS dc_reason
              FROM phi_in_scope_columns col
              LEFT JOIN phi_direct_tags tg ON {_on('tg', 'col')}
              LEFT JOIN phi_allowed_values av ON av.value = tg.tag_value
              LEFT JOIN phi_active_attestations att ON {_on('att', 'col')}
              LEFT JOIN phi_verified_evidence ev ON {_on('ev', 'col')}
              LEFT JOIN phi_suggestions sg ON {_on('sg', 'col')}
            )
            SELECT {lit(self.s.run_id)} AS scan_run_id, b.*,
              CASE WHEN b.issue_type IN ('{c.RECERTIFICATION_DUE}', '{c.CORRECTION_REQUESTED}')
                        AND b.current_tag_value IS NOT NULL THEN b.current_tag_value
                   WHEN b.issue_type = '{c.RECERTIFICATION_DUE}' THEN NULL
                   ELSE b.dc_suggestion END AS suggested_tag_value,
              CASE WHEN b.issue_type IN ('{c.RECERTIFICATION_DUE}', '{c.CORRECTION_REQUESTED}')
                        AND b.current_tag_value IS NOT NULL THEN 'Current tag value (reconfirm with APPROVE_SUGGESTION)'
                   WHEN b.issue_type = '{c.RECERTIFICATION_DUE}' THEN 'Attestation expired (reconfirm with CONFIRM_NOT_PHI)'
                   ELSE b.dc_reason END AS suggestion_reason,
              o.review_item_id AS open_item_id,
              {make_key_sql('BATCH', lit(self.s.run_id), 'b.scope_id')} AS new_batch_id
            FROM base b
            LEFT JOIN (
              SELECT review_item_id, {', '.join(IDENTITY)} FROM {self.rt.t(c.T_ITEM)}
              WHERE status IN ({in_list(c.NON_TERMINAL_STATUSES)})
            ) o ON {_on('o', 'b')}"""
        )
        counts = self.rt.rows(
            f"SELECT issue_type, count(*) AS n, count(open_item_id) AS already_open FROM {stage} GROUP BY issue_type"
        )
        log.info("Scan findings: %s", counts)
        if not self.correction:
            # Guard: a catalog that returns no columns while it has open items is far more likely a lost
            # privilege or binding than an emptied catalog; stop rather than close its items.
            empty = self.rt.rows(
                f"""SELECT DISTINCT i.catalog_name FROM {self.rt.t(c.T_ITEM)} i
                JOIN phi_scopes p ON p.catalog_name = i.catalog_name
                WHERE i.status IN ({in_list(c.NON_TERMINAL_STATUSES)})
                  AND NOT EXISTS (SELECT 1 FROM {stage} s WHERE s.catalog_name = i.catalog_name)"""
            )
            if empty:
                raise RuntimeError(
                    "No columns were visible in catalogs that have open items: "
                    + ", ".join(r["catalog_name"] for r in empty)
                    + ". Check the job identity's privileges and workspace catalog bindings."
                )

    def _prepare_correction(self) -> bool:
        stage = f"{self.s.schema_fqn}.{qi(STAGE_TABLE)}"
        rows = self.rt.rows(f"SELECT open_item_id FROM {stage}")
        if not rows:
            raise ValueError(f"{'.'.join(self.correction)} was not found in scope")
        if rows[0]["open_item_id"]:
            log.warning(
                "Correction not opened: %s already has open item %s; its decision serves as the correction",
                ".".join(self.correction),
                rows[0]["open_item_id"],
            )
            self._report(f"EXISTING_OPEN_ITEM:{rows[0]['open_item_id']}")
            return False
        return True

    def _report(self, value: str) -> None:
        try:
            from databricks.sdk.runtime import dbutils  # type: ignore

            dbutils.jobs.taskValues.set(key="correction_result", value=value)
        except Exception as exc:  # task values are informational only
            log.debug("Could not set task value: %s", exc)

    # ------------------------------------------------------------------
    def _update_open_items(self) -> None:
        stage = f"{self.s.schema_fqn}.{qi(STAGE_TABLE)}"
        # assigned_steward follows the scope registry, so a changed steward_group reroutes open items.
        metadata = ["table_type", "full_data_type", "column_comment", "column_fingerprint", "current_tag_value", "assigned_steward"]
        frozen = ["issue_type", "suggested_tag_value", "suggestion_reason"]
        # Issue and suggestion refresh only while an item awaits its first decision and is not a
        # correction. The App records the suggestion each reviewer saw, and ingestion rejects an
        # APPROVE_SUGGESTION whose suggestion has since changed.
        mutable = (
            f"t.status = {lit(c.PENDING_STEWARD_REVIEW)} AND t.decision IS NULL "
            f"AND t.issue_type <> {lit(c.CORRECTION_REQUESTED)}"
        )
        changed = " OR ".join(
            [f"NOT (t.{f} <=> s.{f})" for f in metadata] + [f"({mutable} AND NOT (t.{f} <=> s.{f}))" for f in frozen]
        )
        sets = ", ".join(
            [f"{f} = s.{f}" for f in metadata] + [f"{f} = CASE WHEN {mutable} THEN s.{f} ELSE t.{f} END" for f in frozen]
        )
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_ITEM)} t
            USING (SELECT *, steward_group AS assigned_steward FROM {stage}
                   WHERE issue_type IS NOT NULL AND open_item_id IS NOT NULL) s
            ON t.review_item_id = s.open_item_id
            WHEN MATCHED AND t.status IN ({in_list(c.NON_TERMINAL_STATUSES)}) AND ({changed})
              THEN UPDATE SET {sets}, {self.rt.audit_update_set()}"""
        )

    def _open_new_items(self) -> None:
        stage = f"{self.s.schema_fqn}.{qi(STAGE_TABLE)}"
        run = lit(self.s.run_id)
        due = due_at_utc(self.now, self.s.review_due_business_days, self.s.business_timezone)
        audit = self.rt.audit_insert_exprs()
        new_rows = f"SELECT * FROM {stage} WHERE issue_type IS NOT NULL AND open_item_id IS NULL"
        # One batch per scope with at least one new item.
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_BATCH)} t
            USING (SELECT DISTINCT new_batch_id, scope_id FROM ({new_rows})) s
            ON t.review_batch_id = s.new_batch_id
            WHEN NOT MATCHED THEN INSERT (review_batch_id, scope_id, scan_run_id, status, created_at, due_at,
                closed_at, last_error, {', '.join(audit)})
            VALUES (s.new_batch_id, s.scope_id, {run}, {lit(c.BATCH_OPEN)}, {lit(self.now)}, {lit(due)},
                NULL, NULL, {', '.join(audit.values())})"""
        )
        item_key = make_key_sql("ITEM", "s.catalog_name", "s.schema_name", "s.table_name", "s.column_name", "s.new_batch_id")
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_ITEM)} t
            USING (SELECT {item_key} AS review_item_id, s.* FROM ({new_rows}) s) s
            ON t.review_item_id = s.review_item_id
            WHEN NOT MATCHED THEN INSERT (review_item_id, review_batch_id, catalog_name, schema_name, table_name,
                table_type, column_name, full_data_type, column_comment, column_fingerprint, issue_type,
                current_tag_value, suggested_tag_value, suggestion_reason, assigned_steward, status, {', '.join(audit)})
            VALUES (s.review_item_id, s.new_batch_id, s.catalog_name, s.schema_name, s.table_name, s.table_type,
                s.column_name, s.full_data_type, s.column_comment, s.column_fingerprint, s.issue_type,
                s.current_tag_value, s.suggested_tag_value, s.suggestion_reason, s.steward_group,
                {lit(c.PENDING_STEWARD_REVIEW)},
                {', '.join(audit.values())})"""
        )
        proc = lit(self.s.process_name)
        self.rt.insert_events_from_query(
            f"""SELECT review_batch_id, CAST(NULL AS STRING) AS review_item_id, {lit(c.EV_BATCH_CREATED)} AS event_type,
                   CAST(NULL AS STRING) AS business_actor,
                   to_json(named_struct('scope_id', scope_id, 'due_at', due_at)) AS event_details_json,
                   CAST(NULL AS STRING) AS discriminator
            FROM {self.rt.t(c.T_BATCH)} WHERE scan_run_id = {run} AND last_error IS NULL"""
        )
        actor = lit(self.s.correction_requested_by) if self.correction else "CAST(NULL AS STRING)"
        reason = lit(self.s.correction_reason) if self.correction else "CAST(NULL AS STRING)"
        self.rt.insert_events_from_query(
            f"""SELECT review_batch_id, review_item_id, {lit(c.EV_ITEM_OPENED)} AS event_type,
                   CASE WHEN issue_type = '{c.CORRECTION_REQUESTED}' THEN {actor} END AS business_actor,
                   to_json(named_struct('issue_type', issue_type, 'current_tag_value', current_tag_value,
                       'suggested_tag_value', suggested_tag_value,
                       'reason', CASE WHEN issue_type = '{c.CORRECTION_REQUESTED}' THEN {reason} END)) AS event_details_json,
                   CAST(NULL AS STRING) AS discriminator
            FROM {self.rt.t(c.T_ITEM)}
            WHERE audit_created_run_id = {run} AND audit_created_process = {proc}"""
        )

    def _close_out_of_scope_items(self) -> None:
        stage = f"{self.s.schema_fqn}.{qi(STAGE_TABLE)}"
        run, proc = lit(self.s.run_id), lit(self.s.process_name)
        self.rt.sql(
            f"""MERGE INTO {self.rt.t(c.T_ITEM)} t
            USING (
              SELECT i.review_item_id FROM {self.rt.t(c.T_ITEM)} i
              LEFT ANTI JOIN {stage} s ON {_on('s', 'i')}
              WHERE i.status IN ({in_list(c.NON_TERMINAL_STATUSES)})
            ) s ON t.review_item_id = s.review_item_id
            WHEN MATCHED THEN UPDATE SET status = {lit(c.CLOSED_NO_ACTION)}, {self.rt.audit_update_set()}"""
        )
        self.rt.insert_events_from_query(
            f"""SELECT review_batch_id, review_item_id, {lit(c.EV_ITEM_CLOSED)} AS event_type,
                   CAST(NULL AS STRING) AS business_actor,
                   to_json(named_struct('reason', 'Column no longer exists or left the approved scope')) AS event_details_json,
                   CAST(NULL AS STRING) AS discriminator
            FROM {self.rt.t(c.T_ITEM)}
            WHERE status = {lit(c.CLOSED_NO_ACTION)} AND audit_updated_run_id = {run} AND audit_updated_process = {proc}"""
        )


def main(settings: Settings) -> None:
    settings.validate(expected_role=c.ROLE_SCAN)
    Scan(Runtime(settings)).run()
