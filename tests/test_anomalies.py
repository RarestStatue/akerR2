"""Computed findings: F3 deterministic z-score anomalies. PLAN6 phase 2.

    pytest -m integration

Needs a database with the corpus loaded (README step 5).
"""

from __future__ import annotations

import statistics

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dash():
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


def test_every_row_clears_the_threshold(dash, snap):
    rows = dash._q(
        "SELECT z, peer_books FROM mart.property_anomaly WHERE snapshot_id = %s", (snap,)
    )
    assert rows
    for r in rows:
        assert abs(float(r["z"])) >= 1.75, r
        assert r["peer_books"] >= 3, r


def test_z_scores_reconcile_with_a_python_recomputation(dash, snap):
    base = dash._q(
        """SELECT pp.property_id, pp.units, pp.pct_occupied,
                  CASE WHEN pp.plottable THEN pp.revenue_capture_pct END AS revenue_capture_pct,
                  CASE WHEN pp.plottable
                       THEN round(pp.concessions / nullif(pp.units,0), 2) END AS concession_per_unit,
                  CASE WHEN pp.plottable
                       THEN round(pp.loss_to_lease / nullif(pp.units,0), 2) END AS loss_to_lease_per_unit,
                  round(pp.balance / nullif(pp.units,0), 2) AS balance_per_unit,
                  round(100.0 * pp.notice_units / nullif(pp.units,0), 2) AS notice_rate,
                  round(100.0 * pp.vacant_units / nullif(pp.units,0), 2) AS vacancy_rate
           FROM mart.property_profitability pp
           WHERE pp.snapshot_id = %s AND pp.units > 0""",
        (snap,),
    )
    metrics = ["pct_occupied", "revenue_capture_pct", "balance_per_unit", "concession_per_unit",
               "loss_to_lease_per_unit", "notice_rate", "vacancy_rate"]
    by_metric: dict[str, list[float]] = {m: [] for m in metrics}
    for row in base:
        for m in metrics:
            if row[m] is not None:
                by_metric[m].append(float(row[m]))

    rows = dash._q(
        "SELECT property_id, metric, value, z FROM mart.property_anomaly WHERE snapshot_id = %s",
        (snap,),
    )
    assert rows
    for r in rows:
        values = by_metric[r["metric"]]
        mean = statistics.mean(values)
        sd = statistics.stdev(values)
        expected_z = round((float(r["value"]) - mean) / sd, 2)
        assert expected_z == pytest.approx(float(r["z"]), abs=0.01), r


def test_peer_counts_are_22_and_12(dash, snap):
    rows = dash._q(
        "SELECT DISTINCT metric, peer_books FROM mart.property_anomaly WHERE snapshot_id = %s",
        (snap,),
    )
    universal = {"pct_occupied", "balance_per_unit", "notice_rate", "vacancy_rate"}
    charge_derived = {"revenue_capture_pct", "concession_per_unit", "loss_to_lease_per_unit"}
    for r in rows:
        if r["metric"] in universal:
            assert r["peer_books"] == 22, r
        elif r["metric"] in charge_derived:
            assert r["peer_books"] == 12, r


def test_adverse_direction_matches_the_metric(dash, snap):
    rows = dash._q(
        "SELECT worse_when, z, adverse FROM mart.property_anomaly WHERE snapshot_id = %s", (snap,)
    )
    assert rows
    for r in rows:
        if r["worse_when"] == "high":
            assert r["adverse"] == (float(r["z"]) > 0), r
        else:
            assert r["adverse"] == (float(r["z"]) < 0), r


def test_charge_derived_metrics_only_cover_plottable_books(dash, snap):
    n = dash._q(
        """SELECT count(*) AS n FROM mart.property_anomaly a
           JOIN mart.property_profitability pp
             ON pp.snapshot_id = a.snapshot_id AND pp.property_id = a.property_id
           WHERE a.snapshot_id = %s
             AND a.metric IN ('revenue_capture_pct','concession_per_unit','loss_to_lease_per_unit')
             AND pp.exclusion_reason IS NOT NULL""",
        (snap,),
    )[0]["n"]
    assert n == 0


def test_no_metric_is_an_unnormalised_total(dash, snap):
    rows = dash._q("SELECT DISTINCT metric FROM mart.property_anomaly WHERE snapshot_id = %s", (snap,))
    metrics = {r["metric"] for r in rows}
    assert metrics <= {
        "pct_occupied", "revenue_capture_pct", "balance_per_unit", "concession_per_unit",
        "loss_to_lease_per_unit", "notice_rate", "vacancy_rate",
    }


@pytest.fixture
def restore_insights(snap):
    """_persist(force=True) is a wholesale DELETE -- back up and restore the
    snapshot's real stored insights so this test does not erase what
    `insights import` put there for every other test (and for F9) to read."""
    from aker_etl.config import get_settings
    from aker_etl.db import connect

    settings = get_settings()
    cols = ("scope", "property_id", "asset_key", "category", "priority", "headline",
            "detail", "evidence", "model", "prompt_sha256", "generated_at")
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {','.join(cols)} FROM core.insight WHERE snapshot_id = %s", (snap,)
        )
        backup = cur.fetchall()
    yield
    from psycopg.types.json import Jsonb

    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM core.insight WHERE snapshot_id = %s", (snap,))
        placeholders = ",".join(["%s"] * len(cols))
        evidence_ix = cols.index("evidence")
        for row in backup:
            row = list(row)
            row[evidence_ix] = Jsonb(row[evidence_ix])
            cur.execute(
                f"INSERT INTO core.insight (snapshot_id, {','.join(cols)}) "
                f"VALUES (%s, {placeholders})",
                (snap, *row),
            )


def test_insights_regeneration_does_not_affect_anomalies(dash, snap, restore_insights):
    from aker_etl.config import get_settings
    from aker_etl.db import connect
    from aker_etl.insights.generate import _persist

    before = len(dash._q("SELECT 1 AS x FROM mart.property_anomaly WHERE snapshot_id = %s", (snap,)))
    settings = get_settings()
    with connect(settings, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO core.insight_run (snapshot_id, model, prompt_sha256, status) "
            "VALUES (%s, 'test', %s, 'succeeded') RETURNING insight_run_id",
            (snap, "0" * 64),
        )
        run_id = cur.fetchone()[0]
        # This is the entire reason for the separate lane: a wholesale delete of
        # core.insight (what --force does) must leave mart.property_anomaly alone.
        _persist(conn, snap, run_id, "test", [], force=True)

    after = len(dash._q("SELECT 1 AS x FROM mart.property_anomaly WHERE snapshot_id = %s", (snap,)))
    assert after == before


def test_the_anomaly_view_survives_refresh_marts(dash, snap):
    from aker_etl.config import get_settings
    from aker_etl.db import connect, refresh_marts

    before = dash._q(
        "SELECT property_code, metric, z FROM mart.property_anomaly "
        "WHERE snapshot_id = %s ORDER BY property_code, metric", (snap,)
    )
    settings = get_settings()
    with connect(settings, autocommit=True) as conn:
        refresh_marts(conn)
    after = dash._q(
        "SELECT property_code, metric, z FROM mart.property_anomaly "
        "WHERE snapshot_id = %s ORDER BY property_code, metric", (snap,)
    )
    assert before == after
