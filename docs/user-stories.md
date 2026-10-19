# PHI Classification – Phase 1 Pilot User Stories

**TL;DR:** Eight stories in four 2-week sprints take the pilot from nothing to production on one catalog. Two of the eight belong to the platform admin and Data Governance and run in parallel; the engineer owns the other six. The engineer starts from this reference implementation, so most stories mean configuring, adapting, and verifying in your environment. Very little is written from scratch.

**Plan at a glance**

| Sprint | Stories | Outcome |
|---|---|---|
| 0 | PHI-0 (admin), PHI-1 (Data Governance), PHI-2 starts | Prerequisites and pilot scope ready; foundation deploying in dev |
| 1 | PHI-2, PHI-3 | Real scan findings on a dev catalog; stewards can review in Excel |
| 2 | PHI-4, PHI-5 | Tags applied and verified; full lifecycle in dev, then a qa pilot |
| 3 | PHI-6, PHI-7, PHI-8 | qa/prod CI/CD, monitoring, production pilot on the first catalog |

**Conventions used below**

- Dev workspace: `<dev workspace>`
- Governance schema: `<dev governance catalog>.data_governance` (qa/prod: `<qa governance catalog>`, `<prod governance catalog>`)
- Governed catalog prefix: `<dev catalog prefix>` (qa: `<qa catalog prefix>`, prod: `<prod catalog prefix>`)
- Governed tag: `phi_classification`
- Values in `<angle brackets>` are yours to fill in.

**Definition of Done (every engineering story)**

- Deployed through the bundle with `scripts/deploy.sh`, with no manual workspace edits.
- Rerunning the job with the same input creates no duplicate rows, workbooks, or events.
- Every governance row written has its audit columns populated, and every state change has an event.
- Unit tests and lint pass (`uv run --with pytest pytest`, `uv run ruff check src tests tools`).
- Any deviation from the process document is recorded in `docs/design-notes.md`.

---

## PHI-0 · Platform prerequisites for dev (Platform admin)

**Type:** Task · **Owner:** Platform admin · **Sprint:** 0 · **Blocks:** PHI-2

**Description**
As the Data Platform team, we need the dev workspace, governed tag, groups, and Data Classification in place, so the framework can be deployed and can scan a real dev catalog.

**Tasks**
- Give the engineer `CREATE SCHEMA` on `<dev governance catalog>`, plus access to serverless jobs and a serverless SQL warehouse in `<dev workspace>`.
- Confirm the `phi_classification` tag policy exists with Data Governance's approved values. Grant the engineer `ASSIGN` on it.
- Enable Data Classification on the dev pilot catalog `<dev pilot catalog>`. Grant `SELECT` on `system.data_classification.results` to the engineer (and later the framework service principal) **only**.
- Create the pilot steward group `<steward_group>` and confirm the Data Governance group `<governance_group>` and Data Platform group `<platform_group>` exist.
- Create two dev-only test groups for the end-to-end test (for example `dev_phi_test_stewards` and `dev_phi_test_governance`) with the engineer as a member.
- Confirm the dev workspace has a READ/WRITE binding to `<dev governance catalog>` and to the pilot catalog.

**Acceptance criteria**
- [ ] The engineer can create a schema in `<dev governance catalog>` and set a `phi_classification` tag on a test column.
- [ ] `SELECT count(*) FROM system.data_classification.results WHERE catalog_name = '<pilot catalog>'` returns results for the engineer.
- [ ] All groups exist, with the expected members.

---

## PHI-1 · Pilot scope and suggestion mapping (Data Governance)

**Type:** Task · **Owner:** Data Governance · **Sprint:** 0 · **Blocks:** PHI-2 acceptance

**Description**
As Data Governance, I need to choose the pilot scope and approve the mapping from Data Classification classes to `phi_classification` values, so the scan covers the right columns and suggests the right values.

**Tasks**
- Choose the pilot catalog/schema, its risk tier (HIGH or STANDARD), its steward group, and the governance owner.
- Review `reference_data/suggestion_mapping.csv` in the repo. Resolve the rows marked "Review":
  - `class.driver_license` / `class.us_driver_license` → `certificate_license`?
  - `class.passport` / `class.us_passport` → which value?
  - `class.health_plan_beneficiary_number` → `patient_identifier` (beneficiary/member numbers are identifiers, not payer names)?
- Confirm the classes added beyond the original spreadsheet: `class.street_address`, `class.us_bank_number`, `class.us_driver_license`, `class.us_passport`.
- Note: the original spreadsheet has a trailing space in `class.gender ` that would never match a detection; the CSV fixes it.
- Choose the pilot stewards, and confirm the self-reported reviewer identity approval for the pilot (accepted limitation).

