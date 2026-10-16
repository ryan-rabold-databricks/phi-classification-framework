# Spike results

Run on a Databricks sandbox workspace (AWS, serverless jobs, environment
version 4 / Python 3.12) with synthetic data. Items marked *customer* must be
reconfirmed in the customer's own workspace.

| ID | Question | Result |
|---|---|---|
| S-01 | Data Classification results schema | Confirmed from the [system table documentation](https://docs.databricks.com/aws/en/admin/system-tables/data-classification): `latest_detected_time`, `first_detected_time`, `catalog_id`, `table_id`, `catalog_name`, `schema_name`, `table_name`, `column_name`, `data_type`, `class_tag`, `samples`, `confidence` (`HIGH`/`LOW`), `frequency`. `class_tag` may also be a custom `key: value`. *Customer:* enable Data Classification on the pilot catalog and grant `SELECT` on the results table to the scan identity only. |
| S-02 | Column tags on streaming tables / materialized views survive a full refresh | Previously confirmed by the customer team. The end-to-end test also tags a materialized view column through `ALTER MATERIALIZED VIEW ... ALTER COLUMN ... SET TAGS`. |
| S-03a | Insert-only `MERGE` on a `delta.appendOnly` table | **Works.** `MERGE ... WHEN NOT MATCHED THEN INSERT` succeeds and skips existing keys; `MERGE ... WHEN MATCHED THEN UPDATE` and `DELETE` fail with `DELTA_CANNOT_MODIFY_APPEND_ONLY`. |
| S-03b | Table-update trigger on an append-only table | **Works**, about 90 s after the write with `wait_after_last_change_seconds: 60`. A write made within about a minute of creating the job did not fire the trigger, so deploy the application job before the first plan rows are written (the deploy script does). |
| S-03c | Does a task retry keep `{{job.run_id}}`? | **Yes.** A task that failed on attempt 0 and succeeded on attempt 1 saw the same job run ID both times, so retried runs rebuild the same deterministic keys. |
| S-03d | File-arrival trigger on a Unity Catalog Volume path | **Works** (`url: /Volumes/<catalog>/<schema>/<volume>/inbound/`). Job creation fails if the Volume does not exist yet; see `scripts/deploy.sh`. |
| S-04a | Read governed-tag allowed values from code | **Works**: `WorkspaceClient().tag_policies.get_tag_policy(<tag_key>)` returns the value list. Governed tags are account-level, and accounts have a tag-policy limit. *Customer:* confirm the job identity can read the policy. |
| S-04b | Group membership for `reviewed_by` / `assigned_steward` checks | **Works** through the workspace SCIM API (`groups.list(filter='displayName eq "<group>"', attributes="members")`) for both account and workspace groups. Only direct members come back, as IDs: nested groups must be expanded and user IDs resolved to user names (implemented in `directory.py`). *Customer:* confirm with the service principal. |
| S-05 | Excel on serverless | `openpyxl` is **not** preinstalled in serverless environment 4 and must be a job dependency (it is a wheel dependency here). Writing a workbook straight to a Volume path fails (`OSError: [Errno 5]`), because zip writes need random access. Build workbooks in memory and write the bytes in one call. Locked cells, dropdown lists, hidden sheets, and Text-formatted cells all round-trip correctly. |

## End-to-end results

`tests/integration/e2e_sandbox.py` passes 51/51 checks in two modes:

- **Governed tag:** a real account-level tag policy (18 HIPAA-identifier values) with no
  override. Allowed values are read from the Tag Policy API, suggestions map to the
  policy's values, and Unity Catalog rejects any tag value outside the policy
  (`UC_TAG_POLICY_VALUE_NOT_ALLOWED`). Because an invalid value cannot be staged under a
  governed tag, this mode exercises `ORPHANED_TAG` (a pre-existing tag with no review
  evidence) instead of `INVALID_TAG_VALUE`.
- **Override:** an ungoverned tag with `allowed_values_override` (dev only), which allows an
  invalid pre-existing value and so exercises `INVALID_TAG_VALUE`.

## Deployment findings

- `information_schema.tables` lists pipeline-internal objects as `MANAGED` tables in the
  user's schema: materialized-view backing tables (`__materialization_mat_<uuid>_<mv>_1`)
  and published pipeline event logs (`event_log_<pipeline uuid>`). Without an exclusion,
  one materialized view added 12 spurious review items. The scan skips tables that match
  `excluded_table_regex` (default `^__|^event_log_<uuid>$`).
- Serverless statement latency dominates the application job, at about 1-2 s per SQL
  statement. The first implementation issued about 10 statements per column (about 14 s per
  column). The application job now batches status changes, prechecks, and verification
  into set-based statements, leaving one tag DDL statement per column.

- Development mode paused the schedule but **did not** pause file-arrival or
  table-update triggers. `presets.trigger_pause_status: PAUSED` on the dev
  target pauses them, provided the job YAML does not set `pause_status` explicitly.
- `run_as` with an empty service principal fails validation, which keeps qa and prod
  from accidentally running as a person.
