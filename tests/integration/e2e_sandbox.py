"""End-to-end test of the deployed framework against synthetic data in a dev sandbox.

Drives the real jobs (setup, scan, ingestion, application) with run-now and the
deployed review App over HTTPS as the signed-in user, the way stewards and Data
Governance use it, and asserts every state transition. Synthetic data only.

  uv run python tests/integration/e2e_sandbox.py \
      -p <profile> --warehouse-id <id> --test-schema-prefix phifw_test \
      --steward-group <dev test steward group> --mapping <mapping CSV>

A few deliberately invalid submissions are written directly to the submission
table under the App's process name, to prove that ingestion rejects anything the
App's own checks would have blocked.

Bundle variables (governance catalog/schema, tag key, groups, ...) are read
from `databricks bundle validate -t dev`. The current user must be a member of
the steward group and the governance group.

--reset drops and recreates the governance schema and the synthetic schemas.
It refuses to run unless the target's env is dev AND every schema it would drop
starts with the test prefix "phifw_" (so a real governance schema such as
data_governance can never be dropped by mistake). Point the bundle's
governance_schema at a throwaway schema (for example phifw_e2e) before using it.

--steward-group names the group used as the steward group for the synthetic
scopes. The current user must be a member of it and of the bundle's governance_group.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import urllib.error
import urllib.request
import uuid

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import RunResultState
from sqlexec import Sql

from phi_framework import constants as c
from phi_framework.keys import make_key, make_key_sql
from phi_framework.sqlutil import fqn, lit, qi

RESULTS: list[tuple[str, bool, str]] = []
FINANCE_STEWARD = "finance.steward@example.com"
RESET_SAFE_PREFIX = "phifw_"


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' -- ' + detail) if detail and not cond else ''}")
    if not cond:
        raise AssertionError(f"{name}: {detail}")


class E2E:
    def __init__(self, a):
        self.a = a
        self.w = WorkspaceClient(profile=a.profile)
        self.sql = Sql(self.w, a.warehouse_id)
        self.me = self.w.current_user.me().user_name.lower()
        out = subprocess.run(
            ["databricks", "bundle", "validate", "-t", "dev", "-p", a.profile, "-o", "json"],
            check=True, capture_output=True, text=True, cwd=ROOT,
        ).stdout
        b = json.loads(out)
        self.v = {k: v.get("value") for k, v in b["variables"].items()}
        if self.v["env"] != "dev":
            raise SystemExit("E2E runs only against env=dev")
        summary = json.loads(
            subprocess.run(["databricks", "bundle", "summary", "-t", "dev", "-p", a.profile, "-o", "json"],
                           check=True, capture_output=True, text=True, cwd=ROOT).stdout
        )
        self.jobs = {k: int(j["id"]) for k, j in summary["resources"]["jobs"].items()}
        self.app_url = self.w.apps.get(self.v["review_app_name"]).url.rstrip("/")
        self.gc, self.gs = self.v["governance_catalog"], self.v["governance_schema"]
        self.cat = self.gc  # sandbox: synthetic schemas live in the governance catalog
        self.tag = self.v["tag_key"]
        self.clinical = f"{a.test_schema_prefix}_clinical"
        self.finance = f"{a.test_schema_prefix}_finance"
        self.support = f"{a.test_schema_prefix}_support"
        if a.reset:
            unsafe = [s for s in (self.gs, self.clinical, self.finance, self.support) if not s.startswith(RESET_SAFE_PREFIX)]
            if unsafe:
                raise SystemExit(
                    f"--reset refused: it would DROP SCHEMA ... CASCADE on {', '.join(unsafe)}, which "
                    f"do not start with {RESET_SAFE_PREFIX!r}. Set the bundle's governance_schema to a "
                    f"throwaway schema (for example {RESET_SAFE_PREFIX}e2e) and keep --test-schema-prefix "
                    f"starting with {RESET_SAFE_PREFIX!r}."
                )
        # Governed mode: a real tag policy enforces allowed values, so an invalid pre-existing tag
        # cannot be staged. The test pre-tags a valid value without review evidence instead and
        # runs scans with ORPHANED_TAG detection on.
        self.governed = not self.v.get("allowed_values_override")
        with open(os.path.join(ROOT, a.mapping), newline="") as fh:
            import csv

            self.mapping = {r["class_tag"]: r["phi_tag_value"] for r in csv.DictReader(fh)}
        self.scan_params = {"detect_orphaned_tags": "true"} if self.governed else {}

    # ---------------- helpers ----------------
    def t(self, name: str) -> str:
        return fqn(self.gc, self.gs, name)

    def q(self, s: str) -> list[dict]:
        return self.sql.run(s)

    def run_job(self, key: str, params: dict | None = None) -> int:
        waiter = self.w.jobs.run_now(job_id=self.jobs[key], job_parameters=params or {})
        run_id = waiter.response.run_id
        while True:
            try:
                run = self.w.jobs.get_run(run_id)
            except (ConnectionError, TimeoutError, OSError) as exc:  # laptop sleep / network drop: keep polling
                print(f"  (polling {key} run {run_id} failed: {type(exc).__name__}; retrying)")
                time.sleep(30)
                continue
            if run.state.life_cycle_state.value in ("TERMINATED", "INTERNAL_ERROR", "SKIPPED"):
                break
            time.sleep(10)
        if run.state.result_state != RunResultState.SUCCESS:
            for t in run.tasks or []:
                if t.state.result_state != RunResultState.SUCCESS:
                    out = self.w.jobs.get_run_output(t.run_id)
                    print(f"  task {t.task_key} failed: {out.error}\n{(out.error_trace or '')[-3000:]}")
            raise RuntimeError(f"{key} run {run_id} finished {run.state.result_state}: {run.state.state_message}")
        print(f"  ran {key} (run {run_id})")
        return run_id

    def item(self, schema: str, table: str, column: str, open_only: bool = False) -> dict | None:
        cond = f"AND status IN ({', '.join(lit(s) for s in c.NON_TERMINAL_STATUSES)})" if open_only else ""
        rows = self.q(
            f"""SELECT * FROM {self.t(c.T_ITEM)} WHERE catalog_name = {lit(self.cat)} AND schema_name = {lit(schema)}
            AND table_name = {lit(table)} AND column_name = {lit(column)} {cond}
            ORDER BY audit_created_at DESC LIMIT 1"""
        )
        return rows[0] if rows else None

    def tag_value(self, schema: str, table: str, column: str) -> str | None:
        rows = self.q(
            f"""SELECT tag_value FROM {qi(self.cat)}.information_schema.column_tags WHERE schema_name = {lit(schema)}
            AND table_name = {lit(table)} AND column_name = {lit(column)} AND tag_name = {lit(self.tag)}"""
        )
        return rows[0]["tag_value"] if rows else None

    def events(self, event_type: str) -> list[dict]:
        return self.q(f"SELECT * FROM {self.t(c.T_EVENT)} WHERE event_type = {lit(event_type)}")

    # ---------------- review App ----------------
    def app(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """Call the deployed review App as the signed-in user (OAuth token from the CLI profile)."""
        headers = {"Content-Type": "application/json", **self.w.config.authenticate()}
        req = urllib.request.Request(f"{self.app_url}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def queue(self, kind: str = "review") -> dict[tuple[str, str, str], dict]:
        status, body = self.app("GET", f"/api/review/queue?kind={kind}")
        assert status == 200, body
        return {(i["schema_name"], i["table_name"], i["column_name"]): i for i in body["items"]}

    def submit(self, kind: str, decisions: dict, queue: dict) -> tuple[int, dict]:
        """decisions: {(schema, table, column): {decision, corrected_tag_value?, steward_comment?}}"""
        rows = [{"review_item_id": queue[k]["review_item_id"], "suggested_tag_value_seen": queue[k]["suggested_tag_value"], **d}
                for k, d in decisions.items()]
        return self.app("POST", "/api/review/submissions", {"kind": kind, "decisions": rows})

    def inject(self, kind: str, item: dict, decision: str, *, by: str, corrected: str | None = None,
               comment: str | None = None) -> None:
        """Write a submission directly, bypassing the App's checks, under the App's process name."""
        set_id = f"E2E-{uuid.uuid4()}:{item['review_batch_id']}"
        self.q(f"""INSERT INTO {self.t(c.T_SUBMISSION)} (submission_id, submission_set_id, submission_type, review_batch_id,
            review_item_id, decision, corrected_tag_value, steward_comment, suggested_tag_value_seen, reviewed_by, reviewed_at,
            audit_created_at, audit_created_by, audit_created_process, audit_created_run_id)
            VALUES ({lit(make_key('SUB', [set_id, item['review_item_id']]))},
            {lit(set_id)}, {lit(kind)}, {lit(item['review_batch_id'])}, {lit(item['review_item_id'])}, {lit(decision)},
            {lit(corrected)}, {lit(comment)}, {lit(item['suggested_tag_value'])}, {lit(by.lower())}, current_timestamp(),
            current_timestamp(), current_user(), {lit(c.app_process_name('dev'))}, 'E2E')""")

    def errors_for(self, item_id: str) -> str:
        return " ".join(r["d"] for r in self.q(
            f"SELECT CAST(event_details AS STRING) AS d FROM {self.t(c.T_EVENT)} "
            f"WHERE event_type = 'VALIDATION_FAILED' AND review_item_id = {lit(item_id)}"))

    def wait_run(self, run_id: int) -> None:
        while True:
            run = self.w.jobs.get_run(run_id)
            if run.state.life_cycle_state.value in ("TERMINATED", "INTERNAL_ERROR", "SKIPPED"):
                break
            time.sleep(10)
        if run.state.result_state != RunResultState.SUCCESS:
            raise RuntimeError(f"run {run_id} finished {run.state.result_state}: {run.state.state_message}")
        print(f"  waited for run {run_id}")

    # ---------------- setup ----------------
    def reset(self) -> None:
        print("== Reset sandbox")
        for sch in (self.gs, self.clinical, self.finance, self.support):
            self.q(f"DROP SCHEMA IF EXISTS {fqn(self.cat, sch)} CASCADE")
        # The jobs already exist, so the (faster) setup job can recreate the governance objects.
        self.run_job("phi_framework_setup")

    def seed(self) -> None:
        print("== Seed synthetic data")
        C = self.cat
        for sch in (self.clinical, self.finance, self.support):
            self.q(f"CREATE SCHEMA IF NOT EXISTS {fqn(C, sch)} COMMENT 'Synthetic data for PHI framework E2E tests'")
        self.q(f"""CREATE OR REPLACE TABLE {fqn(C, self.clinical, 'patient_encounter')} (
            patient_name STRING COMMENT 'Patient legal name', mrn STRING COMMENT 'Medical record number',
            encounter_date DATE COMMENT 'Date of encounter', source_system_code STRING COMMENT 'Technical source system identifier',
            notes STRING COMMENT 'Free text notes', ssn STRING COMMENT 'Social security number')""")
        self.q(f"""INSERT INTO {fqn(C, self.clinical, 'patient_encounter')} VALUES
            ('Jane Synthetic', 'MRN0001', DATE'2026-01-02', 'EPIC', 'none', '000-00-0000')""")
        self.q(f"""CREATE OR REPLACE TABLE {fqn(C, self.clinical, 'lab_result')} (
            patient_mrn STRING COMMENT 'MRN of the patient', result_value DOUBLE COMMENT 'Numeric result',
            legacy_flag STRING COMMENT 'Pre-tagged with a value that is not allowed')""")
        # Start from untagged synthetic columns: tags survive CREATE OR REPLACE and earlier runs.
        for sch, tbl, cols in [
            (self.clinical, "patient_encounter", ["patient_name", "mrn", "encounter_date", "source_system_code", "notes", "ssn"]),
            (self.clinical, "lab_result", ["patient_mrn", "result_value", "legacy_flag"]),
        ]:
            for col in cols:
                self.q(f"ALTER TABLE {fqn(C, sch, tbl)} ALTER COLUMN {qi(col)} UNSET TAGS ({lit(self.tag)})")
        pre_tag = self.mapping["class.name"] if self.governed else "retired_value"
        self.q(f"ALTER TABLE {fqn(C, self.clinical, 'lab_result')} ALTER COLUMN legacy_flag SET TAGS ({lit(self.tag)} = {lit(pre_tag)})")
        self.q(f"""CREATE OR REPLACE TABLE {fqn(C, self.finance, 'billing')} (
            account_number STRING COMMENT 'Billing account number', amount DECIMAL(10,2) COMMENT 'Charge amount',
            patient_email STRING COMMENT 'Patient email address')""")
        for col in ["account_number", "amount", "patient_email"]:
            self.q(f"ALTER TABLE {fqn(C, self.finance, 'billing')} ALTER COLUMN {qi(col)} UNSET TAGS ({lit(self.tag)})")
        self.mv = False
        if not self.a.skip_mv:
            try:
                self.q(f"""CREATE OR REPLACE MATERIALIZED VIEW {fqn(C, self.clinical, 'encounter_summary_mv')}
                    COMMENT 'Synthetic MV' AS SELECT mrn, count(*) AS encounter_count
                    FROM {fqn(C, self.clinical, 'patient_encounter')} GROUP BY mrn""")
                for col in ["mrn", "encounter_count"]:
                    self.q(f"ALTER MATERIALIZED VIEW {fqn(C, self.clinical, 'encounter_summary_mv')} "
                           f"ALTER COLUMN {qi(col)} UNSET TAGS ({lit(self.tag)})")
                self.mv = True
            except Exception as exc:
                print(f"  (materialized view skipped: {str(exc)[:200]})")
        # Synthetic Data Classification results (same schema as system.data_classification.results).
        dc = fqn(C, self.support, "dc_results")
        self.q(f"""CREATE OR REPLACE TABLE {dc} (latest_detected_time TIMESTAMP, first_detected_time TIMESTAMP,
            catalog_id STRING, table_id STRING, catalog_name STRING, schema_name STRING, table_name STRING,
            column_name STRING, data_type STRING, class_tag STRING, samples ARRAY<STRING>, confidence STRING, frequency FLOAT)""")
        rows = [
            (self.clinical, "patient_encounter", "patient_name", "class.name", "HIGH"),
            (self.clinical, "patient_encounter", "mrn", "class.medical_record_number", "HIGH"),
            (self.clinical, "patient_encounter", "ssn", "class.us_ssn", "HIGH"),
            (self.clinical, "patient_encounter", "notes", "class.name", "HIGH"),
            (self.clinical, "patient_encounter", "notes", "class.location", "HIGH"),  # conflict -> no suggestion
            (self.clinical, "patient_encounter", "encounter_date", "class.medical_date", "LOW"),  # low -> none
            (self.finance, "billing", "account_number", "class.bank_number", "HIGH"),
            (self.finance, "billing", "account_number", "class.credit_card", "HIGH"),  # same mapped value -> ok
            (self.finance, "billing", "patient_email", "class.email_address", "HIGH"),
        ]
        values = ", ".join(
            f"(current_timestamp(), current_timestamp(), 'x', 'y', {lit(C)}, {lit(s)}, {lit(t)}, {lit(col)}, 'string', "
            f"{lit(tag)}, array('SAMPLE-SECRET-VALUE'), {lit(conf)}, 0.9)"
            for s, t, col, tag, conf in rows
        )
        self.q(f"INSERT INTO {dc} VALUES {values}")
        stewards, gov = self.a.steward_group, self.v["governance_group"]
        # Clinical: group reviewers. Finance: single-email reviewers; the current user is only the
        # governance owner, so finance decisions exercise "owner reviews in place of a steward".
        scopes = f"""scope_id,catalog_name,schema_name,environment,risk_tier,steward_group,governance_owner,scan_enabled,effective_date,expiration_date
