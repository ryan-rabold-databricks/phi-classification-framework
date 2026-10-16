# Design notes and deviations

This reference implementation follows the PHI Classification Audit Process
design (v1.7) and its operating flow / ERD. This page records every place the
implementation adds to, interprets, or deviates from that design, and why.

## Approved additions

| Addition | Why |
|---|---|
| `classification_suggestion_mapping` table | The design relies on "the approved Data Governance mapping" from Data Classification `class_tag` values to allowed tag values but does not define where it lives. It is a Data Governance-owned table (like the scope registry, it accepts `MANUAL` edits). |
| `classification_review_submission` table | Ingestion validates normalized submission records, never workbooks directly. The Excel pilot writes one record per returned workbook row; the Phase 2 App writes the same records, so the validation, plan, and attestation logic is reused unchanged. Append-only. |
| Assignment workbook + `STEWARD_ASSIGNED` event | `assigned_steward` lives on `classification_review_item`, which only the jobs may write. Data Governance therefore assigns stewards through an exported ASSIGNMENT workbook that the ingestion job processes. Assignees must belong to the scope's `steward_group`. |

## Interpretations and deviations

1. **Workbook export is a task, not a fourth job.** `export_workbooks` runs after
   the scan and after ingestion. It writes files only (no governance rows), so the
   three logical process names and their CHECK constraints are unchanged. File names
   carry a content hash, so unchanged exports are not rewritten and earlier exports
   are never overwritten.
2. **Batch closing runs in every job.** The design assigns `BATCH_CLOSED` to the
   application job, but a batch resolved entirely by non-PHI attestations or
   `CLOSED_NO_ACTION` produces no plan rows and would never trigger that job. All
   three jobs call the same close helper at the end of a run.
3. **Scope-conflict batches.** When the registry has conflicting rows, the scan
   records a batch per conflicting scope with status `CLOSED`, `last_error` set, and a
   `BATCH_ERROR` event, then fails the run. These batches contain no items.
4. **Superseded failed plans.** When a plan fails because the column changed after
   approval (fingerprint mismatch), the next scan closes the `APPLICATION_FAILED` item
   as `CLOSED_NO_ACTION` and opens a new item. Without this, the one-open-item-per-column
   rule would leave the column stuck.
5. **ORPHANED_TAG is off for the pilot** (`detect_orphaned_tags=false`). When enabled,
   a tag is orphaned if it has no framework `TAG_VERIFIED` evidence, its value differs
   from the last verified value, or the column also has an active non-PHI attestation.
   Pre-existing tags are not reviewed or recertified while it is off.
6. **Privacy outcomes only through PRIVACY workbooks.** A REVIEW-workbook row for an
   item in `NEEDS_PRIVACY_REVIEW` (for example, a steward resubmitting an old file) is
   rejected with a `VALIDATION_FAILED` event and does not change the item's status.
7. **Rejected assignments** are recorded as `VALIDATION_FAILED` events with
   `event_details.kind = "ASSIGNMENT"` and do not change item status.
8. **Governance-schema exclusion** applies to the governance schema inside the
   governance catalog (the design's illustrative query excluded any schema named
   `data_governance`).
9. **Governed catalog prefix is a variable.** The design hard-codes an
   organization-specific `<env>_<org>_` naming convention. Here the CHECK constraint is
   `environment = '<env>' AND startswith(catalog_name, '<governed_catalog_prefix>')`.
10. **Schema and Volume are created by the setup code, not as bundle resources**, so
    deploying into an existing schema never fails and `bundle destroy` never drops
    governance evidence.
11. **Bootstrap before deploy.** Lakeflow Jobs validates trigger targets at job
    creation, so the Volume and plan table must exist before the ingestion and
    application jobs can be deployed. `scripts/deploy.sh` runs the same setup code
    through a SQL warehouse first.
12. **Dev triggers are paused explicitly** with `presets.trigger_pause_status`.
    Development mode pauses schedules but did not pause file-arrival or table-update
    triggers in testing.
13. **`allowed_values_override` (dev only).** A sandbox where a governed tag policy
    cannot be created may supply the allowed values directly. The setting is rejected
    in qa and prod.
14. **A staging table** (`scan_findings_stage`) holds the current run's in-scope
    columns and findings so that every statement in a run sees the same snapshot of
    `information_schema`. It is overwritten every run and holds no evidence.
15. **Empty-catalog guard.** If a catalog with open items returns no columns, the scan
    stops instead of closing those items (lost privileges or bindings look identical to
    an emptied catalog).
16. **Due dates** are 17:00 in `business_timezone`, `review_due_business_days`
    business days after the batch is created. Holidays are not modeled.

## Open items for the customer

- **Classify the governance schema at deployment.** The design says the
  `data_governance` schema is "classified directly at deployment", but the allowed
  values contain no non-PHI value and the framework's attestations exclude that
  schema. Decide how these columns are recorded.
- **Group membership lookups** use the workspace SCIM API with the job identity.
  Confirm the production service principal can read group membership in each
  workspace (spike S-04 ran as a workspace admin).
- **Tag policy reads** use the Tag Policy API. Confirm the service principal can read
  the `phi_classification` policy.
- **Mapping review.** `reference_data/suggestion_mapping.csv` flags entries to confirm
  (driver license, passport, beneficiary number) and adds Data Classification classes
  that the original mapping did not cover (`class.street_address`, `class.us_*`
  variants).
