"""End-to-end load against a live database.

    docker compose -f docker/docker-compose.yml --env-file .env up -d db
    pytest -m integration

Skipped automatically when the database is unreachable, so the default
`pytest` run stays hermetic.
"""

from __future__ import annotations

import pytest

from aker_etl.config import get_settings
from aker_etl.db import connect, init_db
from aker_etl.loader import load

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    try:
        with connect(s, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unreachable: {exc}")
    init_db(s)
    return s


@pytest.fixture(scope="module")
def loaded(settings):
    """One full load, from scratch, shared by the assertions below."""
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """TRUNCATE core.lease_charge, core.lease, core.unit, core.unit_type,
                        core.resident, core.rent_roll_summary_group, core.charge_summary,
                        core.unit_availability, core.insight, core.insight_run,
                        core.snapshot, core.property, raw.load_issue, raw.source_file,
                        raw.ingest_run RESTART IDENTITY CASCADE"""
        )
    return load(settings, force=True)


def _scalar(settings, sql: str):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_load_hits_every_golden_number(settings, loaded):
    assert loaded.files_loaded == 50
    assert loaded.files_failed == 0
    assert loaded.errors == 0
    assert _scalar(settings, "SELECT count(*) FROM core.property") == 25
    assert _scalar(settings, "SELECT count(*) FROM core.lease") == 4106
    assert _scalar(settings, "SELECT count(*) FROM core.lease WHERE section='current'") == 4013
    assert _scalar(settings, "SELECT count(*) FROM core.lease WHERE section='future'") == 93
    assert _scalar(settings, "SELECT count(*) FROM core.unit") == 4013
    assert _scalar(settings, "SELECT count(*) FROM core.unit_type") == 448
    assert _scalar(settings, "SELECT count(*) FROM core.resident") == 3917
    assert _scalar(settings, "SELECT count(*) FROM core.lease_charge") == 9177
    assert _scalar(settings, "SELECT count(*) FROM core.rent_roll_summary_group") == 150
    assert _scalar(settings, "SELECT count(*) FROM core.charge_summary") == 117
    assert _scalar(settings, "SELECT count(*) FROM core.unit_availability") == 25
    assert _scalar(settings, "SELECT count(*) FROM core.snapshot") == 1


def test_reload_of_unchanged_files_is_a_no_op(settings, loaded):
    again = load(settings)
    assert again.files_skipped == 50
    assert again.files_loaded == 0
    assert _scalar(settings, "SELECT count(*) FROM core.lease") == 4106


def test_force_reload_does_not_duplicate(settings, loaded):
    forced = load(settings, force=True)
    assert forced.files_loaded == 50
    assert forced.errors == 0
    assert _scalar(settings, "SELECT count(*) FROM core.lease") == 4106
    assert _scalar(settings, "SELECT count(*) FROM core.lease_charge") == 9177
    assert _scalar(settings, "SELECT count(*) FROM core.unit") == 4013


def test_charge_totals_reconcile_in_the_database(settings, loaded):
    mismatched = _scalar(settings, """
        SELECT count(*) FROM (
          SELECT l.lease_id, l.charges_total, COALESCE(sum(lc.amount), 0) AS s
          FROM core.lease l
          LEFT JOIN core.lease_charge lc ON lc.lease_id = l.lease_id
          GROUP BY l.lease_id, l.charges_total
          HAVING abs(l.charges_total - COALESCE(sum(lc.amount), 0)) > 0.005
        ) t""")
    assert mismatched == 0


def test_only_153c_disagrees_on_unit_count(settings, loaded):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT property_code::text, unit_delta FROM mart.reconciliation "
                    "WHERE unit_delta <> 0 ORDER BY 1")
        rows = cur.fetchall()
    assert rows == [("153c", 7)]


def test_report_charge_totals_match_the_detail(settings, loaded):
    """No summary_group_charges_vs_detail or charge_summary_vs_detail issue was raised."""
    assert _scalar(settings, """
        SELECT count(*) FROM raw.load_issue
        WHERE severity = 'error'
          AND run_id = (SELECT max(run_id) FROM raw.ingest_run)""") == 0


def test_zero_unit_books_load_as_properties(settings, loaded):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT p.property_code::text FROM core.property p
            WHERE NOT EXISTS (SELECT 1 FROM core.lease l WHERE l.property_id = p.property_id)
            ORDER BY 1""")
        rows = [r[0] for r in cur.fetchall()]
    assert rows == ["134land", "183c", "altapm"]


def test_mart_views_are_populated(settings, loaded):
    assert _scalar(settings, "SELECT count(*) FROM mart.property_snapshot_kpi") == 22
    assert _scalar(settings, "SELECT count(*) FROM mart.charge_mix") > 0
    assert _scalar(settings, "SELECT count(*) FROM mart.expiration_schedule") > 0
    assert _scalar(settings, "SELECT count(*) FROM mart.loss_to_lease") == 3824
    # Single snapshot today, so period-over-period is legitimately empty.
    assert _scalar(settings, "SELECT count(*) FROM mart.property_trend") == 0


def test_asset_rollup_groups_the_books_of_one_property(settings, loaded):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT property_codes FROM mart.asset_snapshot_kpi WHERE asset_key = '134'")
        codes = cur.fetchone()[0]
    assert sorted(codes) == ["134c", "134r"]  # 134land has no leases, so no KPI row
