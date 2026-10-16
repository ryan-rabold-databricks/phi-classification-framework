# Pilot backlog (consolidated user stories)

Plan: four two-week sprints to a production pilot on one catalog with one
steward group, built by one engineer starting from this reference
implementation. Each story is now mostly "adapt, configure, and verify in our
environment"; the code it starts from is listed.

**Definition of Done (every story):** deployed through the bundle in dev; a
rerun produces no duplicates; audit columns and events written; the relevant
steps of `tests/integration/e2e_sandbox.py` pass against the team's dev
environment.

## Sprint 0: foundations (in parallel)

| ID | Story | Owner | Starts from | Acceptance criteria |
|---|---|---|---|---|
| P-1 | **Platform prerequisites.** As Data Platform, I need the framework identity and access in place so jobs can run. | Platform admins | README, Security and access | Service principal created. Catalog bindings set. Data Classification enabled on the pilot catalog. `SELECT` on the results table granted to the SP only. `phi_classification` tag policy holds the approved values. Steward and governance groups exist. SP can read group membership and the tag policy. |
| P-2 | **Framework foundation.** As Data Platform, I can deploy the governance schema, tables, constraints, and jobs to dev, qa, and prod from one bundle. | Engineer | `databricks.yml`, `resources/`, `ddl.py`, `setup.py`, `tools/bootstrap.py`, `scripts/deploy.sh` | `scripts/deploy.sh dev` succeeds. Rerunning setup changes nothing. Customer catalog names and groups set in targets. |
| P-3 | **Reference data.** As Data Governance, I maintain the scan scope and the suggestion mapping. | Data Governance + engineer | `tools/load_reference_data.py`, `reference_data/` | Pilot scope rows loaded. Mapping reviewed (flagged rows resolved) and loaded. Scope conflicts stop the scan with `BATCH_ERROR`. |

## Sprint 1: scan and handoff

| ID | Story | Starts from | Acceptance criteria |
|---|---|---|---|
| P-4 | **Weekly scan.** As Data Governance, every Monday I get review items for columns that are missing, invalid, stale, or due for recertification, with suggestions where Data Classification is confident. | `scan.py` | Correct issue precedence. Suggestions from HIGH, latest, unambiguous detections only; no sample values copied. Rerun with unchanged metadata opens nothing. Removed columns close as `CLOSED_NO_ACTION`. |
| P-5 | **Assign and export.** As Data Governance, I assign stewards in a workbook and receive one locked review workbook per steward. | `export.py`, `workbook.py`, `ingest.py` (assignments) | ASSIGNMENT workbook per batch. Non-member assignees rejected. REVIEW workbook per steward with dropdowns, a Text `reviewed_at`, and locked identity columns. Exports never overwrite. |

## Sprint 2: decisions and tag application (end-to-end in dev, then qa pilot)

| ID | Story | Starts from | Acceptance criteria |
|---|---|---|---|
| P-6 | **Decision ingestion.** As Data Governance, returned workbooks become validated plans, attestations, or clear rejections. | `ingest.py`, `validation.py` | Every rule in the decision table enforced. Duplicate files skipped by SHA-256. Changes to accepted items ignored and reported. Rejected rows re-exported with the reason. |
| P-7 | **Tag application.** As Data Platform, approved tags are applied and verified automatically, and failures are visible. | `apply.py`, `tag_ddl.py` | Recheck before apply (fingerprint, object type, environment, allowed value). `SET` and `UNSET` verified in `information_schema.column_tags`. `APPLICATION_FAILED` alerts. `retry_failed` path works. Batches close. |

## Sprint 3: lifecycle and operations (prod pilot on the first catalog)

| ID | Story | Starts from | Acceptance criteria |
|---|---|---|---|
| P-8 | **Lifecycle.** As Data Governance, I can resolve Privacy reviews and request corrections. | `scan.py` (corrections), PRIVACY workbook | PRIVACY workbook outcome recorded by a governance-group user. Correction opens a `CORRECTION_REQUESTED` item, or reports the open item. Removing a tag from a verified column works end to end. |
| P-9 | **CI/CD.** As Data Platform, qa and prod deploy only from CI as the service principal. | `scripts/deploy.sh` | CI pipeline runs validate, unit tests, bootstrap, and deploy. No human prod deploys. |
| P-10 | **Pilot monitoring.** As Data Governance, I can see completeness, aging, and failures. | `docs/queries.sql` | Dashboard or saved queries for completeness, overdue items by steward, and failures by reason. Failure emails configured. |

## Post-pilot backlog

| ID | Story | Deadline / trigger |
|---|---|---|
| P-11 | Recertification operating procedure (code exists; confirm volumes and steward load). | Before the first HIGH-tier verification turns 3 months old. |
| P-12 | Turn on `ORPHANED_TAG` and plan the review of pre-existing tags. | After the pilot backlog is under control. |
| P-13 | Full metrics pack and monthly governance review. | Month 2 |
| P-14 | Generate email text from batch data. | Optional |
| P-15 | Phase 2 Databricks App (writes `classification_review_submission`). | After pilot exit criteria |
