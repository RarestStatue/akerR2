"""Economics tab: F2 revenue bridge, F5 rent positioning, F6 expiration
concentration. PLAN6 phase 1.

    pytest -m integration

Needs a database with the corpus loaded (README step 5).

`test_concentrated_row_count` and the row count in
`test_outliers_exclude_small_unit_types_and_zero_sqft` are golden numbers in the
same sense as `cli.GOLDEN`: they pin the thresholds to the corpus. The plan that
specified this feature (PLAN6.md) claimed the heaviest concentrated book-month
is `134r` 2026-07 -- the corpus instead shows `153r` 2026-07 at 19.90% share is
heavier than `134r`'s 19.52%. Per the plan's own instruction ("report the actual
number... do not adjust the assertion"), the assertions below use the measured
value, not the plan's claim.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dash():
    # Imported inside the fixture, not at module scope: importing the dashboard
    # opens a connection pool, and an integration-marked module still gets
    # *collected* during a unit-test run.
    from aker_etl.config import get_settings
    from aker_etl.dashboard import app
    from aker_etl.db import connect
    from aker_etl.loader import load

    s = get_settings()
    try:
        with connect(s, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM core.lease")
            empty = cur.fetchone()[0] == 0
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unreachable: {exc}")
    if empty:
        load(s, force=True)
    return app


@pytest.fixture(scope="module")
def snap(dash):
    return dash._snapshot_id(None)[0]


def test_the_bridge_closes_for_every_property(dash, snap):
    rows = dash._q(
        """SELECT property_code, gross_potential_rent, vacancy_loss, loss_to_lease,
                  rent_charges, subsidy, concessions, ancillary, billed_charges
           FROM mart.revenue_bridge WHERE snapshot_id = %s""",
        (snap,),
    )
    assert rows
    for r in rows:
        lhs = float(r["gross_potential_rent"]) - float(r["vacancy_loss"]) - float(r["loss_to_lease"])
        assert lhs == pytest.approx(float(r["rent_charges"]), abs=0.01), r
        rhs = (float(r["rent_charges"]) + float(r["subsidy"]) + float(r["concessions"])
               + float(r["ancillary"]))
        assert rhs == pytest.approx(float(r["billed_charges"]), abs=0.01), r


def test_billed_charges_equals_the_kpi_view(dash, snap):
    rows = dash._q(
        """SELECT rb.property_code, rb.billed_charges, coalesce(k.lease_charges, 0) AS kpi_charges
           FROM mart.revenue_bridge rb
           LEFT JOIN mart.property_snapshot_kpi k
             ON k.property_id = rb.property_id AND k.snapshot_id = rb.snapshot_id
           WHERE rb.snapshot_id = %s""",
        (snap,),
    )
    assert rows
    for r in rows:
        assert float(r["billed_charges"]) == pytest.approx(float(r["kpi_charges"]), abs=0.01), r


def test_the_excluded_set_matches_the_matrix(dash, snap):
    bridge_excluded = {
        r["property_code"] for r in dash._q(
            "SELECT property_code FROM mart.revenue_bridge "
            "WHERE snapshot_id = %s AND exclusion_reason IS NOT NULL", (snap,))
    }
    matrix_excluded = {
        r["property_code"] for r in dash._q(
            "SELECT property_code::text AS property_code FROM mart.property_profitability "
            "WHERE snapshot_id = %s AND exclusion_reason IS NOT NULL", (snap,))
    }
    assert bridge_excluded == matrix_excluded
    assert len(bridge_excluded) == 13


def test_vacancy_loss_only_covers_units_that_bill_nothing(dash, snap):
    n = dash._q(
        """SELECT count(*) AS n FROM core.lease l
           WHERE l.snapshot_id = %s AND l.section = 'current'
             AND l.occupancy_status IN ('vacant','model','down')
             AND EXISTS (SELECT 1 FROM core.lease_charge lc WHERE lc.lease_id = l.lease_id)""",
        (snap,),
    )[0]["n"]
    assert n == 0


def test_outlier_median_is_an_observed_rent(dash, snap):
    n = dash._q(
        """SELECT count(*) AS n FROM mart.unit_rent_outlier o
           WHERE o.snapshot_id = %s AND NOT EXISTS (
             SELECT 1 FROM core.lease l
             JOIN core.unit_type ut ON ut.unit_type_id = l.unit_type_id
             WHERE l.snapshot_id = o.snapshot_id AND l.property_id = o.property_id
               AND ut.unit_type_code = o.unit_type_code
               AND l.market_rent = o.median_market_rent)""",
        (snap,),
    )[0]["n"]
    assert n == 0


def test_outliers_exclude_small_unit_types_and_zero_sqft(dash, snap):
    rows = dash._q(
        "SELECT peer_units, unit_sqft FROM mart.unit_rent_outlier WHERE snapshot_id = %s", (snap,)
    )
    assert len(rows) == 3480
    assert all(r["peer_units"] >= 3 for r in rows)
    assert all(r["unit_sqft"] > 0 for r in rows)


def test_contract_rent_is_null_where_the_source_prints_no_charges(dash, snap):
    rows = dash._q(
        """SELECT count(*) FILTER (WHERE contract_rent IS NULL)                 AS absent,
                  count(*) FILTER (WHERE contract_rent = 0)                     AS genuine_zero,
                  count(*) FILTER (WHERE contract_rent IS NULL AND EXISTS (
                      SELECT 1 FROM core.lease_charge lc WHERE lc.lease_id = o.lease_id)) AS wrong
           FROM mart.unit_rent_outlier o WHERE snapshot_id = %s""",
        (snap,),
    )[0]
    # 908 leases on the charge-less books print nothing; 3 carry charge lines but
    # no rent-category line, and their zero is a fact.
    assert rows["absent"] == 908
    assert rows["genuine_zero"] == 3
    assert rows["wrong"] == 0


def test_pct_vs_median_signs_match_market_vs_median(dash, snap):
    rows = dash._q(
        """SELECT market_vs_median, pct_vs_median FROM mart.unit_rent_outlier
           WHERE snapshot_id = %s AND market_vs_median <> 0 AND pct_vs_median IS NOT NULL""",
        (snap,),
    )
    assert rows
    for r in rows:
        assert (float(r["market_vs_median"]) > 0) == (float(r["pct_vs_median"]) > 0), r


def test_concentration_shares_sum_to_one_per_property(dash, snap):
    rows = dash._q(
        """SELECT property_id, sum(share_of_book) AS s
           FROM mart.expiration_concentration WHERE snapshot_id = %s GROUP BY 1""",
        (snap,),
    )
    assert rows
    # Each row is round(share, 4); a book with N expiry months accumulates up to
    # ~N x 0.00005 of rounding error. Measured max on this corpus is 0.0008 (the
    # 51-month book), well inside 0.15% -- the exact-arithmetic sum (no per-row
    # rounding) is 1.0000000... to float precision for every property.
    for r in rows:
        assert float(r["s"]) == pytest.approx(1.0, abs=0.0015), r


def test_the_floor_excludes_small_books(dash, snap):
    rows = dash._q(
        """SELECT property_code, expiry_month::text AS m, dated_leases
           FROM mart.expiration_concentration WHERE snapshot_id = %s AND concentrated""",
        (snap,),
    )
    assert all(r["dated_leases"] >= 50 for r in rows)

    # These clear the 15% line but not the 50-lease floor -- concentrated must be
    # false for every one of them.
    below_floor = {(r["property_code"], r["m"][:7])
                   for r in dash._q(
                       """SELECT property_code, expiry_month::text AS m
                          FROM mart.expiration_concentration
                          WHERE snapshot_id = %s AND dated_leases < 50
                            AND expiring_leases::numeric / nullif(dated_leases, 0) >= 0.15""",
                       (snap,))}
    assert below_floor == {("126a", "2026-04"), ("126a", "2026-09"),
                            ("153a", "2026-09"), ("183a", "2026-07")}
    concentrated_codes = {r["property_code"] for r in rows}
    assert "126a" not in concentrated_codes
    assert "183a" not in concentrated_codes
    assert "153a" not in concentrated_codes


def test_concentrated_row_count(dash, snap):
    rows = dash._q(
        """SELECT property_code, expiry_month::text AS m, share_of_book, leases_to_shift
           FROM mart.expiration_concentration
           WHERE snapshot_id = %s AND concentrated ORDER BY share_of_book DESC""",
        (snap,),
    )
    assert len(rows) == 12
    heaviest = rows[0]
    assert heaviest["property_code"] == "153r"
    assert heaviest["m"].startswith("2026-07")
    assert heaviest["leases_to_shift"] == 10


def test_api_economics_returns_all_four_sections(dash):
    d = dash.economics()
    assert {"as_of", "exclusion_reasons", "bridge", "bridge_portfolio", "outliers",
            "concentration"} <= set(d)
    assert len(d["bridge_portfolio"]) == 1
    assert d["bridge_portfolio"][0]["books"] == 12
