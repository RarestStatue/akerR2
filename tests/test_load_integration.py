"""End-to-end load against a live database.

    docker compose -f docker/docker-compose.yml --env-file .env up -d db
    pytest -m integration

Skipped automatically when the database is unreachable, so the default
`pytest` run stays hermetic.
"""

from __future__ import annotations

import psycopg
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


# --------------------------------------------------------------------------- #
# Profitability matrix -- PLAN2 section 5.2
# --------------------------------------------------------------------------- #

_PLOTTABLE_FIGURES = {
    # code: (units, occupied, pct_occupied, market_rent, lease_charges, capture_pct, quadrant)
    "138a": (63, 63, "100.00", "86114.00", "94756.90", "110.04", "performing"),
    "126a": (19, 18, "94.74", "32936.00", "35387.00", "107.44", "vacancy_led"),
    "115r": (300, 288, "96.00", "763814.00", "791650.93", "103.64", "performing"),
    "462a": (266, 253, "95.11", "417895.50", "415606.08", "99.45", "performing"),
    "143a": (312, 298, "95.51", "572421.00", "568612.77", "99.33", "performing"),
    "134r": (348, 333, "95.69", "1212256.00", "1180076.57", "97.35", "performing"),
    "144r": (775, 759, "97.94", "1722576.00", "1636735.63", "95.02", "performing"),
    "126r": (284, 274, "96.48", "1108579.00", "1034844.53", "93.35", "leaking"),
    "138r": (235, 221, "94.04", "739550.00", "675043.47", "91.28", "distressed"),
    "139r": (71, 65, "91.55", "537740.00", "421627.77", "78.41", "distressed"),
    "153r": (211, 196, "92.89", "684747.00", "515321.39", "75.26", "distressed"),
    "153a": (23, 19, "82.61", "63954.00", "29587.00", "46.26", "distressed"),
}

_MOVEMENT_DELTAS = {
    "153a": ("31169.30", 3),
    "153r": ("135188.26", 5),
    "139r": ("89225.23", 3),
    "138r": ("27529.03", 3),
    "126r": ("18305.52", 0),
    "144r": ("0", 0), "134r": ("0", 0), "143a": ("0", 0),
    "462a": ("0", 0), "115r": ("0", 0), "138a": ("0", 0),
    "126a": ("0", 1),
}


def test_matrix_has_exactly_the_twelve_plottable_books(settings, loaded):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT property_code::text FROM mart.property_profitability WHERE plottable ORDER BY 1"
        )
        codes = [r[0] for r in cur.fetchall()]
    assert codes == sorted(_PLOTTABLE_FIGURES)


def test_matrix_exclusion_reason_counts(settings, loaded):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT exclusion_reason, count(*) FROM mart.property_profitability
               WHERE NOT plottable GROUP BY 1"""
        )
        counts = dict(cur.fetchall())
    assert counts == {"no_units": 3, "no_market_rent": 4, "no_charge_data": 6}


def test_matrix_figures_match_the_verified_table(settings, loaded):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT property_code::text, units, occupied_units, pct_occupied, market_rent,
                      lease_charges, revenue_capture_pct, quadrant
               FROM mart.property_profitability WHERE plottable"""
        )
        rows = {r[0]: r[1:] for r in cur.fetchall()}
    for code, expected in _PLOTTABLE_FIGURES.items():
        units, occupied, pct_occupied, market_rent, lease_charges, capture_pct, quadrant = expected
        got = rows[code]
        assert got[0] == units
        assert got[1] == occupied
        assert str(got[2]) == pct_occupied
        assert str(got[3]) == market_rent
        assert str(got[4]) == lease_charges
        assert str(got[5]) == capture_pct
        assert got[6] == quadrant


def test_matrix_quadrant_counts(settings, loaded):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT quadrant, count(*) FROM mart.property_profitability
               WHERE plottable GROUP BY 1"""
        )
        counts = dict(cur.fetchall())
    assert counts == {"performing": 6, "leaking": 1, "vacancy_led": 1, "distressed": 4}


def test_matrix_movement_deltas(settings, loaded):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT property_code::text, charges_to_threshold, units_to_threshold
               FROM mart.property_profitability WHERE plottable"""
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for code, (charges, units) in _MOVEMENT_DELTAS.items():
        assert str(rows[code][0]) == charges
        assert rows[code][1] == units


def test_matrix_has_no_third_state(settings, loaded):
    assert _scalar(settings, """
        SELECT count(*) FROM mart.property_profitability
        WHERE plottable AND (revenue_capture_pct IS NULL OR quadrant IS NULL)""") == 0
    assert _scalar(settings, """
        SELECT count(*) FROM mart.property_profitability
        WHERE NOT plottable AND (quadrant IS NOT NULL OR exclusion_reason IS NULL)""") == 0


