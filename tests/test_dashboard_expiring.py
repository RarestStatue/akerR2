"""/api/leases/expiring must agree with the chart it drills into (PLAN4 change C).

    pytest -m integration

Needs a database with the corpus loaded (README step 5).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dash():
    # Imported inside the fixture, not at module scope: importing the dashboard
    # opens a connection pool, and an integration-marked module still gets
    # *collected* during a unit-test run.
    from aker_etl.dashboard import app

    return app


def test_every_bar_total_matches_the_view(dash):
    """The drill-down is the bar. If these ever disagree, one of the two
    predicates was edited without the other."""
    snap, _ = dash._snapshot_id(None)
    months = dash._q(
        """SELECT expiry_month::text AS expiry_month, sum(expiring_leases) AS n
           FROM mart.expiration_schedule WHERE snapshot_id = %s GROUP BY 1 ORDER BY 1""",
        (snap,),
    )
    assert months, "no expiration rows -- run `aker-etl load` first"
    for m in months:
        d = dash.leases_expiring(month=m["expiry_month"])
        assert d["total"] == int(m["n"]), f"{m['expiry_month']}: {d['total']} != {m['n']}"
        assert len(d["rows"]) == d["total"]


def test_rows_carry_every_field_the_lease_dialog_reads(dash):
    snap, _ = dash._snapshot_id(None)
    month = dash._q(
        """SELECT expiry_month::text AS m FROM mart.expiration_schedule
           WHERE snapshot_id = %s GROUP BY 1 ORDER BY sum(expiring_leases) DESC LIMIT 1""",
        (snap,),
    )[0]["m"]
    rows = dash.leases_expiring(month=month)["rows"]
    assert rows
    needed = {"lease_id", "property_code", "unit_code", "occupancy_status", "display_name",
              "resident_id", "market_rent", "balance", "charges_total", "lease_expiration",
              "holdover"}
    assert needed <= set(rows[0])


def test_only_occupied_and_notice_are_returned(dash):
    snap, _ = dash._snapshot_id(None)
    month = dash._q(
        """SELECT expiry_month::text AS m FROM mart.expiration_schedule
           WHERE snapshot_id = %s GROUP BY 1 ORDER BY sum(expiring_leases) DESC LIMIT 1""",
        (snap,),
    )[0]["m"]
    statuses = {r["occupancy_status"] for r in dash.leases_expiring(month=month)["rows"]}
    assert statuses <= {"occupied", "notice"}


def test_holdover_flag_matches_the_snapshot_date(dash):
    snap, as_of = dash._snapshot_id(None)
    months = dash._q(
        """SELECT expiry_month::text AS m FROM mart.expiration_schedule
           WHERE snapshot_id = %s GROUP BY 1 ORDER BY 1""",
        (snap,),
    )
    for m in months:
        for r in dash.leases_expiring(month=m["m"])["rows"]:
            assert r["holdover"] == (r["lease_expiration"] < as_of)


def test_a_month_with_no_expirations_is_an_empty_list_not_an_error(dash):
    d = dash.leases_expiring(month="1900-01-01")
    assert d == {"month": "1900-01-01", "as_of": d["as_of"], "total": 0, "rows": []}


def test_an_explicit_as_of_selects_the_same_snapshot_the_chart_drew(dash):
    """The frontend sends SUMMARY.as_of with every drill-down, so the bar and its
    list stay on one snapshot once a snapshot picker exists. Today that argument
    is the only thing keeping the pair honest, and nothing else exercises it."""
    snap, as_of = dash._snapshot_id(None)
    month = dash._q(
        """SELECT expiry_month::text AS m FROM mart.expiration_schedule
           WHERE snapshot_id = %s GROUP BY 1 ORDER BY sum(expiring_leases) DESC LIMIT 1""",
        (snap,),
    )[0]["m"]
    default = dash.leases_expiring(month=month)
    explicit = dash.leases_expiring(month=month, as_of=as_of)
    assert explicit["as_of"] == as_of == default["as_of"]
    assert explicit["total"] == default["total"]
    assert [r["lease_id"] for r in explicit["rows"]] == [r["lease_id"] for r in default["rows"]]


@pytest.mark.parametrize("bad", ["2026-07", "2026-7-1", "nope", "2026-07-15"])
def test_a_malformed_month_is_a_400(dash, bad):
    with pytest.raises(HTTPException) as exc:
        dash.leases_expiring(month=bad)
    assert exc.value.status_code == 400
