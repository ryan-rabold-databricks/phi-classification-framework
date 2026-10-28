# Design notes and deviations

This reference implementation follows the PHI Classification Audit Process
design (v1.8, App-only review) and its operating flow / ERD. This page records
every place the implementation adds to, interprets, or deviates from that
design, and why.

## Design additions (v1.8)

| Addition | Why |
|---|---|
| `classification_suggestion_mapping` table | The approved mapping from Data Classification `class_tag` values to allowed tag values. Data Governance maintains it in the review App (or `MANUAL` for the initial load). |
| `classification_review_submission` table | The review App writes each decision exactly as submitted, with the authenticated reviewer, server-captured time, and the suggestion the reviewer saw. Ingestion validates submissions and records outcomes on items and in events; submissions are append-only. |
| Reviewers from the scope registry | No assignment step. `steward_group` and `governance_owner` each hold one user email or one group name. The scan copies `steward_group` into `assigned_steward`; the governance owner may review in place of a steward, and `DECISION_RECEIVED` records `acting_as`. |
| Review App | AppKit (TypeScript/React) Databricks App over a SQL warehouse: review and Privacy queues, scope registry and mapping maintenance, correction requests. Replaces the Excel workbooks and the review Volume. |
| Review-request email | The scan job's `notify_reviewers` task emails each new batch's reviewers (To: `steward_group`; Cc: `governance_owner`) through an SMTP relay, with a link to the batch in the App. `REVIEW_REQUEST_SENT` / `REVIEW_REQUEST_FAILED`. |
| Reference-data audit events | `SCOPE_REGISTRY_UPDATED` and `SUGGESTION_MAPPING_UPDATED` record the actor and the before/after values of every App edit, because the reference tables are updated in place. `CORRECTION_REQUESTED` records corrections requested in the App. |

## Interpretations and deviations

1. **Batch closing runs in every job.** The design assigns `BATCH_CLOSED` to the
   application job, but a batch resolved entirely by non-PHI attestations or
   `CLOSED_NO_ACTION` produces no plan rows and would never trigger that job. All
   three jobs call the same close helper at the end of a run.
2. **Scope-conflict batches.** When the registry has conflicting rows, the scan
   records a batch per conflicting scope with status `CLOSED`, `last_error` set, and a
   `BATCH_ERROR` event, then fails the run. These batches contain no items.
3. **Superseded failed plans.** When a plan fails because the column changed after
   approval (fingerprint mismatch), the next scan closes the `APPLICATION_FAILED` item
   as `CLOSED_NO_ACTION` and opens a new item. Without this, the one-open-item-per-column
   rule would leave the column stuck.
4. **ORPHANED_TAG is off for the pilot** (`detect_orphaned_tags=false`). When enabled,
   a tag is orphaned if it has no framework `TAG_VERIFIED` evidence, its value differs
   from the last verified value, or the column also has an active non-PHI attestation.
   Pre-existing tags are not reviewed or recertified while it is off.
5. **Privacy outcomes only from the Privacy queue.** A `REVIEW` submission for an item
   in `NEEDS_PRIVACY_REVIEW` is rejected with a `VALIDATION_FAILED` event and does not
   change the item's status. The App refuses it before writing; ingestion enforces it
   for anything written another way.
6. **Governance-schema exclusion** applies to the governance schema inside the
   governance catalog (the design's illustrative query excluded any schema named
   `data_governance`).
7. **Governed catalog prefix is a variable.** The design hard-codes an
   organization-specific `<env>_<org>_` naming convention. Here the CHECK constraint is
   `environment = '<env>' AND startswith(catalog_name, '<governed_catalog_prefix>')`,
   and the App refuses scope edits outside the prefix.
8. **The schema is created by the setup code, not as a bundle resource**, so
   deploying into an existing schema never fails and `bundle destroy` never drops
   governance evidence.
9. **Bootstrap before deploy.** Lakeflow Jobs validates trigger targets at job
   creation, so the submission and plan tables must exist before the ingestion and
   application jobs can be deployed. The App's Unity Catalog resources also reference
   the tables and `make_key`. `scripts/deploy.sh` runs the same setup code through a
   SQL warehouse first.
10. **Dev triggers are paused explicitly** with `presets.trigger_pause_status`.
    Development mode pauses schedules but did not pause file-arrival or table-update
    triggers in testing.
11. **`allowed_values_override` (dev only).** A sandbox where a governed tag policy
    cannot be created may supply the allowed values directly. The setting is rejected
    in qa and prod by the jobs and by the App. The App receives it as
    `PHI_ALLOWED_VALUES_OVERRIDE` with a fixed `values:` prefix, which it strips,
    because the Apps API rejects an environment variable with an empty value.
12. **A staging table** (`scan_findings_stage`) holds the current run's in-scope
    columns and findings so that every statement in a run sees the same snapshot of
    `information_schema`. It is overwritten every run and holds no evidence.
13. **Empty-catalog guard.** If a catalog with open items returns no columns, the scan
    stops instead of closing those items (lost privileges or bindings look identical to
    an emptied catalog).
14. **Pipeline-internal tables are excluded** (`excluded_table_regex`): materialized-view
    backing tables and pipeline event-log tables appear in `information_schema` as
    `MANAGED` tables but are not business data.
15. **Approve-what-you-saw check replaces the suggestion freeze.** The App records the
    suggestion each reviewer saw in `suggested_tag_value_seen`, and ingestion rejects an
    `APPROVE_SUGGESTION` whose item suggestion has since changed. The scan therefore keeps
    refreshing `issue_type` and the suggestion while an item awaits its first decision
    (`PENDING_STEWARD_REVIEW` and `decision IS NULL`). `CORRECTION_REQUESTED` is never
    overwritten.