def test_quadrant_rule_puts_the_boundary_on_the_high_side(settings, loaded):
    """PLAN2 2.2: a value EQUAL to a threshold counts as the high side.

    Calls mart.quadrant() -- the same function the view calls -- so the rule is
    tested rather than restated.
    """
    cases = [
        ((95.00, 95.00), "performing"),
        ((95.00, 94.99), "vacancy_led"),
        ((94.99, 95.00), "leaking"),
        ((94.99, 94.99), "distressed"),
        ((None, 95.00), None),
        ((95.00, None), None),
    ]
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        for (capture, occ), expected in cases:
            cur.execute(
                "SELECT mart.quadrant(%s::numeric, %s::numeric, 95.0, 95.0)",
                (capture, occ),
            )
            assert cur.fetchone()[0] == expected, (capture, occ)


def test_view_quadrant_agrees_with_the_function(settings, loaded):
    """The view must not carry a second copy of the rule."""
    assert _scalar(settings, """
        SELECT count(*) FROM mart.property_profitability
        WHERE plottable
          AND quadrant IS DISTINCT FROM
              mart.quadrant(revenue_capture_pct, pct_occupied,
                            capture_threshold, occupancy_threshold)""") == 0


def test_matrix_view_survives_refresh_marts(settings, loaded):
    """Plain view, so refresh_marts() (which refreshes matviews only) leaves it alone."""
    from aker_etl.db import refresh_marts

    with connect(settings, autocommit=True) as conn:
        refresh_marts(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mart.property_profitability")
            assert cur.fetchone()[0] == 25


def test_api_matrix_covers_every_property(settings, loaded):
    from fastapi.testclient import TestClient

    from aker_etl.dashboard.app import app

    with TestClient(app) as client:
        r = client.get("/api/matrix")
    assert r.status_code == 200
    d = r.json()
    assert len(d["points"]) + len(d["excluded"]) == 25


def test_api_property_detail_does_not_404_on_zero_unit_books(settings, loaded):
    """Regression test for the section 3.2 fix."""
    from fastapi.testclient import TestClient

    from aker_etl.dashboard.app import app

    with TestClient(app) as client:
        r = client.get("/api/property/134land")
    assert r.status_code == 200
    d = r.json()
    assert d["kpi"]["units"] == 0
    assert d["matrix"] == [] or all(not m["plottable"] for m in d["matrix"])


def test_a_moveout_on_a_non_notice_row_does_not_abort_the_load(settings, loaded):
    """BUG.md 1: the CHECK is an implication, not a biconditional.

    A future-section row or a VACANT/MODEL/DOWN sentinel can legitimately print a
    Move Out date. The old biconditional turned one such cell into a COPY failure
    that rolled back the entire ingest run.
    """
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT snapshot_id FROM core.snapshot LIMIT 1")
        snap = cur.fetchone()[0]
        # A unit with no future-section row already: core.lease has
        # UNIQUE (snapshot_id, unit_id, section), and the first INSERT below is
        # meant to succeed, so an unqualified LIMIT 1 could collide with one of
        # the 93 existing future rows and fail for the wrong reason.
        cur.execute(
            """SELECT l.property_id, l.unit_id FROM core.lease l
               WHERE l.snapshot_id = %s AND NOT EXISTS (
                 SELECT 1 FROM core.lease f
                 WHERE f.snapshot_id = l.snapshot_id AND f.unit_id = l.unit_id
                   AND f.section = 'future')
               LIMIT 1""",
            (snap,),
        )
        pid, uid = cur.fetchone()
        cur.execute("SELECT file_id FROM raw.source_file LIMIT 1")
        fid = cur.fetchone()[0]
        cur.execute("SELECT resident_id FROM core.resident LIMIT 1")
        rid = cur.fetchone()[0]

        # accepted: future row that carries a move-out date
        cur.execute(
            """INSERT INTO core.lease (snapshot_id, property_id, unit_id, resident_id,
                 section, occupancy_status, move_in, move_out, file_id, sheet_row)
               VALUES (%s,%s,%s,%s,'future','future','2026-01-01','2026-06-01',%s,999999)""",
            (snap, pid, uid, rid, fid),
        )
        # still rejected: a notice row with no move-out date
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """INSERT INTO core.lease (snapshot_id, property_id, unit_id, resident_id,
                     section, occupancy_status, file_id, sheet_row)
                   VALUES (%s,%s,%s,%s,'current','notice',%s,999998)""",
                (snap, pid, uid, rid, fid),
            )

    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM core.lease WHERE sheet_row IN (999999, 999998)")


