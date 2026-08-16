"""`insights import` against a live database. PLAN3 7.2.

    docker compose -f docker/docker-compose.yml --env-file .env up -d db
    pytest -m integration

Needs Postgres loaded (the existing integration tests already load it). Must
NOT need Ollama -- artifacts are built by hand from the real payload.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from aker_etl.config import get_settings
from aker_etl.db import connect
from aker_etl.insights import store
from aker_etl.insights.artifact import (
    ArtifactGenerator,
    ArtifactInsight,
    ArtifactStats,
    InsightArtifact,
    write_artifact,
)
from aker_etl.insights.context import build_payload
from aker_etl.insights.schema import Evidence
from aker_etl.insights.store import import_artifact

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live_payload():
    s = get_settings()
    try:
        with connect(s, autocommit=True) as conn:
            payload, sha = build_payload(conn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unreachable: {exc}")
    if not payload.get("properties"):
        pytest.skip("no properties loaded")
    return s, payload, sha


@pytest.fixture
def clean(live_payload):
    """A snapshot with zero core.insight / core.insight_run rows, isolating each test."""
    settings, payload, sha = live_payload
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT snapshot_id FROM core.snapshot WHERE as_of_date = %s", (payload["as_of"],))
        snapshot_id = cur.fetchone()[0]
        cur.execute("DELETE FROM core.insight WHERE snapshot_id = %s", (snapshot_id,))
        cur.execute("DELETE FROM core.insight_run WHERE snapshot_id = %s", (snapshot_id,))
    return settings, payload, sha, snapshot_id


def _artifact(payload: dict, sha: str, *, insights=None, **kw) -> InsightArtifact:
    prop = payload["properties"][0]
    if insights is None:
        insights = [
            ArtifactInsight(
                scope="property",
                property_code=prop["property_code"],
                asset_key=None,
                category="occupancy",
                priority="medium",
                headline="Integration test insight for import",
                detail="This insight cites a real figure from the live payload for the gate.",
                evidence=[Evidence(metric="units", value=str(prop["units"]))],
                source_chunk="map:test",
            )
        ]
    base = dict(
        as_of=dt.date.fromisoformat(payload["as_of"]),
        prompt_sha256=sha,
        model="qwen3.5:4b",
        generated_at=dt.datetime.now(dt.timezone.utc),
        payload_source="database",
        generator=ArtifactGenerator(
            ollama_host="http://localhost:11434", num_ctx_map=4096, num_ctx_reduce=8192,
            num_predict=3072, positioning=True, seed=7,
        ),
        stats=ArtifactStats(),
        insights=insights,
    )
    base.update(kw)
    return InsightArtifact(**base)


def _seed_insight(settings, snapshot_id: int) -> None:
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO core.insight
                 (snapshot_id, scope, property_id, asset_key, category, priority,
                  headline, detail, evidence, model, prompt_sha256)
               VALUES (%s,'portfolio',NULL,NULL,'occupancy','low',
                       'Seed insight for isolation test', 'Placeholder detail text.',
                       '[]'::jsonb, 'seed', %s)""",
            (snapshot_id, "0" * 64),
        )


def _count(settings, snapshot_id: int) -> int:
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.insight WHERE snapshot_id = %s", (snapshot_id,))
        return cur.fetchone()[0]


def _max_id(settings, snapshot_id: int):
    """Max insight_id, so "unchanged" means these rows, not merely this many rows."""
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(insight_id) FROM core.insight WHERE snapshot_id = %s", (snapshot_id,))
        return cur.fetchone()[0]


