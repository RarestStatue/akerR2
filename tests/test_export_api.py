"""/api/export/{dataset}.{fmt}: F7. PLAN6 phase 4.

    pytest -m integration

Needs a database with the corpus loaded (README step 5).
"""

from __future__ import annotations

import io

import openpyxl
import pytest

from aker_etl.dashboard.export import DATASETS

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from aker_etl.config import get_settings
    from aker_etl.dashboard.app import app
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

    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("dataset", sorted(DATASETS))
@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_every_registered_dataset_exports_in_both_formats(client, dataset, fmt):
    r = client.get(f"/api/export/{dataset}.{fmt}")
    assert r.status_code == 200, r.text
    assert len(r.content) > 0
    if fmt == "csv":
        assert r.headers["content-type"].startswith("text/csv")
        assert r.content.startswith(b"\xef\xbb\xbf")
    else:
        assert r.headers["content-type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        assert wb.active.max_row >= 1
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    stem = DATASETS[dataset][0]
    assert f"aker-{stem}-" in cd
    assert cd.endswith(f".{fmt}\"")


def test_an_unknown_dataset_is_a_404(client):
    r = client.get("/api/export/bogus.csv")
    assert r.status_code == 404
    for name in DATASETS:
        assert name in r.json()["detail"]


def test_an_unknown_format_is_a_404(client):
    r = client.get("/api/export/units.pdf")
    assert r.status_code == 404


def test_units_export_is_not_paginated(client):
    r = client.get("/api/export/units.csv")
    assert r.status_code == 200
    # header row + one row per lease block, current and future both
    n_rows = len(r.text.strip().splitlines()) - 1
    assert n_rows == 4106