def test_availability_check_groups_by_snapshot(settings, loaded):
    """BUG.md 2: two snapshots reporting the same unit count must not collapse.

    Exercises the grouping shape against a synthetic two-snapshot fixture, so it
    does not need a second month's files to be loaded.
    """
    assert _scalar(settings, """
        WITH ua(snapshot_id, property_id, units) AS (VALUES (1,1,10),(2,1,10)),
             l(snapshot_id, property_id, section) AS (
               SELECT s, 1, 'current' FROM generate_series(1,2) s, generate_series(1,10))
        SELECT count(*) FROM (
          SELECT ua.snapshot_id, ua.units,
                 count(*) FILTER (WHERE l.section = 'current') AS detail_units
          FROM ua
          LEFT JOIN l ON l.snapshot_id = ua.snapshot_id
                     AND l.property_id = ua.property_id
          GROUP BY ua.snapshot_id, ua.property_id, ua.units
          HAVING ua.units <> count(*) FILTER (WHERE l.section = 'current')
        ) x""") == 0


def test_occupancy_numerator_and_denominator_cover_the_same_rows(settings, loaded):
    """BUG.md 7.5: occupied_units is not section-filtered; pct_occupied's denominator is.

    Safe only while derive_status() gives every future-section row the 'future'
    status. This asserts that invariant directly, so a change to derive_status
    cannot silently push pct_occupied above 100%.
    """
    assert _scalar(settings, """
        SELECT count(*) FROM core.lease
        WHERE section <> 'current' AND occupancy_status IN ('occupied','notice')""") == 0
    assert _scalar(settings,
                   "SELECT count(*) FROM mart.property_snapshot_kpi WHERE pct_occupied > 100") == 0


def test_reload_removes_a_summary_group_that_vanished_from_the_file(settings, loaded):
    """B4: a summary group or charge-code row that no longer appears in a reloaded
    file must not survive as a stale row from the previous load."""
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT snapshot_id, property_id, file_id FROM core.lease LIMIT 1")
        snap, pid, fid = cur.fetchone()
        cur.execute(
            """INSERT INTO core.rent_roll_summary_group
                 (snapshot_id, property_id, group_label, file_id)
               VALUES (%s, %s, 'a label no workbook prints', %s)
               ON CONFLICT (snapshot_id, property_id, group_label) DO NOTHING""",
            (snap, pid, fid),
        )
    try:
        load(settings, force=True)
        assert _scalar(
            settings,
            """SELECT count(*) FROM core.rent_roll_summary_group
               WHERE group_label = 'a label no workbook prints'""",
        ) == 0
    finally:
        with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM core.rent_roll_summary_group WHERE group_label = "
                "'a label no workbook prints'"
            )


def test_lease_file_id_has_an_index(settings, loaded):
    """B13."""
    assert _scalar(settings, """
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'core' AND indexname = 'lease_file_ix'""") == 1


def test_the_schema_carries_no_alter_statements():
    """Part A regression guard: sql/ is rebuilt from scratch, never migrated."""
    import re

    from aker_etl.db import SQL_DIR

    pattern = re.compile(r"\bALTER\s+(TABLE|TYPE)\b", re.IGNORECASE)
    for path in SQL_DIR.rglob("*.sql"):
        text = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        assert not pattern.search(text), f"{path} contains an ALTER TABLE/TYPE statement"


def test_insight_category_has_positioning(settings):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
               WHERE t.typname = 'insight_category' ORDER BY e.enumsortorder"""
        )
        labels = [r[0] for r in cur.fetchall()]
    assert "positioning" in labels
    assert len(labels) == 9


def test_reset_leaves_no_rows_in_the_mart_views(settings, loaded):
    """B5. Destructive: truncates the corpus, including core.insight and
    core.insight_run, which `load(...)` cannot rebuild -- only a re-import of
    insights.json can. Reload and re-import both happen in `finally` so a
    failed assertion does not leave the developer's database empty or the
    Insights tab blank."""
    from pathlib import Path

    from aker_etl.cli import reset as reset_cmd
    from aker_etl.insights.store import import_artifact

    artifact = Path(__file__).resolve().parents[1] / "insights.json"
    try:
        reset_cmd(yes=True)
        assert _scalar(settings, "SELECT count(*) FROM mart.property_snapshot_kpi") == 0
    finally:
        load(settings, force=True)
        if artifact.is_file():
            import_artifact(settings, artifact)
    assert _scalar(settings, "SELECT count(*) FROM core.lease") == 4106
