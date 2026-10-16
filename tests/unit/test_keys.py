from phi_framework.keys import fingerprint, make_key


def test_make_key_is_deterministic_and_prefixed():
    k = make_key("ITEM", ["cat", "sch", "tbl", "col", "BATCH-1"])
    assert k == make_key("ITEM", ["cat", "sch", "tbl", "col", "BATCH-1"])
    assert k.startswith("ITEM-") and len(k) == len("ITEM-") + 16
    assert k[5:].upper() == k[5:]


def test_make_key_distinguishes_null_empty_and_boundaries():
    assert make_key("X", [None]) != make_key("X", [""])
    assert make_key("X", ["ab", "c"]) != make_key("X", ["a", "bc"])


def test_fingerprint_normalizes_case_and_whitespace():
    a = fingerprint("Cat", " sch ", "TBL", "Col", "string", " Patient Name ")
    b = fingerprint("cat", "sch", "tbl", "col", "STRING", "patient name")
    assert a == b
    assert fingerprint("cat", "sch", "tbl", "col", "STRING", None) == fingerprint("cat", "sch", "tbl", "col", "STRING", "")
    assert a != fingerprint("cat", "sch", "tbl", "col", "STRING", "changed")


def test_default_exclusion_regex_skips_pipeline_internal_tables():
    import re

    from phi_framework.config import DEFAULT_EXCLUDED_TABLE_REGEX

    rx = re.compile(DEFAULT_EXCLUDED_TABLE_REGEX)
    assert rx.search("__materialization_mat_8aa28cfc_fbd8_40b7_81d6_7a8f9fca284f_mv_1")
    assert rx.search("event_log_8aa28cfc_fbd8_40b7_81d6_7a8f9fca284f")
    assert not rx.search("event_log_business")
    assert not rx.search("patient_encounter")


def test_fingerprint_matches_spark_trim_semantics():
    # Spark trim() removes spaces only; tabs/newlines are significant, as in the SQL fingerprint.
    assert fingerprint("c", "s", "t", "x", "STRING", "note\n") != fingerprint("c", "s", "t", "x", "STRING", "note")
    assert fingerprint("c", "s", "t", "x", "STRING", "  note  ") == fingerprint("c", "s", "t", "x", "STRING", "note")