**Acceptance criteria**
- [ ] Scope row values agreed: `scope_id` (for example `SCOPE-<DOMAIN>-DEV`), catalog, schema or `*`, risk tier, steward group, owner, effective date.
- [ ] Mapping CSV approved, with every value in the `phi_classification` tag policy.
- [ ] Pilot steward list and Compliance acknowledgement recorded.

---

## PHI-2 · Weekly scan of a dev catalog (includes framework foundation)

**Type:** Story · **Owner:** Engineer · **Sprint:** 0–1 · **Size:** L · **Depends on:** PHI-0; PHI-1 for acceptance

**Description**
As Data Governance, I want the weekly scan to run against our registered dev catalog and open review items, with suggestions from Data Classification, so we can see real findings and size the pilot backlog.

**Context**
The scan reads only metadata (`information_schema`, the tag policy, Data Classification results, and the governance tables). It never reads table data. All eight governance tables come from one setup module and are deployed together in this story, even though later stories write most of them.
Code to start from: `databricks.yml`, `resources/`, `src/phi_framework/ddl.py`, `setup.py`, `scan.py`, `export.py`, `tools/bootstrap.py`, `tools/load_reference_data.py`, `scripts/deploy.sh`.

**Tasks**
1. Fork or import the repo. Keep organization-specific values out of any public upstream.
2. Set the `dev` target values: `governance_catalog=<dev governance catalog>`, `governance_schema=data_governance`, `governed_catalog_prefix=<dev catalog prefix>`, `tag_key=phi_classification`, `governance_group`, `platform_group`, and `business_timezone` (for example `America/New_York`).
3. Deploy: `scripts/deploy.sh dev <warehouse_id> <profile>`. This runs the setup DDL through the warehouse first, because triggered jobs can't be created until the Volume and plan table exist.
4. Load the PHI-1 scope row and mapping with `tools/load_reference_data.py`.
5. Run `dev_phi_classification_scan` manually. Dev triggers and the schedule are paused.
6. Report issue-type counts to Data Governance, to size the backlog.
7. Regression check: run `tests/integration/e2e_sandbox.py` against a throwaway governance schema, using `BUNDLE_VAR_governance_schema=phifw_e2e`, `--steward-group dev_phi_test_stewards`, and `--mapping reference_data/suggestion_mapping.csv`. The test refuses `--reset` on any schema not starting with `phifw_`.

**Acceptance criteria**
- [ ] `scripts/deploy.sh dev` succeeds, and running it a second time changes nothing.
- [ ] `<dev governance catalog>.data_governance` contains all 8 tables, their CHECK/PK/FK constraints and comments, the `make_key` function, and the `classification_review_files` Volume (`inbound/`, `outbound/`).
- [ ] Jobs show the schedule and triggers **paused** in dev.
- [ ] The scan opens one batch per scope with new findings, and one item per actionable column. Issue types follow the documented order.
- [ ] Suggestions come from your real Data Classification results: HIGH confidence, latest detection only, and only when every detection maps to the same allowed value. The `samples` column never appears in any governance table or workbook.
- [ ] Pipeline-internal tables (`__materialization_*`, `event_log_<uuid>`) are not scanned. Verify against your real pipelines.
- [ ] Rerunning the scan with unchanged metadata opens no new items, batches, or workbooks.
- [ ] Two conflicting scope rows (for example two `*` rows for the same catalog) stop the scan with a `BATCH_ERROR` and open no items.
- [ ] An ASSIGNMENT workbook appears in `outbound/<batch_id>/` and opens correctly in your Excel, with locked columns and only `assigned_steward` editable.
- [ ] The end-to-end suite passes against `phifw_e2e`. Attach the log.
- [ ] Spike confirmations recorded in `docs/spike-results.md`: the engineer's identity can read the tag policy and group membership (SCIM).

---

## PHI-3 · Steward assignment, review workbooks, and decision ingestion

**Type:** Story · **Owner:** Engineer · **Sprint:** 1 · **Size:** L · **Depends on:** PHI-2

**Description**
As Data Governance, I want to assign stewards in a workbook and receive one review workbook per steward. As a Data Steward, I want to record my decisions in Excel. As Data Governance, I want returned workbooks to become validated plans, non-PHI attestations, Privacy routing, or clear rejections.

**Context**
Code to start from: `export.py`, `workbook.py`, `ingest.py`, `validation.py`. Ingestion converts every workbook row into a record in `classification_review_submission`, the same contract the Phase 2 App will write, and validates those records.