16. **Every latest HIGH detection must map to the same allowed value.** An unmapped
    detection blocks the suggestion.
17. **One submission set per batch per App action.** A submit that spans several batches
    writes one set per batch (`submission_set_id = <App request id>:<review_batch_id>`).
    Ingestion processes each set once (it skips sets with a `SUBMISSION_PROCESSED` event,
    written last) and rejects a set that mixes batches or submission types.
18. **`REFERENCE_DATA` sentinel batch id.** App events that do not belong to a review
    batch (reference-data edits, correction requests) use `review_batch_id = 'REFERENCE_DATA'`.
    Foreign keys are informational, so the sentinel is accepted.
19. **A rejected Privacy outcome keeps the item in `NEEDS_PRIVACY_REVIEW`.** The Privacy
    queue shows the reason, so a reviewer can never resolve a Privacy item by default.
20. **A verified PHI tag supersedes the column's active non-PHI attestation**, so a
    column is never both tagged and attested.
21. **Application failures survive task retries.** The job fails whenever this job run
    recorded `APPLICATION_FAILED` items, even if a retry attempt had nothing left to apply.
22. **Fingerprints are computed by Spark in every job.** Spark `trim` removes only spaces;
    the Python mirror in `keys.py` matches it and is used only by tests.
23. **Due dates** are 17:00 in `business_timezone`, `review_due_business_days`
    business days after the batch is created. Holidays are not modeled.
24. **App reads go through server routes, not `config/queries`.** AppKit recommends SQL
    files for reads. Here every read is a server route because (a) table names depend on
    the environment's governance catalog and schema, and (b) queues must be filtered on
    the server to the scopes the signed-in user may act on; parameters in SQL-file
    queries come from the browser. This is a deliberate deviation from the AppKit guidance.
25. **App role checks use the user's own token.** The App uses the user
    token the Apps proxy forwards with the default `iam.current-user:read` scope (it cannot
    be requested explicitly; the Apps API rejects it in `user_api_scopes`) and reads the signed-in user's groups from
    SCIM `/Me` with the forwarded token. A reviewer value matches when it is the user's
    email or one of their groups. The App service principal never reads group member
    lists (workspace SCIM restricts that to admins).
26. **App grants are declared as App resources.** `resources/review_app.yml` declares
    `uc_securable` resources: `SELECT` on the batch, item, scope, mapping, event, and
    submission tables; `MODIFY` only on the submission, scope, mapping, and event tables;
    `EXECUTE` on `make_key`. Plus `CAN_USE` on the warehouse and `CAN_MANAGE_RUN` on the
    scan job (also listed in the scan job's permissions so a job deploy does not remove it).
27. **The email task never retries and records each send immediately.** `max_retries: 0`
    and an event flush after every batch, so a crash or retry never emails a batch twice.
    An empty `smtp_host` disables email entirely.
28. **Ingestion trigger timing.** The table-update trigger uses
    `min_time_between_triggers_seconds: 60` and `wait_after_last_change_seconds: 60`
    (the platform minimum), so validation results appear about a minute or two after a submit.

29. **Ingestion trusts the App's reviewer check.** The App determines each reviewer's role
    from the signed-in user's own groups and records it with the scope value that
    authorized it (`acting_as`, `authorized_via`, both NOT NULL with a CHECK on the role).
    Ingestion confirms the role is valid for the decision (Privacy outcomes:
    `DATA_GOVERNANCE` or `GOVERNANCE_OWNER`) and that `authorized_via` still equals the
    scope's current `steward_group`, `governance_owner`, or the Data Governance group, so a
    registry change after submission invalidates it. It never reads group member lists.
    Correction scans likewise trust the requester the App passes; only Data Governance,
    Data Platform, and the App can run the scan job. The trust boundary is MODIFY on the
    submission table, which only the App's service principal holds.

30. **Review-request failures are isolated per batch.** Reviewer values are resolved
    per batch inside the error handling, so one unreadable group (403) or empty value
    never stops other batches; unresolved values are listed on the event and the email
    goes to the rest. A batch with no resolvable recipients records
    `REVIEW_REQUEST_FAILED` with `kind = RECIPIENTS` once and is excluded from later scans
    (a configuration problem should not fail every weekly scan); only `kind = SEND`
    failures fail the task and are retried.

## Open items for the customer

- **Group membership reads for review-request emails.** Confirmed in testing: the App
  service principal received `403 ... is only accessible by admins` from SCIM Groups
  `get`. Reviewer authorization no longer needs member lists (see "Ingestion trusts the
  App's reviewer check"), but the notify task's email expansion does whenever a scope's
  `steward_group` or `governance_owner` is a group. Decide how the qa/prod framework
  service principal gets that ability; not yet verified with a non-admin principal.
- **Tag policy reads.** The App service principal could read the governed tag policy in
  the sandbox. Confirm the same for the framework service principal in qa and prod.
- **App URL lookup by the jobs.** The notify task resolves the App URL with the Apps API
  as the job identity. Confirm the framework service principal can read the App; if not,
  emails go out without a link.
- **Classify the governance schema at deployment.** The design says the
  `data_governance` schema is "classified directly at deployment", but the allowed
  values contain no non-PHI value and the framework's attestations exclude that
  schema. Decide how these columns are recorded.
- **Mapping review.** `reference_data/suggestion_mapping.csv` flags entries to confirm
  (driver license, passport, beneficiary number) and adds Data Classification classes
  that the original mapping did not cover (`class.street_address`, `class.us_*`
  variants).
