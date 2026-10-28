# PHI Classification – Pilot User Stories (review App)

**TL;DR:** Ten stories in four 2-week sprints take the pilot from nothing to production on one catalog, with stewards reviewing in a Databricks App (no Excel). Two of the ten belong to the platform admin and Data Governance and run in parallel; the engineer owns the other eight. The engineer starts from this reference implementation, so most stories mean configuring, adapting, and verifying in your environment. Very little is written from scratch.

**Plan at a glance**

| Sprint | Stories | Outcome |
|---|---|---|
| 0 | PHI-0 (admin), PHI-1 (Data Governance), PHI-2 starts | Prerequisites and pilot scope ready; foundation deploying in dev |
| 1 | PHI-2, PHI-3, PHI-3a | Real scan findings on a dev catalog; reviewers notified and deciding in the App |
| 2 | PHI-4, PHI-5, PHI-5a | Tags applied and verified; full lifecycle and Data Governance maintenance in the App |
| 3 | PHI-6, PHI-7, PHI-8 | qa/prod CI/CD, monitoring, production pilot on the first catalog |

**Conventions used below**

- Dev workspace: `<dev workspace>`
- Governance schema: `<dev governance catalog>.data_governance` (qa/prod: `<qa governance catalog>`, `<prod governance catalog>`)
- Governed catalog prefix: `<dev catalog prefix>` (qa: `<qa catalog prefix>`, prod: `<prod catalog prefix>`)
- Governed tag: `phi_classification`
- Review App: `<review_app_name>`, on SQL warehouse `<warehouse_id>`
- Values in `<angle brackets>` are yours to fill in.

**Definition of Done (every engineering story)**

- Deployed through the bundle with `scripts/deploy.sh`, with no manual workspace edits.
- Rerunning a job with the same input creates no duplicate rows, emails, or events.
- Every governance row written has its audit columns populated, and every state change has an event.
- Python unit tests and lint pass (`uv run --with pytest pytest`, `uv run ruff check src tests tools`); App tests, lint, and type checks pass (`cd app && npx vitest run && npm run lint && npm run typecheck`).
- Any deviation from the process document is recorded in `docs/design-notes.md`.

---

## PHI-0 · Platform prerequisites for dev (Platform admin)

**Type:** Task · **Owner:** Platform admin · **Sprint:** 0 · **Blocks:** PHI-2, PHI-3

**Description**
As the Data Platform team, we need the dev workspace, governed tag, groups, Databricks Apps, email relay, and Data Classification in place, so the framework can be deployed, scan a real dev catalog, and let reviewers work in the App.

**Tasks**
- Give the engineer `CREATE SCHEMA` on `<dev governance catalog>`, plus access to serverless jobs, Databricks Apps, and a serverless SQL warehouse `<warehouse_id>` in `<dev workspace>`.
- Confirm the `phi_classification` tag policy exists with Data Governance's approved values. Grant the engineer `ASSIGN` on it.
- Enable Data Classification on the dev pilot catalog `<dev pilot catalog>`. Grant `SELECT` on `system.data_classification.results` to the engineer (and later the framework service principal) **only**.
- Create or confirm the groups: Data Governance `<governance_group>`, Data Platform `<platform_group>`, the pilot steward group `<steward_group>` (if the scope uses a group rather than one email), and the App users group `<review_app_users_group>` containing every steward and governance owner.
- Create a dev-only test steward group for the end-to-end test (for example `dev_phi_test_stewards`) with the engineer as a member, and add the engineer to `<governance_group>` and `<review_app_users_group>` in dev.
- Provide an SMTP relay for review-request emails: host, port, sender address, and (if the relay needs a login) a secret scope `<smtp_secret_scope>` with keys `smtp_username` and `smtp_password`.
- Confirm the dev workspace has a READ/WRITE binding to `<dev governance catalog>` and to the pilot catalog.

**Acceptance criteria**
- [ ] The engineer can create a schema in `<dev governance catalog>`, set a `phi_classification` tag on a test column, and deploy a Databricks App.
- [ ] `SELECT count(*) FROM system.data_classification.results WHERE catalog_name = '<pilot catalog>'` returns results for the engineer.
- [ ] All groups exist, with the expected members.
- [ ] SMTP relay details and the secret scope are available to the engineer.

---

## PHI-1 · Pilot scope, reviewers, and suggestion mapping (Data Governance)