**Tasks**
1. Walk through the assignment flow with Data Governance on the PHI-2 batch: download the ASSIGNMENT workbook, fill in `assigned_steward`, upload it to `inbound/`, then run `dev_phi_decision_ingestion`.
2. Walk through a steward REVIEW workbook with one pilot steward, using real columns.
3. Agree on the inbound naming convention (`<review_batch_id>_<steward>_v<N>.xlsx`) and the rule that returned files are never overwritten.
4. Draft the steward email from the process-document template, with the `reviewed_at` format.

**Acceptance criteria**
- [ ] Assigning someone outside the scope's steward group is rejected (`VALIDATION_FAILED` event), and the item stays unassigned. An updated ASSIGNMENT workbook is re-exported.
- [ ] One REVIEW workbook per steward per batch. Identity columns are locked, `decision` and `corrected_tag_value` are dropdowns, `reviewed_at` is a Text cell, and `reviewed_by` is pre-filled.
- [ ] Every decision rule is enforced:
  - `APPROVE_SUGGESTION` requires an allowed suggestion.
  - `CORRECT_CLASSIFICATION` requires an allowed corrected value.
  - `CONFIRM_NOT_PHI` requires a comment.
  - `REQUEST_PRIVACY_REVIEW` requires a comment.
  - The reviewer must be in the steward group.
  - `reviewed_at` must be `YYYY-MM-DD HH:MM ±HH:MM`, not in the future, and not before the batch was created.
- [ ] Valid decisions produce immutable plan rows (`classification_application_plan`, append-only) or attestations, with `expires_at` 3 months out for HIGH scopes and 6 months for STANDARD.
- [ ] Rejected rows go back out in a new REVIEW workbook with `last_validation_error` filled in.
- [ ] Uploading the same file twice, or a renamed copy, is processed once (SHA-256).
- [ ] A revised workbook that changes an already-accepted decision is ignored and reported (`CHANGE_IGNORED_ALREADY_RESOLVED`).
- [ ] A malformed workbook, or one with duplicate rows, is rejected once (`WORKBOOK_REJECTED`), alerts once, and does not fail later runs.

---

## PHI-4 · Tag application and verification

**Type:** Story · **Owner:** Engineer · **Sprint:** 2 · **Size:** M · **Depends on:** PHI-3

**Description**
As Data Platform, I want approved tags applied and verified automatically, with failures visible, so steward decisions reach Unity Catalog without manual DDL.

**Context**
Code to start from: `apply.py`, `tag_ddl.py`. The job runs on a table-update trigger on `classification_application_plan`.

**Tasks**
1. Run `dev_phi_classification_application` on the PHI-3 plans.
2. Include at least one materialized view or streaming table column in the pilot, if the pilot catalog has one.
3. Force one failure, for example by changing a column comment after approval. Confirm the failure alert and the recovery path.

**Acceptance criteria**
- [ ] Before any DDL runs, each plan is rechecked: the column still exists, its object type and fingerprint match, its catalog starts with `<dev catalog prefix>`, and the value is still allowed. A plan that fails the recheck is never executed.
- [ ] `SET` and `UNSET` are verified in `information_schema.column_tags`. Items move to `TAG_VERIFIED` or `ATTESTED_NOT_PHI`, with `applied_at` set.
- [ ] Tags on materialized views and streaming tables use the correct `ALTER MATERIALIZED VIEW` / `ALTER STREAMING TABLE` statement and survive a pipeline refresh.
- [ ] A verified PHI tag supersedes any active non-PHI attestation on the same column.
- [ ] A failure sets `APPLICATION_FAILED` with the reason, and the job run **fails** even after task retries, so `failure_notification_emails` fire. A manual run with `retry_failed=true` retries it after remediation.
- [ ] If the column changed after approval, the next scan closes the failed item (`CLOSED_NO_ACTION`) and reopens the column for review.
- [ ] A batch closes (`BATCH_CLOSED`) when every item is final.

---

## PHI-5 · Lifecycle: Privacy outcomes, corrections, and reopened columns

**Type:** Story · **Owner:** Engineer + Data Governance · **Sprint:** 2 · **Size:** M · **Depends on:** PHI-4

**Description**
As Data Governance, I can record Privacy's outcome, request a correction to an accepted decision, and have changed or expiring columns come back for review.

**Context**
Code to start from: `scan.py` (correction parameters and issue order), the PRIVACY workbook in `export.py`, and `ingest.py`.