def _run_rows(settings, snapshot_id: int):
    with connect(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, error, prompt_sha256 FROM core.insight_run WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        return cur.fetchall()


def test_import_replaces_wholesale(clean, tmp_path):
    settings, payload, sha, snapshot_id = clean
    _seed_insight(settings, snapshot_id)
    _seed_insight(settings, snapshot_id)

    art = _artifact(payload, sha)
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    outcome = import_artifact(settings, path)

    assert outcome.status == "succeeded"
    assert outcome.replaced == 2
    assert outcome.inserted == 1
    assert _count(settings, snapshot_id) == 1
    rows = _run_rows(settings, snapshot_id)
    assert len(rows) == 1
    assert rows[0][0] == "succeeded"
    assert rows[0][2].strip() == sha


def test_import_refuses_stale(clean, tmp_path):
    settings, payload, sha, snapshot_id = clean
    _seed_insight(settings, snapshot_id)
    before = _count(settings, snapshot_id)
    before_max = _max_id(settings, snapshot_id)

    art = _artifact(payload, sha, prompt_sha256="0" * 64)
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    outcome = import_artifact(settings, path)

    assert outcome.status == "refused"
    assert "0" * 12 in outcome.error
    assert sha[:12] in outcome.error
    assert _count(settings, snapshot_id) == before
    # PLAN3 7.2 #2: byte-identical, not merely the same row count -- a
    # delete-then-reinsert of two rows would keep the count and move the max.
    assert _max_id(settings, snapshot_id) == before_max


def test_import_allow_stale_records_provenance(clean, tmp_path):
    settings, payload, sha, snapshot_id = clean
    stale_sha = "0" * 64

    art = _artifact(payload, sha, prompt_sha256=stale_sha)
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    outcome = import_artifact(settings, path, allow_stale=True)

    assert outcome.status == "succeeded"
    assert outcome.stale is True
    rows = _run_rows(settings, snapshot_id)
    assert len(rows) == 1
    assert rows[0][0] == "succeeded"
    assert stale_sha[:12] in (rows[0][1] or "")
    assert rows[0][2].strip() == sha
    # The provenance is in the database; it must also be on the console, or the
    # one command that stored figures against a payload it did not see says so
    # nowhere the operator is looking.
    rendered = outcome.render()
    assert "--allow-stale" in rendered
    assert stale_sha[:12] in rendered
    assert sha[:12] in rendered


def test_import_drops_unknown_property(clean, tmp_path, caplog):
    settings, payload, sha, snapshot_id = clean
    _seed_insight(settings, snapshot_id)
    before = _count(settings, snapshot_id)

    bad = ArtifactInsight(
        scope="property", property_code="zzz9", asset_key=None, category="occupancy",
        priority="medium", headline="Bogus property citation for the gate test",
        detail="This insight names a property code that does not exist in the database.",
        evidence=[Evidence(metric="units", value=str(payload["properties"][0]["units"]))],
        source_chunk="map:test",
    )
    art = _artifact(payload, sha, insights=[bad])
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    with caplog.at_level(logging.WARNING, logger="aker_etl.insights.store"):
        outcome = import_artifact(settings, path)

    assert outcome.dropped == 1
    assert outcome.inserted == 0
    assert outcome.status == "refused"
    rows = _run_rows(settings, snapshot_id)
    assert any(r[0] == "refused" for r in rows)
    assert _count(settings, snapshot_id) == before
    # When every insight is dropped the per-insight reason is the whole
    # diagnostic: it has to reach both the log and the refusal output.
    assert any("Bogus property citation" in r.getMessage() for r in caplog.records)
    assert "Bogus property citation" in outcome.render()
    assert "does not exist" in outcome.render()


def test_import_drops_fabricated_evidence(clean, tmp_path):
    settings, payload, sha, snapshot_id = clean
    _seed_insight(settings, snapshot_id)
    before = _count(settings, snapshot_id)

    prop = payload["properties"][0]
    bad = ArtifactInsight(
        scope="property", property_code=prop["property_code"], asset_key=None,
        category="occupancy", priority="medium",
        headline="Fabricated figure for the gate test",
        detail="This insight cites a unit count that was never in the payload.",
        evidence=[Evidence(metric="units", value="123456789")],
        source_chunk="map:test",
    )
    art = _artifact(payload, sha, insights=[bad])
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    outcome = import_artifact(settings, path)

    assert outcome.dropped == 1
    assert outcome.inserted == 0
    assert outcome.status == "refused"
    rows = _run_rows(settings, snapshot_id)
    assert any(r[0] == "refused" for r in rows)
    assert _count(settings, snapshot_id) == before


def test_import_refuses_empty(clean, tmp_path):
    settings, payload, sha, snapshot_id = clean
    _seed_insight(settings, snapshot_id)
    before = _count(settings, snapshot_id)
    runs_before = len(_run_rows(settings, snapshot_id))

    art = _artifact(payload, sha, insights=[])
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    outcome = import_artifact(settings, path)

    assert outcome.status == "refused"
    assert _count(settings, snapshot_id) == before
    assert len(_run_rows(settings, snapshot_id)) == runs_before


def test_import_empty_with_allow_empty_clears(clean, tmp_path):
    """PLAN3 7.2 #7: --allow-empty lets the file through the D9 gate, but an
    artifact with zero insights still has zero insights that survive the
    evidence check (step 8), so it is refused at step 9 -- and step 9 runs
    before the DELETE, so nothing is cleared. This is deliberate: the flag
    only waives the "you probably didn't mean this" check, not the "don't
    store nothing" one.
    """
    settings, payload, sha, snapshot_id = clean
    _seed_insight(settings, snapshot_id)
    before = _count(settings, snapshot_id)

    art = _artifact(payload, sha, insights=[])
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    outcome = import_artifact(settings, path, allow_empty=True)

    assert outcome.status == "refused"
    assert _count(settings, snapshot_id) == before


def test_import_wrong_snapshot_date(clean, tmp_path):
    settings, payload, sha, snapshot_id = clean

    art = _artifact(payload, sha, as_of=dt.date(1999, 1, 1))
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    outcome = import_artifact(settings, path)

    assert outcome.status == "failed"
    assert "1999-01-01" in outcome.error


def test_import_is_repeatable(clean, tmp_path):
    settings, payload, sha, snapshot_id = clean

    art = _artifact(payload, sha)
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    first = import_artifact(settings, path)
    second = import_artifact(settings, path)

    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert second.replaced == 1
    assert second.inserted == 1
    rows = _run_rows(settings, snapshot_id)
    assert len(rows) == 2


def test_import_persist_failure_rolls_back_the_delete(clean, tmp_path, monkeypatch):
    """D8's "replaces wholesale" has to hold when the replace itself fails.

    _persist deletes the snapshot's insights and then reinserts them. On an
    autocommit connection each statement would commit on its own, so a failure
    between the two would leave the snapshot emptied. The explicit transaction
    around the call is what makes that impossible; this is the test that fails
    if someone removes it.
    """
    settings, payload, sha, snapshot_id = clean
    _seed_insight(settings, snapshot_id)
    _seed_insight(settings, snapshot_id)
    before = _count(settings, snapshot_id)
    before_max = _max_id(settings, snapshot_id)

    real_persist = store._persist

    def exploding_persist(conn, *args, **kw):
        real_persist(conn, *args, **kw)  # the DELETE and the INSERTs both run
        raise RuntimeError("simulated failure after the replace")

    monkeypatch.setattr(store, "_persist", exploding_persist)

    art = _artifact(payload, sha)
    path = tmp_path / "insights.json"
    write_artifact(art, path)

    outcome = import_artifact(settings, path)

    assert outcome.status == "failed"
    assert "simulated failure" in outcome.error
    assert _count(settings, snapshot_id) == before
    assert _max_id(settings, snapshot_id) == before_max
    # The run row stays 'failed': it is written before the replace and only
    # promoted after it, so a rolled-back replace leaves no successful run.
    assert all(r[0] == "failed" for r in _run_rows(settings, snapshot_id))
