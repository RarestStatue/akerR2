"""B1-B3: malformed input at the dashboard API becomes a 4xx, not a 500.

    pytest -m integration

Needs a database with the corpus loaded (README step 5).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    # Imported inside the fixture, not at module scope: importing the dashboard
    # opens a connection pool, and an integration-marked module still gets
    # *collected* during a unit-test run.
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


ENDPOINTS_WITH_AS_OF = [
    "/api/summary",
    "/api/matrix",
    "/api/economics",
    "/api/anomalies",
    "/api/units",
    "/api/insights",
    "/api/quality",
    "/api/property/115r",
    "/api/export/properties.csv",
    "/api/export/revenue_bridge.xlsx",
]


@pytest.mark.parametrize("path", ENDPOINTS_WITH_AS_OF)
def test_a_malformed_as_of_is_a_400_on_every_endpoint(client, path):
    r = client.get(path, params={"as_of": "not-a-date"})
    assert r.status_code == 400


@pytest.mark.parametrize("path", ENDPOINTS_WITH_AS_OF)
def test_an_iso_week_date_does_not_500(client, path):
    # dt.date.fromisoformat (Python 3.11+) accepts ISO week dates that ::date
    # rejects outright. Passing the *parsed* value as the query parameter (not
    # the raw string) means PostgreSQL never re-parses it, so this becomes a
    # well-formed-but-no-such-snapshot 404, not a 500.
    r = client.get(path, params={"as_of": "2026-W01-1"})
    assert r.status_code == 404


def test_a_malformed_as_of_is_a_400_on_leases_expiring(client):
    r = client.get("/api/leases/expiring", params={"month": "2026-07-01", "as_of": "not-a-date"})
    assert r.status_code == 400


def test_a_wellformed_as_of_with_no_snapshot_is_still_a_404(client):
    r = client.get("/api/summary", params={"as_of": "1999-01-01"})
    assert r.status_code == 404


def test_a_negative_offset_is_a_422_not_a_500(client):
    r = client.get("/api/units", params={"offset": -1, "limit": 5})
    assert r.status_code == 422


def test_limit_is_capped(client):
    r = client.get("/api/units", params={"limit": 999999999})
    assert r.status_code == 422
    r = client.get("/api/units", params={"limit": 1000})
    assert r.status_code == 200


def test_a_percent_in_the_search_term_is_a_literal(client):
    baseline = client.get("/api/units").json()["total"]
    filtered = client.get("/api/units", params={"q": "%"}).json()["total"]
    assert filtered < baseline
    # a term whose only metacharacter is the wildcard must still find real rows,
    # or a broken ESCAPE clause (matching nothing) would satisfy the assertion above too
    plain = client.get("/api/units", params={"q": "1"}).json()["total"]
    hit = client.get("/api/units", params={"q": "1_1"}).json()["total"]
    assert plain > 0 and hit < plain