**Acceptance criteria**
- [ ] Items in `NEEDS_PRIVACY_REVIEW` appear in a PRIVACY workbook. Only a member of `<governance_group>` can record the outcome. A rejected outcome keeps the item in Privacy review, with the reason shown, and a steward's REVIEW workbook cannot close it.
- [ ] Running `dev_phi_classification_scan` with `correction_column`, `correction_reason`, and `correction_requested_by` opens a `CORRECTION_REQUESTED` item in a new batch, pre-filled with the current tag. If the column already has an open item, nothing new is opened.
- [ ] Confirming not-PHI on a tagged column removes the tag and records an attestation. The original item and plan stay unchanged as evidence.
- [ ] A changed column comment or type reopens the column as `STALE_PHI_REVIEW` or `STALE_NON_PHI_ATTESTATION`. A removed column's open item closes as `CLOSED_NO_ACTION`.
- [ ] Re-attesting a column leaves exactly one active attestation.
- [ ] Recertification readiness: in dev only, backdate one verified item and one attestation, and confirm both reopen as `RECERTIFICATION_DUE`.

---

## PHI-6 · CI/CD to qa and prod as the framework service principal

**Type:** Story · **Owner:** Engineer + Platform admin · **Sprint:** 3 · **Size:** M · **Depends on:** PHI-4

**Description**
As Data Platform, I want qa and prod deployed only from CI, running as a dedicated service principal, so production is never changed by hand.

**Tasks**
- Create the framework service principal and grant it:
  - `USE CATALOG`, `USE SCHEMA`, and `SELECT` or `BROWSE` on the governed catalogs
  - `APPLY TAG` on the governed catalogs
  - `ASSIGN` on `phi_classification`
  - `SELECT` on `system.data_classification.results`
  - `ALL PRIVILEGES` on `<env governance catalog>.data_governance`
  - `READ VOLUME` and `WRITE VOLUME` on the review Volume
- Fill in the qa/prod targets: `<qa governance catalog>` / `<qa catalog prefix>` and `<prod governance catalog>` / `<prod catalog prefix>`, plus groups and `framework_service_principal`.
- Build the CI pipeline: validate, then unit tests, then `scripts/deploy.sh <target> <warehouse_id>`, using OAuth M2M.

**Acceptance criteria**
- [ ] qa and prod deploy from CI only; bundle validation fails without `framework_service_principal`.
- [ ] Jobs in qa run as the service principal, and the audit columns show its identity.
- [ ] The service principal can read the tag policy and group membership (SCIM), confirmed in qa.
- [ ] The qa schedule and triggers are unpaused, and the scan runs Monday 06:00 in `business_timezone`.

---

## PHI-7 · Pilot monitoring and alerting

**Type:** Story · **Owner:** Engineer · **Sprint:** 3 · **Size:** S · **Depends on:** PHI-4

**Description**
As Data Governance, I can see classification completeness, aging and overdue items by steward, and failures, so I can send reminders, escalate, and report.

**Context**
Code to start from: `docs/queries.sql`.

**Acceptance criteria**
- [ ] A dashboard or set of saved queries shows:
  - classification completeness per scope (tagged plus attested, divided by in-scope columns)
  - open and overdue items by steward and batch
  - application failures by reason
  - changes ignored for already-accepted items
- [ ] `failure_notification_emails` is set for all jobs in qa and prod.
- [ ] Data Governance has the steps for the 5-day reminder and the 10-business-day escalation.

---

## PHI-8 · Production pilot go-live on the first catalog

**Type:** Story · **Owner:** Engineer + Data Governance · **Sprint:** 3 · **Size:** M · **Depends on:** PHI-5, PHI-6, PHI-7

**Description**
As Data Governance, I can run the first real weekly cycle in prod for the pilot catalog, from scan to verified tags.

**Acceptance criteria**
- [ ] Prod scope row loaded for the pilot catalog only (`<prod pilot catalog>`), with an `effective_date` set.
- [ ] Data Governance and the pilot stewards complete one full cycle: scan, assign, review, ingest, apply, close.
- [ ] Every Phase 1 artifact (workbooks in the Volume, events, plans, attestations) is checked as audit evidence with Compliance.
- [ ] Rollout plan for the next catalogs is agreed, sized from the backlog counts.
- [ ] Recertification deadline recorded: the first HIGH-tier verifications come due 3 months after go-live.

---

## Post-pilot backlog (not yet scheduled)

| Key | Story | Trigger |
|---|---|---|
| PHI-9 | Recertification operating procedure and steward load plan | Before the first HIGH verification turns 3 months old |
| PHI-10 | Turn on `ORPHANED_TAG` (`detect_orphaned_tags=true`) and review pre-existing tags | After the pilot backlog is under control |
| PHI-11 | Full metrics pack and monthly governance review | Month 2 |
| PHI-12 | Generate steward email text from batch data | Optional |
| PHI-13 | Phase 2 Databricks App, writing `classification_review_submission` | After the pilot exit criteria are met |