SCOPE-CLINICAL-DEV,{C},{self.clinical},dev,HIGH,{stewards},{gov},true,2026-01-01,
SCOPE-FINANCE-DEV,{C},{self.finance},dev,STANDARD,{FINANCE_STEWARD},{self.me},true,2026-01-01,
"""
        path = "/tmp/phifw_e2e_scopes.csv"
        with open(path, "w") as fh:
            fh.write(scopes)
        subprocess.run(
            ["uv", "run", "--quiet", "python", "tools/load_reference_data.py", "-p", self.a.profile, "--warehouse-id",
             self.a.warehouse_id, "--catalog", self.gc, "--schema", self.gs, "--scopes", path,
             "--mapping", self.a.mapping], check=True, cwd=ROOT,
        )
        self.stewards = stewards

    # ---------------- scenario ----------------
    def run(self) -> None:
        if self.a.reset:
            self.reset()
        self.seed()
        cl, fi = self.clinical, self.finance

        print("== 0. Key parity (Python vs SQL make_key)")
        sql_key = self.sql.scalar(f"SELECT {fqn(self.gc, self.gs, 'make_key')}('ITEM', array('a', NULL, 'c'))")
        check("make_key parity", sql_key == make_key("ITEM", ["a", None, "c"]), f"{sql_key}")
        inline = self.sql.scalar(f"SELECT {make_key_sql('ITEM', lit('a'), 'NULL', lit('c'))}")
        check("inline make_key_sql parity", inline == make_key("ITEM", ["a", None, "c"]), f"{inline}")

        print("== 1. Setup job is idempotent")
        self.run_job("phi_framework_setup")

        print("== 2. First scan routes items to each scope's reviewers")
        self.run_job("phi_classification_scan", self.scan_params)
        batches = self.q(f"SELECT * FROM {self.t(c.T_BATCH)}")
        check("two batches (one per scope)", len(batches) == 2, str(len(batches)))
        pn = self.item(cl, "patient_encounter", "patient_name")
        check("MISSING_CLASSIFICATION opened", pn and pn["issue_type"] == c.MISSING_CLASSIFICATION)
        check("assigned_steward is the scope's steward_group", pn["assigned_steward"] == self.stewards
              and self.item(fi, "billing", "amount")["assigned_steward"] == FINANCE_STEWARD)
        check("suggestion from DC", pn["suggested_tag_value"] == self.mapping["class.name"], str(pn["suggested_tag_value"]))
        check("conflicting detections -> no suggestion", self.item(cl, "patient_encounter", "notes")["suggested_tag_value"] is None)
        check("LOW confidence -> no suggestion", self.item(cl, "patient_encounter", "encounter_date")["suggested_tag_value"] is None)
        check("two HIGH detections mapping to one value -> suggestion",
              self.item(fi, "billing", "account_number")["suggested_tag_value"] == self.mapping["class.bank_number"])
        if self.governed:
            check("pre-existing tag without review evidence -> ORPHANED_TAG",
                  self.item(cl, "lab_result", "legacy_flag")["issue_type"] == c.ORPHANED_TAG)
        else:
            check("invalid pre-existing tag -> INVALID_TAG_VALUE",
                  self.item(cl, "lab_result", "legacy_flag")["issue_type"] == c.INVALID_TAG_VALUE)
        leaked = self.sql.scalar(f"SELECT count(*) FROM {self.t(c.T_ITEM)} WHERE suggestion_reason LIKE '%SAMPLE-SECRET%'")
        check("DC sample values never copied", int(leaked) == 0)
        n_items = int(self.sql.scalar(f"SELECT count(*) FROM {self.t(c.T_ITEM)}"))
        check("every in-scope column has an item", n_items == (14 if self.mv else 12), str(n_items))
        check("no review request without SMTP configured", len(self.events(c.EV_REVIEW_REQUEST_SENT)) == 0)

        print("== 3. Rerun scan with unchanged metadata opens nothing")
        self.run_job("phi_classification_scan", self.scan_params)
        check("no new items or batches",
              int(self.sql.scalar(f"SELECT count(*) FROM {self.t(c.T_ITEM)}")) == n_items
              and int(self.sql.scalar(f"SELECT count(*) FROM {self.t(c.T_BATCH)}")) == 2)

        print("== 4. The App resolves the signed-in user's roles and queue")
        status, me = self.app("GET", "/api/review/me")
        roles = {s["scope_id"]: s["roles"] for s in me.get("scopes", [])}
        check("App identifies the signed-in user", status == 200 and me["email"] == self.me, str(me)[:300])
        check("steward of clinical via group", "STEWARD" in roles.get("SCOPE-CLINICAL-DEV", []), str(roles))
        check("governance owner of finance via email", roles.get("SCOPE-FINANCE-DEV") == ["GOVERNANCE_OWNER"], str(roles))
        check("Data Governance membership", me.get("isGovernance") is True)
        q = self.queue()
        check("queue lists every item in both scopes", len(q) == n_items, str(len(q)))

        print("== 5. Decisions through the App")
        bad_status, bad = self.submit("REVIEW", {(cl, "lab_result", "patient_mrn"): {
            "decision": c.CORRECT_CLASSIFICATION, "corrected_tag_value": "not_a_value"}}, q)
        check("App refuses a value outside the tag policy; nothing written", bad_status == 422
              and int(self.sql.scalar(f"SELECT count(*) FROM {self.t(c.T_SUBMISSION)}")) == 0, str(bad)[:300])
        dec = {
            (cl, "patient_encounter", "patient_name"): {"decision": c.APPROVE_SUGGESTION},
            (cl, "patient_encounter", "mrn"): {"decision": c.APPROVE_SUGGESTION},
            (cl, "patient_encounter", "ssn"): {"decision": c.APPROVE_SUGGESTION},
            (cl, "patient_encounter", "encounter_date"): {"decision": c.CORRECT_CLASSIFICATION, "corrected_tag_value": "date"},
            (cl, "patient_encounter", "source_system_code"): {"decision": c.CONFIRM_NOT_PHI, "steward_comment": "Technical source identifier only"},
            (cl, "patient_encounter", "notes"): {"decision": c.REQUEST_PRIVACY_REVIEW, "steward_comment": "Free text may contain names"},
            (cl, "lab_result", "legacy_flag"): {"decision": c.CONFIRM_NOT_PHI, "steward_comment": "Legacy flag, not PHI"},
            (fi, "billing", "account_number"): {"decision": c.APPROVE_SUGGESTION},
        }
        if self.mv:
            dec[(cl, "encounter_summary_mv", "mrn")] = {"decision": c.CORRECT_CLASSIFICATION, "corrected_tag_value": "mrn"}
            dec[(cl, "encounter_summary_mv", "encounter_count")] = {"decision": c.CONFIRM_NOT_PHI, "steward_comment": "Aggregate count"}
        status, body = self.submit("REVIEW", dec, q)
        check("App accepts valid decisions", status == 200 and body.get("submitted") == len(dec), str(body)[:300])
        check("one submission set per batch", int(self.sql.scalar(
            f"SELECT count(DISTINCT submission_set_id) FROM {self.t(c.T_SUBMISSION)}")) == 2)
        sub = self.q(f"SELECT * FROM {self.t(c.T_SUBMISSION)} LIMIT 1")[0]
        check("submission carries the authenticated reviewer and App process",
              sub["reviewed_by"] == self.me and sub["audit_created_process"] == c.app_process_name("dev"), str(sub)[:300])
        # Bypass the App: ingestion must still reject an invalid value and an unauthorized reviewer.
        self.inject(c.SUB_REVIEW, self.item(cl, "lab_result", "patient_mrn"), c.CORRECT_CLASSIFICATION, by=self.me, corrected="not_a_value")
        self.inject(c.SUB_REVIEW, self.item(fi, "billing", "patient_email"), c.APPROVE_SUGGESTION, by="not.a.reviewer@example.com")
        self.run_job("phi_decision_ingestion")
        check("approve -> PLANNED", self.item(cl, "patient_encounter", "patient_name")["status"] == c.PLANNED)
        check("correct -> PLANNED", self.item(cl, "patient_encounter", "encounter_date")["status"] == c.PLANNED)
        check("not PHI (untagged) -> ATTESTED_NOT_PHI", self.item(cl, "patient_encounter", "source_system_code")["status"] == c.ATTESTED_NOT_PHI)
        check("not PHI (tagged) -> PLANNED removal", self.item(cl, "lab_result", "legacy_flag")["status"] == c.PLANNED)
        check("privacy request -> NEEDS_PRIVACY_REVIEW", self.item(cl, "patient_encounter", "notes")["status"] == c.NEEDS_PRIVACY_REVIEW)
        check("ingestion rejects a value outside the policy", self.item(cl, "lab_result", "patient_mrn")["status"] == c.VALIDATION_FAILED)
        email_item = self.item(fi, "billing", "patient_email")
        check("ingestion rejects an unauthorized reviewer", email_item["status"] == c.VALIDATION_FAILED
              and "not a reviewer for this scope" in self.errors_for(email_item["review_item_id"]))
        acting = self.q(f"""SELECT CAST(e.event_details:acting_as AS STRING) AS a FROM {self.t(c.T_EVENT)} e
            JOIN {self.t(c.T_ITEM)} i ON i.review_item_id = e.review_item_id
            WHERE e.event_type = 'DECISION_RECEIVED' AND i.table_name = 'billing' AND i.column_name = 'account_number'""")
        check("governance owner decision recorded as GOVERNANCE_OWNER", acting and acting[0]["a"] == "GOVERNANCE_OWNER", str(acting))
        check("each set processed once", len(self.events(c.EV_SUBMISSION_PROCESSED)) == 4)
        att = self.q(f"SELECT * FROM {self.t(c.T_ATTESTATION)} WHERE column_name = 'source_system_code'")[0]
        days = (datetime.fromisoformat(att["expires_at"][:19]) - datetime.fromisoformat(att["attested_at"][:19])).days
        check("HIGH-tier attestation expires in ~3 months", 88 <= days <= 93, str(days))
        plan = self.q(f"SELECT * FROM {self.t(c.T_PLAN)} WHERE column_name = 'legacy_flag'")[0]
        check("removal plan renders UNSET TAGS", plan["action_type"] == c.REMOVE_PHI_TAG and "UNSET TAGS" in plan["rendered_ddl"])
        if self.mv:
            mvp = self.q(f"SELECT rendered_ddl FROM {self.t(c.T_PLAN)} WHERE table_name = 'encounter_summary_mv'")[0]
            check("MV plan uses ALTER MATERIALIZED VIEW", mvp["rendered_ddl"].startswith("ALTER MATERIALIZED VIEW"))
        q = self.queue()
        check("returned items show the rejection reason in the App",
              "allowed" in (q[(cl, "lab_result", "patient_mrn")]["last_errors"] or ""), str(q.get((cl, "lab_result", "patient_mrn")))[:300])

        print("== 6. Plans and submissions are append-only")
        for tname in (c.T_PLAN, c.T_SUBMISSION):
            try:
                self.q(f"UPDATE {self.t(tname)} SET review_item_id = 'x'")
                check(f"UPDATE on {tname} rejected", False, "update succeeded")
            except RuntimeError as exc:
                check(f"UPDATE on {tname} rejected", "append" in str(exc).lower(), str(exc)[:200])

        print("== 7. Tag application")
        self.run_job("phi_classification_application")
        check("tag applied and verified", self.item(cl, "patient_encounter", "patient_name")["status"] == c.TAG_VERIFIED
              and self.tag_value(cl, "patient_encounter", "patient_name") == self.mapping["class.name"])
        check("corrected value applied", self.tag_value(cl, "patient_encounter", "encounter_date") == "date")
        check("unreviewed tag removed -> ATTESTED_NOT_PHI", self.item(cl, "lab_result", "legacy_flag")["status"] == c.ATTESTED_NOT_PHI
              and self.tag_value(cl, "lab_result", "legacy_flag") is None)
        if self.mv:
            check("MV column tagged", self.tag_value(cl, "encounter_summary_mv", "mrn") == "mrn")

        print("== 8. Privacy outcome, rejected-row fixes, and an ignored change")
        notes = self.item(cl, "patient_encounter", "notes")
        status, _ = self.submit("REVIEW", {(cl, "patient_encounter", "notes"): {
            "decision": c.CONFIRM_NOT_PHI, "steward_comment": "steward tries to close it"}},
            {(cl, "patient_encounter", "notes"): notes})
        check("App refuses a review decision on a Privacy item", status == 422)
        self.inject(c.SUB_REVIEW, notes, c.CONFIRM_NOT_PHI, by=self.me, comment="steward tries to close it")
        self.inject(c.SUB_PRIVACY, notes, c.CONFIRM_NOT_PHI, by="not.gov@example.com", comment="not from governance")
        self.run_job("phi_decision_ingestion")
        errs = self.errors_for(notes["review_item_id"])
        check("ingestion: review decision cannot resolve a Privacy item", "Privacy review queue" in errs, errs[:300])
        check("ingestion: non-governance Privacy outcome rejected", "to record a Privacy outcome" in errs, errs[:300])
        check("rejected Privacy outcomes keep the item in Privacy review",
              self.item(cl, "patient_encounter", "notes")["status"] == c.NEEDS_PRIVACY_REVIEW)
        pq = self.queue("privacy")
        status, body = self.submit("PRIVACY", {(cl, "patient_encounter", "notes"): {
            "decision": c.CONFIRM_NOT_PHI, "steward_comment": "Privacy: notes are system-generated"}}, pq)
        check("App accepts the Privacy outcome", status == 200, str(body)[:300])
        # A change to an accepted decision is ignored and reported.
        self.inject(c.SUB_REVIEW, self.item(cl, "patient_encounter", "patient_name"), c.CORRECT_CLASSIFICATION, by=self.me, corrected="mrn")
        q = self.queue()
        status, body = self.submit("REVIEW", {
            (cl, "lab_result", "patient_mrn"): {"decision": c.CORRECT_CLASSIFICATION, "corrected_tag_value": "mrn"},
            (cl, "lab_result", "result_value"): {"decision": c.CONFIRM_NOT_PHI, "steward_comment": "Lab value without identifiers"},
            (fi, "billing", "patient_email"): {"decision": c.APPROVE_SUGGESTION},
            (fi, "billing", "amount"): {"decision": c.CONFIRM_NOT_PHI, "steward_comment": "Amount only"},
        }, q)
        check("App accepts the fixes", status == 200, str(body)[:300])
        self.run_job("phi_decision_ingestion")
        self.run_job("phi_classification_application")
        check("privacy outcome -> ATTESTED_NOT_PHI", self.item(cl, "patient_encounter", "notes")["status"] == c.ATTESTED_NOT_PHI)
        check("change to accepted item ignored", len(self.events(c.EV_CHANGE_IGNORED)) >= 1
              and self.tag_value(cl, "patient_encounter", "patient_name") == self.mapping["class.name"])
        open_items = int(self.sql.scalar(
            f"SELECT count(*) FROM {self.t(c.T_ITEM)} WHERE status NOT IN ({', '.join(lit(s) for s in c.TERMINAL_STATUSES)})"))
        check("all items terminal", open_items == 0, str(open_items))
        closed = int(self.sql.scalar(f"SELECT count(*) FROM {self.t(c.T_BATCH)} WHERE status = 'CLOSED'"))
        check("both batches CLOSED", closed == 2, str(closed))
        check("BATCH_CLOSED events", len(self.events(c.EV_BATCH_CLOSED)) == 2)

        print("== 9. Rerunning ingestion processes nothing twice")
        before = len(self.events(c.EV_SUBMISSION_PROCESSED))
        self.run_job("phi_decision_ingestion")
        check("no set processed twice", len(self.events(c.EV_SUBMISSION_PROCESSED)) == before)

        print("== 10. Metadata changes reopen items; removed columns close")
        self.q(f"ALTER TABLE {fqn(self.cat, cl, 'patient_encounter')} ALTER COLUMN source_system_code COMMENT 'Changed purpose'")
        self.q(f"ALTER TABLE {fqn(self.cat, cl, 'patient_encounter')} ALTER COLUMN ssn COMMENT 'SSN (changed)'")
        self.q(f"ALTER TABLE {fqn(self.cat, cl, 'patient_encounter')} ALTER COLUMN notes COMMENT 'Free text notes (v2)'")
        self.q(f"CREATE OR REPLACE TABLE {fqn(self.cat, fi, 'temp_drop')} (x STRING)")
        self.run_job("phi_classification_scan", self.scan_params)
        check("STALE_NON_PHI_ATTESTATION", self.item(cl, "patient_encounter", "source_system_code", True)["issue_type"] == c.STALE_NON_PHI_ATTESTATION)
        check("STALE_PHI_REVIEW", self.item(cl, "patient_encounter", "ssn", True)["issue_type"] == c.STALE_PHI_REVIEW)
        check("new table -> MISSING_CLASSIFICATION", self.item(fi, "temp_drop", "x", True)["issue_type"] == c.MISSING_CLASSIFICATION)
        self.q(f"DROP TABLE {fqn(self.cat, fi, 'temp_drop')}")
        self.run_job("phi_classification_scan", self.scan_params)
        check("removed column -> CLOSED_NO_ACTION", self.item(fi, "temp_drop", "x")["status"] == c.CLOSED_NO_ACTION)

        print("== 10b. Re-decide reopened columns: new attestation and PHI tag both supersede the old attestation")
        q = self.queue()
        status, body = self.submit("REVIEW", {
            (cl, "patient_encounter", "source_system_code"): {"decision": c.CORRECT_CLASSIFICATION,
                                                              "corrected_tag_value": self.mapping["class.name"]},
            (cl, "patient_encounter", "notes"): {"decision": c.CONFIRM_NOT_PHI, "steward_comment": "Still system-generated"},
            (cl, "patient_encounter", "ssn"): {"decision": c.APPROVE_SUGGESTION},
        }, q)
        check("App accepts decisions on reopened items", status == 200, str(body)[:300])
        self.run_job("phi_decision_ingestion")
        self.run_job("phi_classification_application")

        def atts(col, active):
            cond = "IS NULL" if active else "IS NOT NULL"
            return int(self.sql.scalar(f"SELECT count(*) FROM {self.t(c.T_ATTESTATION)} WHERE table_name = 'patient_encounter' "
                                       f"AND column_name = {lit(col)} AND superseded_at {cond}"))

        check("re-attestation leaves exactly one active attestation", atts("notes", True) == 1 and atts("notes", False) == 1)
        check("verified PHI tag supersedes the non-PHI attestation",
              atts("source_system_code", True) == 0 and atts("source_system_code", False) == 1
              and self.tag_value(cl, "patient_encounter", "source_system_code") == self.mapping["class.name"])

        print("== 11. Correction requested in the App, then tag removal on a verified PHI column")
        col = f"{self.cat}.{cl}.patient_encounter.mrn"
        status, body = self.app("POST", "/api/review/corrections", {"column": col, "reason": "E2E correction test"})
        check("App starts the correction scan", status == 200 and bool(body.get("scanRunId")), str(body)[:300])
        self.wait_run(int(body["scanRunId"]))
        corr = self.item(cl, "patient_encounter", "mrn", True)
        check("CORRECTION_REQUESTED opened with current tag pre-filled",
              corr and corr["issue_type"] == c.CORRECTION_REQUESTED and corr["suggested_tag_value"] == self.mapping["class.medical_record_number"])
        check("CORRECTION_REQUESTED event records the requester",
              any(e["business_actor"] == self.me for e in self.events(c.EV_CORRECTION_REQUESTED)))
        status, body = self.submit("REVIEW", {(cl, "patient_encounter", "mrn"): {
            "decision": c.CONFIRM_NOT_PHI, "steward_comment": "Correction: surrogate key only"}}, self.queue())
        check("App accepts the corrected decision", status == 200, str(body)[:300])
        self.run_job("phi_decision_ingestion")
        self.run_job("phi_classification_application")
        check("correction removed tag and attested", self.item(cl, "patient_encounter", "mrn")["status"] == c.ATTESTED_NOT_PHI
              and self.tag_value(cl, "patient_encounter", "mrn") is None)
        prior = self.q(f"SELECT status FROM {self.t(c.T_ITEM)} WHERE column_name = 'mrn' AND table_name = 'patient_encounter' "
                       f"AND issue_type = 'MISSING_CLASSIFICATION'")
        check("original item unchanged as evidence", prior and prior[0]["status"] == c.TAG_VERIFIED)

        print("== 12. Data Governance maintains reference data in the App")
        status, body = self.app("PUT", "/api/review/scopes/SCOPE-FINANCE-DEV", {
            "catalog_name": self.cat, "schema_name": fi, "risk_tier": "HIGH", "steward_group": FINANCE_STEWARD,
            "governance_owner": self.me, "scan_enabled": True, "effective_date": "2026-01-01", "expiration_date": None})
        check("scope edit saved", status == 200, str(body)[:300])
        ev = self.q(f"SELECT CAST(event_details AS STRING) AS d, business_actor FROM {self.t(c.T_EVENT)} "
                    f"WHERE event_type = 'SCOPE_REGISTRY_UPDATED'")
        check("scope edit recorded with before/after and actor", bool(ev) and '"before"' in ev[0]["d"] and "STANDARD" in ev[0]["d"]
              and ev[0]["business_actor"] == self.me, str(ev)[:300])
        status, _ = self.app("PUT", "/api/review/scopes/SCOPE-OTHER-DEV", {
            "catalog_name": "other_catalog", "schema_name": "*", "risk_tier": "HIGH", "steward_group": FINANCE_STEWARD,
            "governance_owner": self.me, "scan_enabled": True, "effective_date": "2026-01-01"})
        check("scope outside this environment refused", status == 422)
        status, body = self.app("PUT", "/api/review/mappings/class.e2e_test", {
            "phi_tag_value": self.mapping["class.name"], "is_active": False, "notes": "E2E"})
        check("mapping edit saved and audited", status == 200 and len(self.events(c.EV_SUGGESTION_MAPPING_UPDATED)) == 1, str(body)[:300])
        status, _ = self.app("PUT", "/api/review/mappings/class.e2e_test", {"phi_tag_value": "not_a_value", "is_active": True})
        check("mapping to a value outside the policy refused", status == 422)

        print("== 13. Audit columns and writer constraints")
        bad = self.sql.scalar(
            f"SELECT count(*) FROM {self.t(c.T_ITEM)} WHERE audit_created_process NOT LIKE 'dev_phi_%' OR audit_updated_run_id IS NULL")
        check("item audit columns populated by jobs", int(bad) == 0)
        try:
            self.q(f"UPDATE {self.t(c.T_ITEM)} SET audit_updated_process = {lit(c.app_process_name('dev'))} WHERE true")
            check("App process rejected on job-only table", False, "update succeeded")
        except RuntimeError as exc:
            check("App process rejected on job-only table", "CHECK" in str(exc).upper(), str(exc)[:200])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--profile", required=True)
    ap.add_argument("--warehouse-id", required=True)
    ap.add_argument("--test-schema-prefix", default="phifw_test")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--steward-group", default="phifw_clinical_stewards",
                    help="Steward group for the synthetic scopes; the current user must be a member")
    ap.add_argument("--skip-mv", action="store_true")
    ap.add_argument("--mapping", default="reference_data/suggestion_mapping.csv",
                    help="Suggestion mapping CSV whose values match the tag's allowed values")
    a = ap.parse_args()
    try:
        E2E(a).run()
    finally:
        passed = sum(1 for _, ok, _ in RESULTS if ok)
        print(f"\n{passed}/{len(RESULTS)} checks passed")
    if any(not ok for _, ok, _ in RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
