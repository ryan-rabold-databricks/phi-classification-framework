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
