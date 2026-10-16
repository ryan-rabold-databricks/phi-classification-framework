import pytest

from phi_framework import constants as c
from phi_framework.tag_ddl import render_tag_ddl


def test_set_and_unset_per_object_type():
    assert render_tag_ddl(c.SET_PHI_TAG, "MANAGED", "cat", "sch", "tbl", "col", "phi_classification", "name") == (
        "ALTER TABLE `cat`.`sch`.`tbl` ALTER COLUMN `col` SET TAGS ('phi_classification' = 'name')"
    )
    assert render_tag_ddl(c.REMOVE_PHI_TAG, "STREAMING_TABLE", "cat", "sch", "st", "col", "phi_classification", None) == (
        "ALTER STREAMING TABLE `cat`.`sch`.`st` ALTER COLUMN `col` UNSET TAGS ('phi_classification')"
    )
    assert render_tag_ddl(c.SET_PHI_TAG, "MATERIALIZED_VIEW", "c", "s", "mv", "x", "k", "v").startswith("ALTER MATERIALIZED VIEW")


def test_identifiers_and_values_are_escaped():
    ddl = render_tag_ddl(c.SET_PHI_TAG, "EXTERNAL", "c", "s", "we`ird", "co l", "k", "o'brien")
    assert "`we``ird`" in ddl and "`co l`" in ddl and "'o\\'brien'" in ddl


def test_rejects_unsupported():
    with pytest.raises(ValueError):
        render_tag_ddl(c.SET_PHI_TAG, "VIEW", "c", "s", "t", "x", "k", "v")
    with pytest.raises(ValueError):
        render_tag_ddl(c.SET_PHI_TAG, "MANAGED", "c", "s", "t", "x", "k", None)
