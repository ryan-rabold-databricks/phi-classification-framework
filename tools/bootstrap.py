"""Run the framework setup DDL through a SQL warehouse, before the bundle is deployed.

Why: Lakeflow Jobs validates trigger targets when a job is created, so the
ingestion job (table-update trigger on classification_review_submission) and the
application job (table-update trigger on classification_application_plan) cannot
be deployed until those tables exist. The review App's Unity Catalog resources
(table and function grants) likewise require the objects to exist. This tool executes exactly the same
``phi_framework.setup`` code the setup job runs, so deployments are
order-independent and idempotent.

Usage:
  python tools/bootstrap.py -p <profile> --warehouse-id <id> -t <target>
(target variables are read from `databricks bundle validate -o json`)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from databricks.sdk import WorkspaceClient
from sqlexec import Sql

from phi_framework.config import Settings
from phi_framework.setup import Setup


class _Result:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def collect(self):
        return [_Row(r) for r in self._rows]


class _Row(dict):
    def asDict(self, recursive: bool = False):
        return dict(self)


class WarehouseRuntime:
    """Duck-typed stand-in for phi_framework.runtime.Runtime (sql/rows/w/s only)."""

    def __init__(self, settings: Settings, w: WorkspaceClient, warehouse_id: str):
        self.s = settings
        self.w = w
        self._sql = Sql(w, warehouse_id)

    def sql(self, statement: str):
        return _Result(self._sql.run(statement))

    def rows(self, statement: str) -> list[dict]:
        return self._sql.run(statement)


def bundle_variables(target: str, profile: str | None) -> dict:
    cmd = ["databricks", "bundle", "validate", "-t", target, "-o", "json"]
    if profile:
        cmd += ["-p", profile]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return {k: v.get("value") for k, v in json.loads(out).get("variables", {}).items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--profile", default=None)
    ap.add_argument("-t", "--target", required=True)
    ap.add_argument("--warehouse-id", required=True)
    a = ap.parse_args()
    v = bundle_variables(a.target, a.profile)
    settings = Settings(
        env=v["env"],
        process_name=f"{v['env']}_phi_framework_setup",
        run_id="0",
        governance_catalog=v["governance_catalog"],
        governance_schema=v["governance_schema"],
        governed_catalog_prefix=v["governed_catalog_prefix"],
        tag_key=v["tag_key"],
        governance_group=v["governance_group"],
    )
    settings.validate()
    w = WorkspaceClient(profile=a.profile) if a.profile else WorkspaceClient()
    Setup(WarehouseRuntime(settings, w, a.warehouse_id)).run()
    print(f"Bootstrap complete: {settings.schema_fqn}")


if __name__ == "__main__":
    main()