**Type:** Task · **Owner:** Data Governance · **Sprint:** 0 · **Blocks:** PHI-2 acceptance

**Description**
As Data Governance, I need to choose the pilot scope, who reviews it, and the mapping from Data Classification classes to `phi_classification` values, so the scan covers the right columns, routes them to the right people, and suggests the right values.

**Tasks**
- Choose the pilot catalog/schema and its risk tier (HIGH or STANDARD).
- Choose the reviewers. `steward_group` is one user email or one group name; every user it names is emailed and can review. `governance_owner` is one user email or one group name; it is copied on the email and may review in place of a steward.
- Review `reference_data/suggestion_mapping.csv` in the repo. Resolve the rows marked "Review":
  - `class.driver_license` / `class.us_driver_license` → `certificate_license`?
  - `class.passport` / `class.us_passport` → which value?
  - `class.health_plan_beneficiary_number` → `patient_identifier` (beneficiary/member numbers are identifiers, not payer names)?
- Confirm the classes added beyond the original spreadsheet: `class.street_address`, `class.us_bank_number`, `class.us_driver_license`, `class.us_passport`.
- Note: the original spreadsheet has a trailing space in `class.gender ` that would never match a detection; the CSV fixes it.

**Acceptance criteria**
- [ ] Scope row values agreed: `scope_id` (for example `SCOPE-<DOMAIN>-DEV`), catalog, schema or `*`, risk tier, `steward_group`, `governance_owner`, effective date.
- [ ] Mapping CSV approved, with every value in the `phi_classification` tag policy.
- [ ] Reviewer list agreed and every reviewer added to `<review_app_users_group>`.

---

## PHI-2 · Weekly scan of a dev catalog (includes framework foundation)

**Type:** Story · **Owner:** Engineer · **Sprint:** 0–1 · **Size:** L · **Depends on:** PHI-0; PHI-1 for acceptance

**Description**
As Data Governance, I want the weekly scan to run against our registered dev catalog and open review items, routed to the scope's reviewers and with suggestions from Data Classification, so we can see real findings and size the pilot backlog.

**Context**
The scan reads only metadata (`information_schema`, the tag policy, Data Classification results, and the governance tables). It never reads table data. All eight governance tables come from one setup module and are deployed together in this story, even though later stories write most of them.
Code to start from: `databricks.yml`, `resources/`, `src/phi_framework/ddl.py`, `setup.py`, `scan.py`, `tools/bootstrap.py`, `tools/load_reference_data.py`, `scripts/deploy.sh`.

