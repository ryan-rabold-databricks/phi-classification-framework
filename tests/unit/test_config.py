import re

import pytest
from fakes import settings

from phi_framework import constants as c
from phi_framework.config import parse_args

ARGS = [
    "--env", "dev", "--process-name", "dev_phi_classification_scan", "--run-id", "42",
    "--governance-catalog", "dev_gov", "--governance-schema", "data_governance",
    "--governed-catalog-prefix", "dev_", "--tag-key", "phi_classification", "--governance-group", "gov",
]


def test_parse_args_normalizes():
    s = parse_args(ARGS + ["--detect-orphaned-tags", "yes", "--correction-requested-by", " Gov@Example.com "])
    assert s.detect_orphaned_tags is True and s.correction_requested_by == "gov@example.com"
    s.validate(expected_role=c.ROLE_SCAN)


@pytest.mark.parametrize(
    "overrides,role",
    [
        ({"env": "qa", "allowed_values_override": "a,b"}, None),
        ({"env": "prod", "allowed_values_override": "a"}, None),
        ({"process_name": "dev_phi_classification_scan"}, c.ROLE_APPLICATION),
        ({"run_id": "abc"}, None),
        ({"governance_catalog": "a-b"}, None),
        ({"env": "stage"}, None),
        ({"governed_catalog_prefix": ""}, None),
        ({"excluded_table_regex": "("}, None),
    ],
)
def test_validate_rejects(overrides, role):
    with pytest.raises((ValueError, re.error)):
        settings(**overrides).validate(expected_role=role)


def test_override_allowed_in_dev():
    settings(allowed_values_override="a,b").validate()
