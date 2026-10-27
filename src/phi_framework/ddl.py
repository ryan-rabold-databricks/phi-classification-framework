"""Single source of truth for the governance tables, keys, constraints, and comments.

``phi_framework.setup`` renders these definitions into idempotent DDL that runs
on every deployment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from phi_framework import constants as c
from phi_framework.sqlutil import in_list, lit, qi


@dataclass(frozen=True)
class Col:
    name: str
    type: str
    not_null: bool
    comment: str


@dataclass(frozen=True)
class TableDef:
    name: str
    comment: str
    columns: list[Col]
    pk: list[str]
    fks: list[tuple[str, list[str], str, list[str]]] = field(default_factory=list)  # (name, cols, ref_table, ref_cols)
    cluster_by: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)  # name -> SQL expression
    append_only: bool = False


def _audit_cols(manual_allowed: bool, created_only: bool, app_allowed: bool = False) -> list[Col]:
    run_nn = not manual_allowed
    writer = "Logical job name" + (" or the review App process" if app_allowed else "")
    run = "Lakeflow job run" + (" or App request id" if app_allowed else "")
    cols = [
        Col("audit_created_at", "TIMESTAMP", True, "When the row was inserted."),
        Col("audit_created_by", "STRING", True, "Identity that inserted the row (current_user())."),
        Col(
            "audit_created_process",
            "STRING",
            True,
            f"{writer} that inserted the row" + (", or MANUAL for an approved manual change." if manual_allowed else "."),
        ),
        Col(
            "audit_created_run_id",
            "STRING",
            run_nn,
            f"{run} that inserted the row" + ("; NULL for manual changes." if manual_allowed else "."),
        ),
    ]
    if created_only:
        return cols
    return cols + [
        Col("audit_updated_at", "TIMESTAMP", True, "When the row was last changed."),
        Col("audit_updated_by", "STRING", True, "Identity that last changed the row."),
        Col(
            "audit_updated_process",
            "STRING",
            True,
            f"{writer} that last changed the row" + (", or MANUAL." if manual_allowed else "."),
        ),
        Col(
            "audit_updated_run_id",
            "STRING",
            run_nn,
            f"{run} that last changed the row" + ("; NULL for manual changes." if manual_allowed else "."),
        ),
    ]


def table_defs(env: str, governed_catalog_prefix: str) -> list[TableDef]:
    jobs = c.process_names(env)

    def process_checks(manual_allowed: bool, created_only: bool, app_allowed: bool = False) -> dict[str, str]:
        writers = jobs + ([c.app_process_name(env)] if app_allowed else []) + ([c.MANUAL_PROCESS] if manual_allowed else [])
        allowed = in_list(writers)
        checks = {"valid_audit_created_process": f"audit_created_process IN ({allowed})"}
        if not created_only:
            checks["valid_audit_updated_process"] = f"audit_updated_process IN ({allowed})"
        return checks

    scope = TableDef(
        name=c.T_SCOPE,
        comment="Approved catalog/schema scopes for the weekly PHI classification scan. Maintained by Data Governance.",
        columns=[
            Col("scope_id", "STRING", True, "Stable identifier for the scope, assigned by Data Governance (SCOPE-<DOMAIN>-<ENV>)."),
            Col("catalog_name", "STRING", True, "Governed catalog included in the scope; must start with the environment's governed catalog prefix."),
            Col("schema_name", "STRING", True, "Schema name, or * for every schema in the catalog."),
            Col("environment", "STRING", True, "Deployment environment the scope belongs to: dev, qa, or prod."),
            Col("risk_tier", "STRING", True, "HIGH (3-month recertification) or STANDARD (6-month)."),
            Col("steward_group", "STRING", True, "Reviewers for the scope: one user email or one group name. Every member is notified of new review items."),
            Col("governance_owner", "STRING", True, "Data Governance owner for the scope: one user email or one group name. Copied on review requests and may review in place of a steward."),
            Col("scan_enabled", "BOOLEAN", True, "Whether the scheduled scan includes the scope."),
            Col("effective_date", "DATE", True, "Date on which the scope becomes active."),
            Col("expiration_date", "DATE", False, "Optional date on which the scope stops being active."),
        ]
        + _audit_cols(manual_allowed=True, created_only=False, app_allowed=True),
        pk=["scope_id"],
        checks={
            "valid_environment": f"environment IN ({in_list(c.ENVIRONMENTS)})",
            "catalog_matches_environment": f"environment = {lit(env)} AND startswith(catalog_name, {lit(governed_catalog_prefix)})",
            "valid_risk_tier": f"risk_tier IN ({in_list(c.RISK_TIERS)})",
            "reviewers_trimmed": "steward_group = trim(steward_group) AND governance_owner = trim(governance_owner) AND steward_group <> '' AND governance_owner <> ''",
            **process_checks(True, False, app_allowed=True),
        },
    )

    mapping = TableDef(
        name=c.T_MAPPING,
        comment="Data Governance-approved mapping from Data Classification class_tag values to allowed PHI tag values.",
        columns=[
            Col("class_tag", "STRING", True, "Data Classification class_tag (for example class.name), matched exactly after trimming."),
            Col("phi_tag_value", "STRING", True, "Allowed governed-tag value suggested for columns with this detection."),
            Col("is_active", "BOOLEAN", True, "Whether the scan uses this mapping."),
            Col("notes", "STRING", False, "Data Governance rationale for the mapping."),
        ]
        + _audit_cols(manual_allowed=True, created_only=False, app_allowed=True),
        pk=["class_tag"],
        checks={"class_tag_trimmed": "class_tag = trim(class_tag)", **process_checks(True, False, app_allowed=True)},
    )

    batch = TableDef(
        name=c.T_BATCH,
        comment="One review package per scope per scan run.",
        columns=[
            Col("review_batch_id", "STRING", True, "Deterministic key for the review package: make_key('BATCH', [scan_run_id, scope_id])."),
            Col("scope_id", "STRING", True, "Scope registry record evaluated by the scan."),
            Col("scan_run_id", "STRING", True, "Lakeflow job run that created the batch."),
            Col("status", "STRING", True, "OPEN while any item is non-terminal; CLOSED when every item is terminal."),
            Col("created_at", "TIMESTAMP", True, "When the scan created the batch."),
            Col("due_at", "TIMESTAMP", True, "When steward review is due."),
            Col("closed_at", "TIMESTAMP", False, "When every item in the batch reached a terminal status."),
            Col("last_error", "STRING", False, "Most recent batch-level error, if one occurred."),
        ]
        + _audit_cols(manual_allowed=False, created_only=False),
        pk=["review_batch_id"],
        fks=[("fk_batch_scope", ["scope_id"], c.T_SCOPE, ["scope_id"])],
        checks={"valid_batch_status": f"status IN ({in_list(c.BATCH_STATUSES)})", **process_checks(False, False)},
    )

    item = TableDef(
        name=c.T_ITEM,
        comment="One record per column requiring classification review or remediation.",
        columns=[
            Col("review_item_id", "STRING", True, "Deterministic key: make_key('ITEM', [catalog, schema, table, column, review_batch_id])."),
            Col("review_batch_id", "STRING", True, "Review batch containing the item."),
            Col("catalog_name", "STRING", True, "Catalog containing the reviewed column."),
            Col("schema_name", "STRING", True, "Schema containing the reviewed column."),
            Col("table_name", "STRING", True, "Table containing the reviewed column."),
            Col("table_type", "STRING", True, "Unity Catalog object type when scanned: MANAGED, EXTERNAL, STREAMING_TABLE, or MATERIALIZED_VIEW."),
            Col("column_name", "STRING", True, "Column requiring classification review."),
            Col("full_data_type", "STRING", True, "Current Unity Catalog full data type."),
            Col("column_comment", "STRING", False, "Current Unity Catalog column comment."),
            Col("column_fingerprint", "STRING", True, "SHA-256 of normalized catalog, schema, table, column name, full data type, and comment."),
            Col("issue_type", "STRING", True, "Reason the scan opened the item."),
            Col("current_tag_value", "STRING", False, "Observed direct tag value when scanned (never constrained, so invalid values can be recorded)."),
            Col("suggested_tag_value", "STRING", False, "Proposed governed-tag value; NULL when no approved suggestion exists."),
            Col("suggestion_reason", "STRING", False, "Source of the suggestion: class tag, confidence, frequency, and detection time."),
            Col("assigned_steward", "STRING", False, "Reviewers for the item: the scope's steward_group (one user email or one group name), refreshed by each scan."),
            Col("decision", "STRING", False, "Ratification decision supplied by the Data Steward."),
            Col("corrected_tag_value", "STRING", False, "Replacement allowed value when the suggestion is corrected."),
            Col("steward_comment", "STRING", False, "Steward rationale or supporting comment."),
            Col("reviewed_by", "STRING", False, "Authenticated identity of the reviewer who made the decision (captured by the review App)."),
            Col("reviewed_at", "TIMESTAMP", False, "When the reviewer submitted the decision in the review App, in UTC."),
            Col("status", "STRING", True, "Controlled workflow state."),
            Col("applied_at", "TIMESTAMP", False, "When the planned action completed and was verified."),
            Col("application_error", "STRING", False, "Application or verification error, when present."),
        ]
        + _audit_cols(manual_allowed=False, created_only=False),
        pk=["review_item_id"],
        fks=[("fk_item_batch", ["review_batch_id"], c.T_BATCH, ["review_batch_id"])],
        cluster_by=["review_batch_id"],
        checks={
            "valid_item_status": f"status IN ({in_list(c.ITEM_STATUSES)})",
            "valid_decision": f"decision IS NULL OR decision IN ({in_list(c.DECISIONS)})",
            "valid_issue_type": f"issue_type IN ({in_list(c.ISSUE_TYPES)})",
            "valid_table_type": f"table_type IN ({in_list(c.TABLE_TYPES)})",
            **process_checks(False, False),
        },
    )

    attestation = TableDef(
        name=c.T_ATTESTATION,
        comment="Confirmed-not-PHI evidence. Each column has at most one active (unsuperseded) attestation.",
        columns=[
            Col("attestation_id", "STRING", True, "Deterministic key: make_key('ATT', [review_item_id])."),
            Col("review_batch_id", "STRING", True, "Review batch that authorized the attestation."),
            Col("review_item_id", "STRING", True, "Review item resolved by the attestation."),
            Col("catalog_name", "STRING", True, "Catalog containing the attested column."),
            Col("schema_name", "STRING", True, "Schema containing the attested column."),
            Col("table_name", "STRING", True, "Table containing the attested column."),
            Col("column_name", "STRING", True, "Column confirmed not to contain PHI."),
            Col("column_fingerprint", "STRING", True, "Fingerprint to which the attestation applies."),
            Col("attestation_type", "STRING", True, "Governed non-PHI attestation outcome."),
            Col("rationale", "STRING", True, "Steward-provided basis for the non-PHI decision."),
            Col("attested_by", "STRING", True, "Identity of the steward who made the decision."),
            Col("attested_at", "TIMESTAMP", True, "When ingestion validated the decision; the attestation is effective from this time."),
            Col("expires_at", "TIMESTAMP", True, "attested_at plus the scope's recertification interval."),
            Col("superseded_at", "TIMESTAMP", False, "When a replacement decision invalidated this record."),
        ]
        + _audit_cols(manual_allowed=False, created_only=False),
        pk=["attestation_id"],
        fks=[
            ("fk_attestation_batch", ["review_batch_id"], c.T_BATCH, ["review_batch_id"]),
            ("fk_attestation_item", ["review_item_id"], c.T_ITEM, ["review_item_id"]),
        ],
        checks={"valid_attestation_type": f"attestation_type IN ({in_list(c.ATTESTATION_TYPES)})", **process_checks(False, False)},
    )

    plan = TableDef(
        name=c.T_PLAN,
        comment="Immutable validated tag actions. Never updated or deleted.",
        columns=[
            Col("application_plan_id", "STRING", True, "Deterministic key: make_key('PLAN', [review_item_id])."),
            Col("review_batch_id", "STRING", True, "Review batch that authorized the action."),
            Col("review_item_id", "STRING", True, "Resolved review item that produced the action."),
            Col("action_type", "STRING", True, "SET_PHI_TAG or REMOVE_PHI_TAG."),
            Col("catalog_name", "STRING", True, "Target catalog captured when the plan was created."),
            Col("schema_name", "STRING", True, "Target schema captured when the plan was created."),
            Col("table_name", "STRING", True, "Target table captured when the plan was created."),
            Col("table_type", "STRING", True, "Target object type; revalidated before execution."),
            Col("column_name", "STRING", True, "Target column captured when the plan was created."),
            Col("column_fingerprint", "STRING", True, "Expected fingerprint; revalidated before execution."),
            Col("approved_tag_value", "STRING", False, "Governed-tag value for SET_PHI_TAG; NULL for REMOVE_PHI_TAG."),
            Col("reviewed_by", "STRING", True, "Reviewer identity carried into the plan."),
            Col("reviewed_at", "TIMESTAMP", True, "Reviewer decision time carried into the plan."),
            Col("rendered_ddl", "STRING", True, "Exact validated DDL for the tag action."),
        ]
        + _audit_cols(manual_allowed=False, created_only=True),
        pk=["application_plan_id"],
        fks=[
            ("fk_plan_batch", ["review_batch_id"], c.T_BATCH, ["review_batch_id"]),
            ("fk_plan_item", ["review_item_id"], c.T_ITEM, ["review_item_id"]),
        ],
        checks={
            "valid_action_type": f"action_type IN ({in_list(c.ACTION_TYPES)})",
            "valid_plan_table_type": f"table_type IN ({in_list(c.TABLE_TYPES)})",
            "set_has_value": f"action_type <> {lit(c.SET_PHI_TAG)} OR approved_tag_value IS NOT NULL",
            **process_checks(False, True),
        },
        append_only=True,
    )

    event = TableDef(
        name=c.T_EVENT,
        comment="Append-only workflow audit trail.",
        columns=[
            Col("event_id", "STRING", True, "Deterministic key: make_key('EVT', [event_type, review_batch_id, review_item_id, run_id, discriminator])."),
            Col("review_batch_id", "STRING", True, "Batch associated with the event, or REFERENCE_DATA for reference-data edits and correction requests."),
            Col("review_item_id", "STRING", False, "Review item associated with the event, when applicable."),
            Col("event_type", "STRING", True, "Workflow transition or activity being recorded."),
            Col("event_timestamp", "TIMESTAMP", True, "When the event occurred."),
            Col("business_actor", "STRING", False, "Business user responsible for the underlying decision."),
            Col("event_details", "VARIANT", False, "Structured details specific to the event type."),
            Col("discriminator", "STRING", False, "Submission set for ingestion events; record key for reference-data edits; NULL otherwise."),
        ]
        + _audit_cols(manual_allowed=False, created_only=True, app_allowed=True),
        pk=["event_id"],
        fks=[("fk_event_batch", ["review_batch_id"], c.T_BATCH, ["review_batch_id"])],
        cluster_by=["review_batch_id"],
        checks={"valid_event_type": f"event_type IN ({in_list(c.EVENT_TYPES)})", **process_checks(False, True, app_allowed=True)},
        append_only=True,
    )

    submission = TableDef(
        name=c.T_SUBMISSION,
        comment=(
            "Reviewer decisions exactly as submitted in the review App. Ingestion validates each "
            "submission and records the outcome on the review item and in the audit trail."
        ),
        columns=[
            Col("submission_id", "STRING", True, "Deterministic key: make_key('SUB', [submission_set_id, review_item_id])."),
            Col("submission_set_id", "STRING", True, "One App submit action (App request id); ingestion processes each set once."),
            Col("submission_type", "STRING", True, "REVIEW for reviewer decisions, PRIVACY for Privacy outcomes."),
            Col("review_batch_id", "STRING", True, "Batch the submission refers to."),
            Col("review_item_id", "STRING", True, "Review item the submission refers to."),
            Col("decision", "STRING", True, "Submitted decision, before validation."),
            Col("corrected_tag_value", "STRING", False, "Submitted corrected value, before validation."),
            Col("steward_comment", "STRING", False, "Submitted comment."),
            Col("suggested_tag_value_seen", "STRING", False, "Suggested value shown to the reviewer; APPROVE_SUGGESTION is rejected if it has since changed."),
            Col("reviewed_by", "STRING", True, "Authenticated identity of the reviewer, captured by the App."),
            Col("reviewed_at", "TIMESTAMP", True, "When the reviewer submitted, captured by the App server in UTC."),
        ]
        + _audit_cols(manual_allowed=False, created_only=True, app_allowed=True),
        pk=["submission_id"],
        cluster_by=["submission_set_id"],
        checks={
            "valid_submission_type": f"submission_type IN ({in_list(c.SUBMISSION_TYPES)})",
            "reviewed_by_lower": "reviewed_by = lower(trim(reviewed_by))",
            **process_checks(False, True, app_allowed=True),
        },
        append_only=True,
    )
    return [scope, mapping, batch, item, attestation, plan, event, submission]


def create_table_sql(schema_fqn: str, t: TableDef) -> str:
    cols = ",\n  ".join(
        f"{qi(col.name)} {col.type}{' NOT NULL' if col.not_null else ''} COMMENT {lit(col.comment)}" for col in t.columns
    )
    props = {
        "delta.enableDeletionVectors": "true",
        "delta.enableRowTracking": "true",
    }
    if t.append_only:
        props["delta.appendOnly"] = "true"
    props_sql = ", ".join(f"{lit(k)} = {lit(v)}" for k, v in props.items())
    cluster = f"\nCLUSTER BY ({', '.join(qi(x) for x in t.cluster_by)})" if t.cluster_by else ""
    return (
        f"CREATE TABLE IF NOT EXISTS {schema_fqn}.{qi(t.name)} (\n  {cols}\n)\nUSING DELTA{cluster}\n"
        f"COMMENT {lit(t.comment)}\nTBLPROPERTIES ({props_sql})"
    )
