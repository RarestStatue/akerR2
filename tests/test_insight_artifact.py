"""Artifact contract tests. No network, no model, no database. PLAN3 7.1."""

from __future__ import annotations

import datetime as dt
import inspect
import json

import pytest
from pydantic import ValidationError

from aker_etl.insights.artifact import (
    ARTIFACT_VERSION,
    ArtifactGenerator,
    ArtifactInsight,
    ArtifactStats,
    ArtifactVersionError,
    InsightArtifact,
    read_artifact,
    write_artifact,
)
from aker_etl.insights.context import payload_sha256, targets_from_payload
from aker_etl.insights.generate import check_evidence, collect_values
from aker_etl.insights.schema import Evidence, Insight

SHA = "3f7c" + "0" * 60


def _artifact_insight(**kw) -> ArtifactInsight:
    base = dict(
        scope="property",
        property_code="115r",
        asset_key=None,
        category="occupancy",
        priority="high",
        headline="115r is the portfolio's occupancy outlier at 86.40%",
        detail="115r reports 86.40% physical occupancy against a portfolio 94.10%.",
        evidence=[Evidence(metric="pct_occupied", value="86.40", comparison="vs portfolio 94.10")],
        source_chunk="map:115",
    )
    base.update(kw)
    return ArtifactInsight(**base)


def _artifact(insights=None, **kw) -> InsightArtifact:
    base = dict(
        as_of=dt.date(2025, 9, 30),
        prompt_sha256=SHA,
        model="qwen3.5:4b",
        generated_at=dt.datetime(2026, 8, 16, 14, 3, 11, tzinfo=dt.timezone.utc),
        payload_source="database",
        generator=ArtifactGenerator(
            ollama_host="http://localhost:11434",
            num_ctx_map=4096,
            num_ctx_reduce=8192,
            num_predict=3072,
            positioning=True,
            seed=7,
        ),
        stats=ArtifactStats(chunks=11, calls=24, map_calls=11, positioning_calls=12,
                             reduce_calls=1, insights_kept=1, insights_dropped=4, elapsed_s=41.2),
    )
    base.update(kw)
    if insights is None:
        insights = [_artifact_insight()]
    return InsightArtifact(insights=insights, **base)


# --------------------------------------------------------------------------- #
# Read / write
# --------------------------------------------------------------------------- #


def test_artifact_round_trip(tmp_path):
    art = _artifact(insights=[_artifact_insight(), _artifact_insight(headline="A second finding here")])
    path = tmp_path / "insights.json"
    write_artifact(art, path)
    back = read_artifact(path)
    assert back == art
    assert back.insights[0].source_chunk == "map:115"


def test_artifact_write_is_atomic(tmp_path):
    path = tmp_path / "insights.json"
    write_artifact(_artifact(), path)
    assert list(tmp_path.glob("*.tmp")) == []


def test_artifact_rejects_unknown_version(tmp_path):
    path = tmp_path / "insights.json"
    raw = json.loads(_artifact().model_dump_json())
    raw["artifact_version"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ArtifactVersionError):
        read_artifact(path)
    assert ARTIFACT_VERSION == 1


def test_artifact_rejects_bad_sha():
    with pytest.raises(ValidationError):
        _artifact(prompt_sha256="deadbeef")
    with pytest.raises(ValidationError):
        _artifact(prompt_sha256="Z" * 64)


def test_artifact_rejects_short_headline(tmp_path):
    path = tmp_path / "insights.json"
    raw = json.loads(_artifact().model_dump_json())
    raw["insights"][0]["headline"] = "no"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        read_artifact(path)


