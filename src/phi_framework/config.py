"""Runtime settings shared by every framework job.

All settings arrive as job parameters (see resources/*.job.yml), so each
environment is configured entirely by its bundle target.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field

from phi_framework import constants as c

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def _bool(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


@dataclass(frozen=True)
class Settings:
    env: str
    process_name: str
    run_id: str
    governance_catalog: str
    governance_schema: str
    governed_catalog_prefix: str
    tag_key: str
    governance_group: str
    data_classification_table: str = "system.data_classification.results"
    review_due_business_days: int = 10
    business_timezone: str = "UTC"
    detect_orphaned_tags: bool = False
    retry_failed: bool = False
    workbook_protection_password: str = "phi-review"
    # Correction request (scan job, on demand only)
    correction_column: str = ""
    correction_reason: str = ""
    correction_requested_by: str = ""
    extra: dict = field(default_factory=dict)

    # ---- derived names ----
    @property
    def schema_fqn(self) -> str:
        return f"`{self.governance_catalog}`.`{self.governance_schema}`"

    def table(self, name: str) -> str:
        return f"{self.schema_fqn}.`{name}`"

    @property
    def volume_path(self) -> str:
        return f"/Volumes/{self.governance_catalog}/{self.governance_schema}/{c.V_FILES}"

    @property
    def inbound_path(self) -> str:
        return f"{self.volume_path}/inbound"

    @property
    def outbound_path(self) -> str:
        return f"{self.volume_path}/outbound"

    def validate(self, expected_role: str | None = None) -> None:
        if self.env not in c.ENVIRONMENTS:
            raise ValueError(f"env must be one of {c.ENVIRONMENTS}, got {self.env!r}")
        for name in ("governance_catalog", "governance_schema"):
            if not _IDENT.match(getattr(self, name)):
                raise ValueError(f"{name} contains unsupported characters: {getattr(self, name)!r}")
        if not self.governed_catalog_prefix or not _IDENT.match(self.governed_catalog_prefix):
            raise ValueError(f"governed_catalog_prefix is invalid: {self.governed_catalog_prefix!r}")
        if expected_role is not None and self.process_name != f"{self.env}_{expected_role}":
            raise ValueError(
                f"process_name {self.process_name!r} does not match this job; expected "
                f"{self.env}_{expected_role!r}. Pass the logical job name, not the deployed name."
            )
        if not self.run_id or not self.run_id.isdigit():
            raise ValueError(f"run_id must be the Lakeflow job run id, got {self.run_id!r}")


def parse_args(argv: list[str] | None = None) -> Settings:
    p = argparse.ArgumentParser(description="PHI classification framework job")
    p.add_argument("--env", required=True)
    p.add_argument("--process-name", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--governance-catalog", required=True)
    p.add_argument("--governance-schema", required=True)
    p.add_argument("--governed-catalog-prefix", required=True)
    p.add_argument("--tag-key", required=True)
    p.add_argument("--governance-group", required=True)
    p.add_argument("--data-classification-table", default="system.data_classification.results")
    p.add_argument("--review-due-business-days", default="10")
    p.add_argument("--business-timezone", default="UTC")
    p.add_argument("--detect-orphaned-tags", default="false")
    p.add_argument("--retry-failed", default="false")
    p.add_argument("--workbook-protection-password", default="phi-review")
    p.add_argument("--correction-column", default="")
    p.add_argument("--correction-reason", default="")
    p.add_argument("--correction-requested-by", default="")
    a = p.parse_args(argv)
    return Settings(
        env=a.env.strip(),
        process_name=a.process_name.strip(),
        run_id=a.run_id.strip(),
        governance_catalog=a.governance_catalog.strip(),
        governance_schema=a.governance_schema.strip(),
        governed_catalog_prefix=a.governed_catalog_prefix.strip(),
        tag_key=a.tag_key.strip(),
        governance_group=a.governance_group.strip(),
        data_classification_table=a.data_classification_table.strip(),
        review_due_business_days=int(a.review_due_business_days),
        business_timezone=a.business_timezone.strip(),
        detect_orphaned_tags=_bool(a.detect_orphaned_tags),
        retry_failed=_bool(a.retry_failed),
        workbook_protection_password=a.workbook_protection_password,
        correction_column=a.correction_column.strip(),
        correction_reason=a.correction_reason.strip(),
        correction_requested_by=a.correction_requested_by.strip().lower(),
    )
