"""Charge-code classification review: F4. PLAN6 phase 3.

    pytest -m integration

Needs a database with the corpus loaded (README step 5). Every test restores
the original classification of the code it touches in a fixture teardown --
other tests' golden numbers (and other integration files' assertions about
mart.charge_mix) depend on the sql/060 seed being intact.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aker_etl.cli import app
from aker_etl.config import get_settings
from aker_etl.db import connect, refresh_marts

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    try:
        with connect(s, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM core.lease")
            empty = cur.fetchone()[0] == 0
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unreachable: {exc}")
    if empty:
        from aker_etl.loader import load

        load(s, force=True)
    return s


def _row(settings, code: str):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT category::text, description, label_verified FROM core.charge_code "
            "WHERE charge_code = %s",
            (code,),
        )
        return cur.fetchone()


def _set_row(settings, code: str, row) -> None:
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE core.charge_code SET category = %s, description = %s, label_verified = %s "
            "WHERE charge_code = %s",
            (*row, code),
        )
    with connect(settings, autocommit=True) as conn:
        refresh_marts(conn)


def _audit_count(settings, code: str) -> int:
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.charge_code_audit WHERE charge_code = %s", (code,))
        return cur.fetchone()[0]


@pytest.fixture
def restore_wd(settings):
    """W/D is the code named in PLAN6's own examples. Snapshot + restore its row."""
    original = _row(settings, "W/D")
    yield original
    _set_row(settings, "W/D", original)


def test_setting_a_category_writes_an_audit_row(settings, restore_wd):
    before = _audit_count(settings, "W/D")
    result = runner.invoke(app, [
        "charge-code", "set", "W/D",
        "--category", "fee",
        "--description", "Washer/dryer rental, confirmed",
        "--verified", "--note", "confirmed with asset team", "--by", "pytest",
    ])
    assert result.exit_code == 0, result.output
    assert "service -> fee" in result.output
    assert _audit_count(settings, "W/D") == before + 1

    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT old_category::text, new_category::text, old_description, new_description,
                      old_verified, new_verified, note, changed_by
               FROM core.charge_code_audit WHERE charge_code = 'W/D' ORDER BY audit_id DESC LIMIT 1"""
        )
        old_cat, new_cat, old_desc, new_desc, old_ver, new_ver, note, by = cur.fetchone()
    assert (old_cat, new_cat) == ("service", "fee")
    assert old_desc == "Washer/dryer rental (inferred)"
    assert new_desc == "Washer/dryer rental, confirmed"
    assert (old_ver, new_ver) == (False, True)
    assert note == "confirmed with asset team"
    assert by == "pytest"


def test_a_no_op_set_writes_nothing(settings, restore_wd):
    before = _audit_count(settings, "W/D")
    category, description, verified = restore_wd
    result = runner.invoke(app, [
        "charge-code", "set", "W/D",
        "--category", category, "--description", description,
        "--verified" if verified else "--unverified",
    ])
    assert result.exit_code == 0, result.output
    assert "no change" in result.output
    assert _audit_count(settings, "W/D") == before


def test_an_unknown_code_exits_structural_and_suggests_neighbours(settings):
    result = runner.invoke(app, ["charge-code", "set", "ZZZNOPE", "--category", "fee"])
    assert result.exit_code == 3
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT charge_code FROM core.charge_code")
        real_codes = {r[0] for r in cur.fetchall()}
    assert any(code in result.output for code in real_codes)


def test_an_invalid_category_lists_the_valid_ones(settings):
    result = runner.invoke(app, ["charge-code", "set", "W/D", "--category", "bogus"])
    assert result.exit_code == 3
    for category in (
        "rent", "subsidy", "concession", "parking", "garage", "storage", "pet",
        "utility", "amenity", "service", "tax", "cam", "insurance", "fee", "other",
    ):
        assert category in result.output


def test_the_audit_constraint_rejects_a_no_change_row(settings):
    from psycopg.errors import CheckViolation

    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(CheckViolation):
            cur.execute(
                """INSERT INTO core.charge_code_audit
                     (charge_code, old_category, new_category, old_description, new_description,
                      old_verified, new_verified, changed_by)
                   VALUES ('RENT','rent','rent','Base rent','Base rent', true, true, 'pytest')"""
            )


def test_charge_mix_reflects_the_new_category_after_the_command(settings, restore_wd):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.lease_charge WHERE charge_code = 'W/D'")
        has_usage = cur.fetchone()[0] > 0
    if not has_usage:
        pytest.skip("W/D has no usage in the current corpus")

    result = runner.invoke(app, ["charge-code", "set", "W/D", "--category", "fee"])
    assert result.exit_code == 0, result.output

    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT category::text FROM mart.charge_mix WHERE charge_code = 'W/D'")
        categories = {r[0] for r in cur.fetchall()}
    assert categories == {"fee"}


def test_reset_truncates_the_audit_table(settings, restore_wd):
    # Leaves the whole database empty -- reset TRUNCATEs core.* and raw.*. Every
    # later integration file already tolerates this: each one's own fixture
    # checks "if empty: load()" (see test_dashboard_expiring.py's `dash`
    # fixture), which is the same fallback test_load_integration.py relies on.
    runner.invoke(app, ["charge-code", "set", "W/D", "--category", "fee"])
    result = runner.invoke(app, ["reset", "--yes"])
    assert result.exit_code == 0, result.output
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.charge_code_audit")
        assert cur.fetchone()[0] == 0
