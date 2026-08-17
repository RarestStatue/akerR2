"""/api/insight/{id}/provenance: F9. PLAN6 phase 4.

    pytest -m integration

Needs a database with the corpus loaded and insights stored (README step 5,
then `aker-etl insights import insights.json`).
"""

from __future__ import annotations

import pytest

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


@pytest.fixture(scope="module")
def settings():
    from aker_etl.config import get_settings

    return get_settings()


@pytest.fixture(scope="module")
def stored_insight_ids(client):
    r = client.get("/api/insights")
    assert r.status_code == 200
    ids = [i["insight_id"] for i in r.json()["insights"] if i.get("insight_id") is not None]
    if not ids:
        pytest.skip("no insights stored -- run `aker-etl insights import insights.json` first")
    return ids


def test_provenance_finds_every_cited_figure_of_a_stored_insight(client, stored_insight_ids):
    # This is the evidence gate's own guarantee, re-proved from the other
    # direction: every value that survived check_evidence() at generation time
    # must still be findable in today's payload (nothing else has changed it).
    for insight_id in stored_insight_ids:
        r = client.get(f"/api/insight/{insight_id}/provenance")
        assert r.status_code == 200, r.text
        d = r.json()
        assert not d["stale"], d
        for ev in d["evidence"]:
            assert ev["found"] is True, (insight_id, ev)
            assert ev["paths"], (insight_id, ev)


def test_provenance_reports_staleness(client, settings, stored_insight_ids):
    from aker_etl.db import connect

    insight_id = stored_insight_ids[0]
    fake_sha = "0" * 64
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT prompt_sha256 FROM core.insight WHERE insight_id = %s", (insight_id,)
        )
        original = cur.fetchone()[0]
        cur.execute(
            "UPDATE core.insight SET prompt_sha256 = %s WHERE insight_id = %s",
            (fake_sha, insight_id),
        )
    try:
        r = client.get(f"/api/insight/{insight_id}/provenance")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["stale"] is True
        assert d["payload_at_generation"].strip() == fake_sha
        assert d["payload_current"] and d["payload_current"].strip() != fake_sha
    finally:
        with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE core.insight SET prompt_sha256 = %s WHERE insight_id = %s",
                (original, insight_id),
            )


def test_provenance_on_an_unknown_insight_is_a_404(client):
    r = client.get("/api/insight/99999999/provenance")
    assert r.status_code == 404