**Tasks**
1. Fork or import the repo. Keep organization-specific values out of any public upstream.
2. Set the `dev` target values: `governance_catalog=<dev governance catalog>`, `governance_schema=data_governance`, `governed_catalog_prefix=<dev catalog prefix>`, `tag_key=phi_classification`, `governance_group`, `platform_group`, `review_app_name`, `warehouse_id`, `review_app_users_group`, and `business_timezone` (for example `America/New_York`). Leave `smtp_host` empty until PHI-3a.
3. Deploy: `scripts/deploy.sh dev <warehouse_id> <profile>`. It validates, runs the setup DDL through the warehouse first (the triggered jobs and the App's table grants need the tables to exist), deploys the bundle, and starts the App.
4. Load the PHI-1 scope row and mapping with `tools/load_reference_data.py` (initial load only; later edits happen in the App, PHI-5a).
5. Run `dev_phi_classification_scan` manually. Dev triggers and the schedule are paused.
6. Report issue-type counts to Data Governance, to size the backlog.

**Acceptance criteria**
- [ ] `scripts/deploy.sh dev` succeeds, and running it a second time changes nothing.
- [ ] `<dev governance catalog>.data_governance` contains all 8 tables, their CHECK/PK/FK constraints and comments, and the `make_key` function.
- [ ] Jobs show the schedule and triggers **paused** in dev.
- [ ] The scan opens one batch per scope with new findings, and one item per actionable column. Issue types follow the documented order.
- [ ] Every item's `assigned_steward` is the scope's `steward_group`. Changing `steward_group` and rescanning updates it on open items.
- [ ] Suggestions come from your real Data Classification results: HIGH confidence, latest detection only, and only when every detection maps to the same allowed value. The `samples` column never appears in any governance table.
- [ ] Pipeline-internal tables (`__materialization_*`, `event_log_<uuid>`) are not scanned. Verify against your real pipelines.
- [ ] Rerunning the scan with unchanged metadata opens no new items or batches.
- [ ] Two conflicting scope rows (for example two `*` rows for the same catalog) stop the scan with a `BATCH_ERROR` and open no items.

---

## PHI-3 · Review App queue and decision ingestion

**Type:** Story · **Owner:** Engineer · **Sprint:** 1 · **Size:** L · **Depends on:** PHI-2

**Description**
As a Data Steward, I want to see the columns waiting for me in the review App and record my decisions there. As a governance owner, I want to review in place of a steward when needed. As Data Governance, I want every submitted decision validated and turned into a plan, a non-PHI attestation, Privacy routing, or a clear rejection.

**Context**
Code to start from: `app/` (AppKit, TypeScript/React), `resources/review_app.yml`, `src/phi_framework/ingest.py`, `validation.py`. The App writes `classification_review_submission`; ingestion runs on a table-update trigger on that table and is the authority on every rule.

**Tasks**
1. Confirm the App deployed by PHI-2 is running, and that its service principal received the declared grants (`SELECT` on review tables, `MODIFY` on submission/scope/mapping/event tables, `EXECUTE` on `make_key`, `CAN_USE` on the warehouse, `CAN_MANAGE_RUN` on the scan job).
2. Walk through the review queue with one pilot steward on real columns, then with the governance owner.
3. Submit decisions, run `dev_phi_decision_ingestion` (dev triggers are paused), and walk through rejected items returning to the queue with the reason.
4. Regression check: run `tests/integration/e2e_sandbox.py` against a throwaway governance schema (name starting with `phifw_`) with `--steward-group dev_phi_test_stewards` and `--mapping reference_data/suggestion_mapping.csv`. The test drives the deployed App over HTTPS as the signed-in user.

**Acceptance criteria**
- [ ] The App shows the signed-in user's email and roles. A steward sees only the scopes their `steward_group` names; a governance owner sees the scopes they own; a user with no role sees no queue.
- [ ] Group membership is read from the signed-in user's own token (SCIM `/Me` with the forwarded user token; default `iam.current-user:read` scope); nested groups count.
- [ ] Each row shows column metadata, issue type, current tag, suggestion and reason, due date, and any rejection reason. Selected rows can be bulk-approved.
- [ ] The App refuses invalid decisions before writing anything, with a per-item reason:
  - `APPROVE_SUGGESTION` requires an allowed suggestion, unchanged since the reviewer opened the item.
  - `CORRECT_CLASSIFICATION` requires an allowed corrected value.
  - `CONFIRM_NOT_PHI` requires a comment.
  - `REQUEST_PRIVACY_REVIEW` requires a comment.
  - The reviewer must be a steward or the governance owner for the scope.
- [ ] Submissions record the authenticated reviewer (`reviewed_by`), the server time (`reviewed_at`), and the suggestion seen, one submission set per batch.
- [ ] Each submission records the role the App authorized (`acting_as`) and the scope value that authorized it (`authorized_via`).
- [ ] Ingestion re-checks every decision rule and trusts the App's reviewer check, confirming `authorized_via` is still the scope's current reviewer value; a submission written directly to the table that breaks a rule or no longer matches is rejected (`VALIDATION_FAILED`). Ingestion never reads group member lists.
- [ ] Valid decisions produce immutable plan rows (append-only) or attestations, with `expires_at` 3 months out for HIGH scopes and 6 months for STANDARD. `DECISION_RECEIVED` records `acting_as` (`STEWARD` or `GOVERNANCE_OWNER`).
- [ ] Each submission set is processed exactly once (`SUBMISSION_PROCESSED`); rerunning ingestion changes nothing.
- [ ] A submission that changes an already-accepted decision is ignored and reported (`CHANGE_IGNORED_ALREADY_RESOLVED`).
- [ ] The end-to-end suite passes. Attach the log.

---

## PHI-3a · Review-request email

**Type:** Story · **Owner:** Engineer · **Sprint:** 1 · **Size:** S · **Depends on:** PHI-2; PHI-0 (SMTP relay)

**Description**
As a reviewer, I want an email when new columns need my review, with a link straight to them in the App, so I don't have to check the App on a schedule.

**Context**
Code to start from: `src/phi_framework/notify.py`, the `notify_reviewers` task in `resources/scan.job.yml`.

**Tasks**
1. Set `smtp_host`, `smtp_port`, `smtp_sender`, `smtp_secret_scope`, and `smtp_use_tls` for dev; grant the job identity `READ` on the secret scope.
2. Run the scan on a scope with new findings and check the email with a pilot reviewer.
3. Confirm the email text with Data Governance.

**Acceptance criteria**
- [ ] One email per new batch: To every user `steward_group` names (one email, or all members of the group); Cc every user `governance_owner` names who is not already in To.
- [ ] The email shows counts, scope, due date, and a link to the batch in the App, and never column names, comments, or data.
- [ ] Each send records `REVIEW_REQUEST_SENT` with the recipients and any unresolvable reviewer values. A batch with no resolvable recipients records `REVIEW_REQUEST_FAILED` (kind `RECIPIENTS`) once without failing the scan; a send failure records kind `SEND`, fails the task after every batch is attempted, and is retried by the next scan.
- [ ] Rerunning the scan does not resend for a batch already emailed. The task has no retries.
- [ ] With `smtp_host` empty, nothing is sent and the scan still succeeds.

---

## PHI-4 · Tag application and verification

**Type:** Story · **Owner:** Engineer · **Sprint:** 2 · **Size:** M · **Depends on:** PHI-3

**Description**
As Data Platform, I want approved tags applied and verified automatically, with failures visible, so reviewer decisions reach Unity Catalog without manual DDL.

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
As Data Governance, I can record Privacy's outcome in the App, request a correction to an accepted decision in the App, and have changed or expiring columns come back for review.

**Context**
Code to start from: the Privacy review tab and `/api/review/corrections` in `app/`, `scan.py` (correction parameters and issue order), and `ingest.py`.

**Acceptance criteria**
- [ ] Items in `NEEDS_PRIVACY_REVIEW` appear on the Privacy review tab with the steward's question. Only a member of `<governance_group>` or the scope's `governance_owner` can record the outcome. A rejected outcome keeps the item in Privacy review, with the reason shown, and a review-queue decision cannot close it.
- [ ] A correction requested on the Scope registry tab starts the scan, records `CORRECTION_REQUESTED` with the requester, and opens a `CORRECTION_REQUESTED` item in a new batch, pre-filled with the current tag. If the column already has an open item, nothing new is opened. Only `<governance_group>` members can request one.
- [ ] Confirming not-PHI on a tagged column removes the tag and records an attestation. The original item and plan stay unchanged as evidence.
- [ ] A changed column comment or type reopens the column as `STALE_PHI_REVIEW` or `STALE_NON_PHI_ATTESTATION`. A removed column's open item closes as `CLOSED_NO_ACTION`.
- [ ] Re-attesting a column leaves exactly one active attestation.
- [ ] Recertification readiness: in dev only, backdate one verified item and one attestation, and confirm both reopen as `RECERTIFICATION_DUE`.

---

## PHI-5a · Data Governance maintains scopes and mappings in the App

**Type:** Story · **Owner:** Engineer + Data Governance · **Sprint:** 2 · **Size:** S · **Depends on:** PHI-3

**Description**
As Data Governance, I want to add and change scopes, reviewers, and suggestion mappings in the App, with every change recorded, so we stay in control of the framework without running SQL.

**Context**
Code to start from: the Scope registry and Suggestion mapping tabs and the `/api/review/scopes` and `/api/review/mappings` routes in `app/`.

**Acceptance criteria**
- [ ] Everyone with App access can view both tables; only `<governance_group>` members can edit.
- [ ] A scope's catalog must start with `<dev catalog prefix>`; the expiration date cannot precede the effective date; scopes are paused with `scan_enabled` or an expiration date, never deleted.
- [ ] A mapping value must be in the `phi_classification` tag policy; mappings are deactivated, never deleted.
- [ ] Every edit records `SCOPE_REGISTRY_UPDATED` or `SUGGESTION_MAPPING_UPDATED` with the editor and the before/after values, and the row's audit columns show the App process (`dev_phi_review_app`).
- [ ] A changed `steward_group` reroutes open items at the next scan.

---

## PHI-6 · CI/CD to qa and prod as the framework service principal

**Type:** Story · **Owner:** Engineer + Platform admin · **Sprint:** 3 · **Size:** M · **Depends on:** PHI-4

**Description**
As Data Platform, I want qa and prod (jobs and the review App) deployed only from CI, with the jobs running as a dedicated service principal, so production is never changed by hand.

**Tasks**
- Create the framework service principal and grant it:
  - `USE CATALOG`, `USE SCHEMA`, and `SELECT` or `BROWSE` on the governed catalogs
  - `APPLY TAG` on the governed catalogs
  - `ASSIGN` on `phi_classification`
  - `SELECT` on `system.data_classification.results`
  - `ALL PRIVILEGES` on `<env governance catalog>.data_governance`
  - `READ` on the SMTP secret scope
- **Decide how the framework service principal reads group member lists for emails.** Workspace SCIM limits member lists to admins (the App's service principal got a 403 in testing). Reviewer authorization does not need it, but the review-request email does whenever a scope's reviewer value is a group. Agree an approach with the platform admin and verify it with a non-admin principal before go-live.
- Fill in the qa/prod targets: `<qa governance catalog>` / `<qa catalog prefix>` and `<prod governance catalog>` / `<prod catalog prefix>`, plus groups, `framework_service_principal`, `review_app_name`, `warehouse_id`, `review_app_users_group`, and the SMTP settings.
- Build the CI pipeline: validate, then Python and App tests, then `scripts/deploy.sh <target> <warehouse_id>`, using OAuth M2M.

**Acceptance criteria**
- [ ] qa and prod deploy from CI only; bundle validation fails without `framework_service_principal`.
- [ ] Jobs in qa run as the service principal, and the audit columns show its identity.
- [ ] The App runs in qa, and its service principal holds only the grants declared in `resources/review_app.yml`.
- [ ] The service principal can read the tag policy, the App URL, and the member lists of the pilot reviewer groups, confirmed in qa.
- [ ] The qa schedule and triggers are unpaused, and the scan runs Monday 06:00 in `business_timezone`.

---

## PHI-7 · Pilot monitoring and alerting

**Type:** Story · **Owner:** Engineer · **Sprint:** 3 · **Size:** S · **Depends on:** PHI-4

**Description**
As Data Governance, I can see classification completeness, aging and overdue items by scope and reviewer group, email delivery, and failures, so I can send reminders, escalate, and report.

**Context**
Code to start from: `docs/queries.sql`.

**Acceptance criteria**
- [ ] A dashboard or set of saved queries shows:
  - classification completeness per scope (tagged plus attested, divided by in-scope columns)
  - open and overdue items by scope, reviewer group, and batch
  - decisions by reviewer and `acting_as`
  - review requests sent and failed
  - application failures by reason
  - changes ignored for already-accepted items
- [ ] `failure_notification_emails` is set for all jobs in qa and prod.
- [ ] Data Governance has the steps for the 5-day reminder and the 10-business-day escalation.

---

## PHI-8 · Production pilot go-live on the first catalog

**Type:** Story · **Owner:** Engineer + Data Governance · **Sprint:** 3 · **Size:** M · **Depends on:** PHI-5, PHI-5a, PHI-6, PHI-7

**Description**
As Data Governance, I can run the first real weekly cycle in prod for the pilot catalog, from scan to verified tags.

**Acceptance criteria**
- [ ] Prod scope row for the pilot catalog only (`<prod pilot catalog>`), with an `effective_date` set and reviewers confirmed.
- [ ] Data Governance and the pilot reviewers complete one full cycle: scan, email, review in the App, ingest, apply, close.
- [ ] The audit evidence (submissions, events, plans, attestations, reference-data edit events) is checked with Compliance.
- [ ] Rollout plan for the next catalogs is agreed, sized from the backlog counts.
- [ ] Recertification deadline recorded: the first HIGH-tier verifications come due 3 months after go-live.

---

## Post-pilot backlog (not yet scheduled)

| Key | Story | Trigger |
|---|---|---|
| PHI-9 | Recertification operating procedure and reviewer load plan | Before the first HIGH verification turns 3 months old |
| PHI-10 | Turn on `ORPHANED_TAG` (`detect_orphaned_tags=true`) and review pre-existing tags | After the pilot backlog is under control |
| PHI-11 | Full metrics pack and monthly governance review | Month 2 |
| PHI-12 | Automated reminder and escalation emails | After the pilot |
| PHI-13 | Monitor for items stuck in `APPLICATION_IN_PROGRESS` and resume them automatically | After the pilot |
