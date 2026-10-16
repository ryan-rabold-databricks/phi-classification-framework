from datetime import datetime

from phi_framework.batches import due_at_utc


def test_due_at_local_five_pm_after_business_days():
    # Fri 2026-10-16 23:30 UTC = 19:30 EDT Friday -> 1 business day -> Mon 17:00 EDT = 21:00 UTC
    assert due_at_utc(datetime(2026, 10, 16, 23, 30), 1, "America/New_York") == datetime(2026, 10, 19, 21, 0)


def test_due_at_local_date_boundary():
    # Sat 01:00 UTC is still Friday evening in New York
    assert due_at_utc(datetime(2026, 10, 17, 1, 0), 1, "America/New_York") == datetime(2026, 10, 19, 21, 0)


def test_due_at_crosses_dst_end():
    # Mon 2026-10-26 + 10 business days = Mon 2026-11-09, after DST ends (EST, UTC-5) -> 22:00 UTC
    assert due_at_utc(datetime(2026, 10, 26, 12, 0), 10, "America/New_York") == datetime(2026, 11, 9, 22, 0)


def test_due_at_utc_zone():
    assert due_at_utc(datetime(2026, 10, 16, 8, 0), 10, "UTC") == datetime(2026, 10, 30, 17, 0)