def test_artifact_missing_source_chunk_still_reads(tmp_path):
    path = tmp_path / "insights.json"
    raw = json.loads(_artifact().model_dump_json())
    del raw["insights"][0]["source_chunk"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    back = read_artifact(path)
    assert back.insights[0].source_chunk is None


# --------------------------------------------------------------------------- #
# Payload hash stability -- the guard on the --from path
# --------------------------------------------------------------------------- #


def test_payload_json_round_trip_is_sha_stable():
    payload = {
        "as_of": "2025-09-30",
        "portfolio": {"units": 4013, "pct_occupied": "94.10"},
        "properties": [{"property_code": "115r", "units": 250}],
        "assets": [{"asset_key": "115"}],
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    back = json.loads(text)
    assert payload_sha256(payload) == payload_sha256(back)


def test_targets_from_payload():
    payload = {
        "properties": [{"property_code": "115r"}],
        "assets": [{"asset_key": "115"}],
    }
    codes, keys = targets_from_payload(payload)
    assert codes == {"115r"}
    assert keys == {"115"}

    empty_codes, empty_keys = targets_from_payload({})
    assert empty_codes == frozenset()
    assert empty_keys == frozenset()


# --------------------------------------------------------------------------- #
# The import-time gate (D6): whole-payload value set, evidence + target checks
# --------------------------------------------------------------------------- #


def test_gate_drops_fabricated_value():
    payload = {"properties": [{"property_code": "115r", "pct_occupied": "94.10"}]}
    allowed = collect_values(payload, set())
    insight = _artifact_insight(evidence=[Evidence(metric="vacant_units", value="999999")])
    base = Insight(**insight.model_dump(exclude={"source_chunk"}))
    ok, why = check_evidence(base, allowed, property_codes=frozenset({"115r"}))
    assert ok is False
    assert "999999" in (why or "")


def test_gate_accepts_payload_value_in_any_format():
    payload = {"properties": [{"property_code": "115r", "pct_occupied": "94.10"}]}
    allowed = collect_values(payload, set())
    insight = _artifact_insight(evidence=[Evidence(metric="pct_occupied", value="94.10%")])
    base = Insight(**insight.model_dump(exclude={"source_chunk"}))
    ok, why = check_evidence(base, allowed, property_codes=frozenset({"115r"}))
    assert ok is True, why


def test_gate_drops_unknown_property():
    payload = {"properties": [{"property_code": "115r", "pct_occupied": "94.10"}]}
    allowed = collect_values(payload, set())
    insight = _artifact_insight(property_code="zzz9",
                                 evidence=[Evidence(metric="pct_occupied", value="94.10")])
    base = Insight(**insight.model_dump(exclude={"source_chunk"}))
    ok, why = check_evidence(base, allowed, property_codes=frozenset({"115r"}))
    assert ok is False
    assert "does not exist" in (why or "")


# --------------------------------------------------------------------------- #
# Regression guard on route A (PLAN3 7.3)
# --------------------------------------------------------------------------- #


def test_generate_signature_unchanged():
    from aker_etl.insights.generate import generate

    sig = inspect.signature(generate)
    assert list(sig.parameters) == ["settings", "as_of", "force", "dry_run"]
    for name in ("as_of", "force", "dry_run"):
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------------- #
# Regression guards on the GAPS2 fixes. Each one names the defect it pins.
# --------------------------------------------------------------------------- #


def _payload() -> dict:
    """The smallest payload map_chunks/reduce_chunk will both accept."""
    return {
        "as_of": "2025-09-30",
        "portfolio": {"units": 250, "pct_occupied": "94.10", "property_count": 1},
        "rankings": {},
        "assets": [{"asset_key": "115"}],
        "properties": [{"property_code": "115r", "asset_key": "115", "units": 250}],
    }


def _enabled_settings():
    from aker_etl.config import get_settings

    s = get_settings()
    # The enabled gate runs before the payload file is parsed, and a skipped run
    # would make the malformed-payload assertions below vacuously pass.
    s.aker_insight_enabled = True
    return s


@pytest.mark.parametrize(
    "body",
    [
        "[1, 2, 3]",           # root is an array   -> payload["as_of"] raised TypeError
        '"a string"',          # root is a string   -> same
        "12",                  # root is a number   -> same
        '{"as_of": 20250930}', # as_of is not a str -> fromisoformat raised TypeError
        '{"nope": 1}',         # no as_of at all    -> KeyError
        "{not json",           # not JSON           -> ValueError
    ],
)
def test_from_payload_rejects_malformed_input_as_valueerror(tmp_path, body):
    """GAPS2 B1: only ValueError reaches the CLI, which turns it into exit 3.

    A TypeError escaping here is a Rich traceback and exit 1 -- the wrong code
    for a bad `--from`, on inputs a user produces by pointing the flag at the
    wrong file.
    """
    from aker_etl.insights.generate import generate_to_file

    payload_file = tmp_path / "payload.json"
    payload_file.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        generate_to_file(_enabled_settings(), tmp_path / "out.json", payload_file=payload_file)
    assert "is not an aker-etl payload" in str(exc.value)
    assert not (tmp_path / "out.json").exists()


def test_from_payload_dry_run_counts_chunks(tmp_path):
    """GAPS2 B4: route B fills GenerateOutcome.chunks like route A does."""
    from aker_etl.insights.generate import generate_to_file

    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_payload()), encoding="utf-8")

    out = generate_to_file(
        _enabled_settings(), tmp_path / "out.json", payload_file=payload_file, dry_run=True
    )

    assert out.chunks == 1  # one asset, one map chunk
    assert out.dry_run_report
    assert not (tmp_path / "out.json").exists()  # --dry-run writes nothing


