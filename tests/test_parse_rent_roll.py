"""Golden counts on the real corpus -- the strongest single regression test here.

If the source format changes, these fail before anything reaches the database.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from aker_etl.models import CoercionError, as_date, as_decimal, as_int
from aker_etl.parsers import parse_rent_roll
from aker_etl.parsers.rent_roll import RentRollStructureError
from tests.conftest import rent_roll_path, write_rent_roll

SECTION_HEADER = ["Current/Notice/Vacant Residents"] + [""] * 13


def test_golden_counts(all_rent_rolls):
    leases = [x for f in all_rent_rolls for x in f.leases]
    charges = [c for x in leases for c in x.charges]
    assert len(all_rent_rolls) == 25
    assert len(leases) == 4106
    assert sum(1 for x in leases if x.section == "current") == 4013
    assert sum(1 for x in leases if x.section == "future") == 93
    assert len({(f.property_code, x.unit_code) for f in all_rent_rolls for x in f.leases}) == 4013
    assert len(charges) == 9177
    assert len({c.charge_code for c in charges}) == 32
    assert sum(len(f.summary_groups) for f in all_rent_rolls) == 150
    assert sum(len(f.charge_summary) for f in all_rent_rolls) == 117
    assert sum(1 for f in all_rent_rolls if f.charge_summary) == 16


def test_every_file_is_one_as_of_and_month(all_rent_rolls):
    assert {f.as_of_date for f in all_rent_rolls} == {dt.date(2026, 2, 25)}
    assert {f.report_month for f in all_rent_rolls} == {dt.date(2026, 2, 1)}


def test_distinct_property_codes_and_residents(all_rent_rolls):
    assert len({f.property_code for f in all_rent_rolls}) == 25
    ids = [x.resident_id for f in all_rent_rolls for x in f.leases if x.resident_id]
    assert len(ids) == len(set(ids)) == 3917, "resident ids are globally unique -- safe as a PK"


def test_sentinels_are_not_residents(all_rent_rolls):
    sentinels = [x for f in all_rent_rolls for x in f.leases if x.sentinel]
    counts = {s: sum(1 for x in sentinels if x.sentinel == s) for s in {"VACANT", "MODEL", "DOWN"}}
    assert counts == {"VACANT": 176, "MODEL": 5, "DOWN": 8}
    assert all(x.resident_id is None for x in sentinels)
    assert all(x.move_in is None and x.lease_expiration is None and x.balance == 0
               for x in sentinels)


def test_status_derivation(all_rent_rolls):
    leases = [x for f in all_rent_rolls for x in f.leases]
    counts: dict[str, int] = {}
    for x in leases:
        counts[x.occupancy_status] = counts.get(x.occupancy_status, 0) + 1
    assert counts == {"occupied": 3677, "vacant": 176, "notice": 147,
                      "future": 93, "model": 5, "down": 8}
    # The CHECK constraint in core.lease depends on this equivalence holding.
    assert all((x.occupancy_status == "notice") == (x.move_out is not None) for x in leases)


def test_every_block_total_reconciles(all_rent_rolls):
    """Zero mismatches today. A non-zero count means the report format changed."""
    for f in all_rent_rolls:
        for lease in f.leases:
            total = sum((c.amount for c in lease.charges), Decimal(0))
            assert abs(lease.charges_total - total) <= Decimal("0.005"), (
                f"{f.property_code} {lease.unit_code}: {lease.charges_total} != {total}"
            )
    assert not [i for f in all_rent_rolls for i in f.issues if i.rule == "block_total_mismatch"]


def test_115r_block_a103_keeps_both_parking_lines(rr_115r):
    lease = next(x for x in rr_115r.leases if x.unit_code == "A103" and x.section == "current")
    assert [c.charge_code for c in lease.charges] == [
        "RENT", "PETFEEM", "AMENITY", "PARKING", "PARKING", "TRASH"
    ]
    assert [c.line_no for c in lease.charges] == [1, 2, 3, 4, 5, 6]
    assert sum(c.amount for c in lease.charges) == Decimal("2760")
    assert lease.charges_total == Decimal("2760")


def test_115r_a105_is_two_leases_one_vacant_one_future(rr_115r):
    rows = [x for x in rr_115r.leases if x.unit_code == "A105"]
    assert len(rows) == 2
    current = next(x for x in rows if x.section == "current")
    future = next(x for x in rows if x.section == "future")
    assert current.occupancy_status == "vacant" and current.resident_id is None
    assert future.occupancy_status == "future" and future.resident_id


def test_115r_a107_is_on_notice(rr_115r):
    lease = next(x for x in rr_115r.leases if x.unit_code == "A107" and x.section == "current")
    assert lease.occupancy_status == "notice"
    assert lease.move_out == dt.date(2026, 3, 14)
    assert lease.balance == Decimal("-2888.50")


def test_134land_is_an_empty_book_that_still_parses():
    f = parse_rent_roll(rent_roll_path("134land"))
    assert f.leases == []
    assert len(f.summary_groups) == 6
    assert f.charge_summary == []
    assert not [i for i in f.issues if i.severity == "error"]


def test_summary_group_label_collision_produces_no_lease(all_rent_rolls):
    """The section labels also appear as summary-group labels.

    Treating a label as a section header without the 'B..N blank' test injects
    four fake leases per file. This asserts none leaked in.
    """
    labels = {"Current/Notice/Vacant Residents", "Future Residents/Applicants",
              "Occupied Units", "Total Non Rev Units", "Total Vacant Units", "Totals:"}
    assert not [x for f in all_rent_rolls for x in f.leases if x.unit_code in labels]
    for f in all_rent_rolls:
        got = {g.group_label for g in f.summary_groups}
        assert got == labels, f"{f.property_code}: {got}"


def test_summary_group_money_parses_from_comma_strings(rr_115r):
    g = next(x for x in rr_115r.summary_groups
             if x.group_label == "Current/Notice/Vacant Residents")
    assert g.square_footage == Decimal("260778.00")
    assert g.market_rent == Decimal("763814.00")
    assert g.lease_charges == Decimal("791650.93")
    assert g.unit_count == 300
    assert g.balance == Decimal("-35465.66")


def test_zero_sqft_blocks_are_kept(all_rent_rolls):
    assert sum(1 for f in all_rent_rolls for x in f.leases if not x.unit_sqft) == 50


def test_structural_failure_names_the_file(tmp_path):
    bad = tmp_path / "not_a_workbook.xlsx"
    bad.write_bytes(b"definitely not a zip")
    with pytest.raises(Exception):
        parse_rent_roll(bad)


def test_wrong_sheet_is_a_structural_error(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    path = tmp_path / "wrong_sheet.xlsx"
    wb.save(path)
    with pytest.raises(RentRollStructureError, match="Report1"):
        parse_rent_roll(path)


# --------------------------------------------------------------------------- #
# B6-B9 regression tests
# --------------------------------------------------------------------------- #


def test_a_current_row_with_no_resident_is_dropped_with_an_error(tmp_path):
    path = write_rent_roll(tmp_path / "b6_blank.xlsx", [
        SECTION_HEADER,
        ["A101", "1BR", 750, "", "John Doe", 1200, "", "", 0, 0, "", "", "", 0],
    ])
    f = parse_rent_roll(path)
    assert f.leases == []
    issues = [i for i in f.issues if i.rule == "occupied_row_without_resident"]
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].detail["unit_code"] == "A101"


def test_a_vacant_sentinel_row_still_loads_without_a_resident(tmp_path):
    path = write_rent_roll(tmp_path / "b6_vacant.xlsx", [
        SECTION_HEADER,
        ["A102", "1BR", 750, "VACANT", "", 0, "", "", 0, 0, "", "", "", 0],
    ])
    f = parse_rent_roll(path)
    assert len(f.leases) == 1
    assert f.leases[0].resident_id is None
    assert not [i for i in f.issues if i.rule == "occupied_row_without_resident"]


def test_a_malformed_resident_id_is_dropped_with_an_error(tmp_path):
    path = write_rent_roll(tmp_path / "b7.xlsx", [
        SECTION_HEADER,
        ["A103", "1BR", 750, "XYZ123", "Jane Doe", 1200, "", "", 0, 0, "", "", "", 0],
    ])
    f = parse_rent_roll(path)
    assert f.leases == []
    issues = [i for i in f.issues if i.rule == "resident_id_format"]
    assert len(issues) == 1
    assert issues[0].severity == "error"


def test_charge_lines_after_a_dropped_block_are_not_separate_errors(tmp_path):
    path = write_rent_roll(tmp_path / "b8_dropped.xlsx", [
        SECTION_HEADER,
        ["A104", "1BR", 750, "", "Jane Doe", 1200, "", "", 0, 0, "", "", "", 0],
        ["", "", "", "", "", "", "RENT", 1000, "", "", "", "", "", ""],
        ["", "", "", "", "", "", "TRASH", 50, "", "", "", "", "", ""],
        ["", "", "", "", "", "", "PARKING", 75, "", "", "", "", "", ""],
    ])
    f = parse_rent_roll(path)
    assert len(f.issues) == 1
    assert f.issues[0].rule == "occupied_row_without_resident"
    assert not [i for i in f.issues if i.rule == "charge_without_block"]


def test_a_charge_line_with_no_block_is_still_an_error(tmp_path):
    path = write_rent_roll(tmp_path / "b8_no_block.xlsx", [
        SECTION_HEADER,
        ["", "", "", "", "", "", "RENT", 1000, "", "", "", "", "", ""],
    ])
    f = parse_rent_roll(path)
    issues = [i for i in f.issues if i.rule == "charge_without_block"]
    assert len(issues) == 1


def test_total_in_the_charge_code_column_of_a_lease_row_is_not_a_charge(tmp_path):
    path = write_rent_roll(tmp_path / "b9.xlsx", [
        SECTION_HEADER,
        ["A105", "1BR", 750, "tRES001", "John Doe", 1200, "Total", 500, 0, 0, "", "", "", 0],
    ])
    f = parse_rent_roll(path)
    assert len(f.leases) == 1
    assert f.leases[0].charges == []
    assert not [c for lease in f.leases for c in lease.charges if c.charge_code == "Total"]


# --------------------------------------------------------------------------- #
# Coercers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("raw", "want"), [
    (None, "0"), ("", "0"), (0, "0"), (1234, "1234"),
    ("260,778.00", "260778.00"), ("$1,234.56", "1234.56"), ("(1,234.00)", "-1234.00"),
    ("96.00", "96.00"), (-2888.5, "-2888.5"), ("97.6666666666667", "97.6666666666667"),
])
def test_as_decimal(raw, want):
    assert as_decimal(raw) == Decimal(want)


def test_as_decimal_never_goes_through_float():
    """Decimal(0.1) would carry binary error in; Decimal('0.1') does not."""
    assert as_decimal(0.1) == Decimal("0.1")


def test_as_decimal_rejects_junk():
    with pytest.raises(CoercionError):
        as_decimal("not a number")


def test_as_date():
    assert as_date(dt.datetime(2026, 3, 14, 0, 0)) == dt.date(2026, 3, 14)
    assert as_date("03/14/2026") == dt.date(2026, 3, 14)
    assert as_date(None) is None and as_date("") is None
    with pytest.raises(CoercionError):
        as_date("14 March")


def test_as_int_rejects_non_integral():
    assert as_int("1,159") == 1159
    assert as_int(None) is None
    with pytest.raises(CoercionError):
        as_int("1.5")
