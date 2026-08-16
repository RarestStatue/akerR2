from __future__ import annotations

import datetime as dt
from decimal import Decimal

from aker_etl.parsers import parse_availability
from tests.conftest import availability_path


def test_one_row_per_file(all_availability):
    assert len(all_availability) == 25
    assert len({f.property_code for f in all_availability}) == 25
    assert {f.as_of_date for f in all_availability} == {dt.date(2026, 2, 25)}
    assert all(f.sheet_rows == 7 for f in all_availability)


def test_115r_values():
    f = parse_availability(availability_path("115r"))
    assert f.property_code == "115r"
    assert f.property_name == "Canfield Park"
    assert f.units == 300
    assert f.occupied_no_notice == 270
    assert f.available == 21
    assert f.vacant_rented == 5 and f.vacant_unrented == 7
    assert f.notice_rented == 4 and f.notice_unrented == 14
    assert round(f.pct_leased, 4) == Decimal("97.6667")
    assert f.issues == []


def test_total_row_is_ignored_but_still_checked(all_availability):
    """Row 7 duplicates row 6 and is not loaded.

    It differs from row 6 only in pct_trend, and only on the two zero-occupancy
    commercial books -- an upstream rounding artifact, recorded as info.
    """
    flagged = {f.property_code: [i for i in f.issues] for f in all_availability if f.issues}
    assert set(flagged) == {"134c", "139c"}
    for code, issues in flagged.items():
        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert issues[0].rule == "availability_total_row_differs"
        assert issues[0].detail["field"] == "pct_trend"


def test_153c_reports_zero_units_while_the_rent_roll_has_seven():
    """The known upstream discrepancy. A warning at load time, never a failure."""
    f = parse_availability(availability_path("153c"))
    assert f.units == 0
    assert f.occupied_no_notice == 0