def test_run_inference_accepts_precomputed_chunks():
    """The chunks= hand-off that stops map_chunks being rebuilt per route."""
    from aker_etl.insights.generate import _dry_run_report, run_inference

    for fn in (run_inference, _dry_run_report):
        assert "chunks" in inspect.signature(fn).parameters
        assert inspect.signature(fn).parameters["chunks"].default is None


def test_artifact_reads_without_generator_or_stats(tmp_path):
    """GAPS2 B3: both are diagnostic, so a hand-written artifact may omit them."""
    path = tmp_path / "insights.json"
    raw = json.loads(_artifact().model_dump_json())
    del raw["generator"]
    del raw["stats"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    back = read_artifact(path)

    assert back.generator is None
    assert back.stats == ArtifactStats()
    assert len(back.insights) == 1


def test_artifact_rejects_unknown_keys(tmp_path):
    """GAPS2 B5: a mistyped key must fail as a mistyped key.

    Without extra="forbid", `propertyCode` validates, imports as
    property_code=None and is dropped for "scope/target mismatch" -- a true
    refusal pointing at the wrong cause.
    """
    path = tmp_path / "insights.json"

    raw = json.loads(_artifact().model_dump_json())
    raw["insights"][0]["propertyCode"] = "115r"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError, match="propertyCode"):
        read_artifact(path)

    raw = json.loads(_artifact().model_dump_json())
    raw["as_of_date"] = "2025-09-30"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError, match="as_of_date"):
        read_artifact(path)


def test_import_reports_a_dead_database_as_failed(tmp_path):
    """GAPS2 B7: the database phase returns a failed outcome, never a traceback."""
    from aker_etl.config import get_settings
    from aker_etl.insights.store import import_artifact

    s = get_settings()
    s.postgres_host, s.postgres_port = "127.0.0.1", 1  # refused immediately

    path = tmp_path / "insights.json"
    write_artifact(_artifact(), path)

    outcome = import_artifact(s, path)

    assert outcome.status == "failed"
    assert outcome.snapshot_id is None
    assert "OperationalError" in outcome.error
    assert outcome.render().startswith("[red]import failed:")


def test_import_outcome_renders_drops_when_everything_was_dropped():
    """GAPS2 B2: the refusal must carry the per-insight reasons with it.

    All-dropped is exactly the case where the reason is the only diagnostic,
    and the early return on `refused` used to swallow the whole list.
    """
    from aker_etl.insights.store import ImportOutcome

    out = ImportOutcome(
        status="refused",
        error="no insight in the artifact survived the evidence check",
        read=2,
        dropped=2,
        drops=[("First dropped headline", "cites 999999, not in the payload"),
               ("Second dropped headline", "property zzz9 does not exist")],
    )
    rendered = out.render()

    assert "import refused" in rendered
    for headline, reason in out.drops:
        assert headline in rendered
        assert reason in rendered


def test_import_outcome_render_surfaces_read_count_and_stale():
    """The success line names its denominator, and never hides --allow-stale."""
    from aker_etl.insights.store import ImportOutcome

    out = ImportOutcome(
        status="succeeded", path="a.json", model="qwen3.5:4b", as_of="2025-09-30",
        snapshot_id=1, read=5, inserted=3, dropped=2, replaced=4,
        stale=True, artifact_sha="a" * 64, payload_sha="b" * 64,
        drops=[("Dropped headline", "cites 999999, not in the payload")],
    )
    rendered = out.render()

    assert "imported 3 of 5 insight(s)" in rendered
    assert "--allow-stale" in rendered
    assert "a" * 12 in rendered and "b" * 12 in rendered
    assert "Dropped headline" in rendered

    quiet = ImportOutcome(status="succeeded", read=1, inserted=1)
    assert "--allow-stale" not in quiet.render()
