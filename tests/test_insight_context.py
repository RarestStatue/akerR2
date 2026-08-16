"""Insight-layer tests. No network, no model, no API key."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from aker_etl.config import Settings
from aker_etl.insights.context import canonical_json, estimate_tokens, payload_sha256
from aker_etl.insights.generate import check_evidence, collect_values
from aker_etl.insights.schema import (
    Evidence,
    Insight,
    InsightBatch,
    normalise_target,
    scope_target_ok,
)

SAMPLE = {
    "as_of": "2026-02-25",
    "portfolio": {"units": 4013, "pct_occupied": "95.29"},
    "properties": [{"property_code": "115r", "pct_occupied": "96.00", "balance": "-35465.66"}],
}


def test_serialisation_is_byte_stable():
    """prompt_sha256 idempotency depends on this exactly."""
    a, b = canonical_json(SAMPLE), canonical_json(dict(reversed(list(SAMPLE.items()))))
    assert a == b
    assert payload_sha256(SAMPLE) == payload_sha256(json.loads(a))


def test_hash_changes_when_a_kpi_changes():
    changed = json.loads(json.dumps(SAMPLE))
    changed["portfolio"]["pct_occupied"] = "95.30"
    assert payload_sha256(changed) != payload_sha256(SAMPLE)


def test_decimals_are_strings_not_floats():
    """Money must never round-trip through float on the way to the model."""
    assert '"pct_occupied":"95.29"' in canonical_json(SAMPLE)
    assert "95.29000" not in canonical_json(SAMPLE)


def test_token_estimate_is_monotonic():
    assert estimate_tokens("x" * 4000) > estimate_tokens("x" * 400) > 0


# --------------------------------------------------------------------------- #
# Schema contract
# --------------------------------------------------------------------------- #


def _insight(**kw) -> Insight:
    base = dict(scope="property", property_code="115r", category="occupancy",
                priority="medium", headline="Occupancy holding at 96%",
                detail="Occupancy is in line with the portfolio.",
                evidence=[Evidence(metric="pct_occupied", value="96.00")])
    base.update(kw)
    return Insight(**base)


def test_property_insight_without_a_code_is_rejected():
    assert not scope_target_ok(_insight(property_code=None))


def test_portfolio_insight_with_a_target_is_rejected():
    assert not scope_target_ok(_insight(scope="portfolio", property_code="115r"))


def test_asset_insight_needs_an_asset_key():
    assert scope_target_ok(_insight(scope="asset", property_code=None, asset_key="134"))
    assert not scope_target_ok(_insight(scope="asset", property_code=None, asset_key=None))


def test_empty_evidence_is_rejected():
    with pytest.raises(ValidationError):
        _insight(evidence=[])


def test_overlong_headline_is_rejected():
    with pytest.raises(ValidationError):
        _insight(headline="x" * 121)


def test_batch_cap_is_looser_than_the_prompt_asks_for():
    """The prompt asks for 6. A reply of 10 must not be discarded wholesale."""
    assert len(InsightBatch(insights=[_insight() for _ in range(10)]).insights) == 10
    with pytest.raises(ValidationError):
        InsightBatch(insights=[_insight() for _ in range(13)])


def test_evidence_cap_is_looser_than_the_prompt_asks_for():
    """A chatty reply must not fail the whole batch. The prompt asks for 4."""
    ev = [Evidence(metric=f"m{i}", value="96.00") for i in range(8)]
    assert len(_insight(evidence=ev).evidence) == 8
    with pytest.raises(ValidationError):
        _insight(evidence=[Evidence(metric=f"m{i}", value="96.00") for i in range(11)])


# --------------------------------------------------------------------------- #
# Target coercion -- the model fills the scope's target *and* the other one
# --------------------------------------------------------------------------- #


def test_property_scope_drops_a_redundant_asset_key():
    """This was 32 of 33 drops on the first full generation run."""
    raw = _insight(scope="property", property_code="115r", asset_key="115")
    assert not scope_target_ok(raw)
    fixed = normalise_target(raw)
    assert scope_target_ok(fixed)
    assert (fixed.property_code, fixed.asset_key) == ("115r", None)


def test_asset_scope_drops_a_redundant_property_code():
    fixed = normalise_target(_insight(scope="asset", property_code="115r", asset_key="115"))
    assert scope_target_ok(fixed)
    assert (fixed.property_code, fixed.asset_key) == (None, "115")


def test_portfolio_scope_drops_both_targets():
    fixed = normalise_target(_insight(scope="portfolio", property_code="115r", asset_key="115"))
    assert scope_target_ok(fixed)
    assert (fixed.property_code, fixed.asset_key) == (None, None)


CODES = frozenset({"115r", "143c", "153r"})
KEYS = frozenset({"115", "143", "153"})


def test_property_code_in_the_asset_slot_is_rescoped():
    """`143c` is a property. It was published as an asset on the first good run."""
    fixed = normalise_target(
        _insight(scope="asset", property_code=None, asset_key="143c"),
        property_codes=CODES, asset_keys=KEYS,
    )
    assert (fixed.scope, fixed.property_code, fixed.asset_key) == ("property", "143c", None)


def test_asset_key_in_the_property_slot_is_rescoped():
    fixed = normalise_target(
        _insight(scope="property", property_code="143", asset_key=None),
        property_codes=CODES, asset_keys=KEYS,
    )
    assert (fixed.scope, fixed.property_code, fixed.asset_key) == ("asset", None, "143")


def test_a_correct_target_is_left_alone():
    fixed = normalise_target(
        _insight(scope="asset", property_code=None, asset_key="143"),
        property_codes=CODES, asset_keys=KEYS,
    )
    assert (fixed.scope, fixed.asset_key) == ("asset", "143")


def test_an_invented_target_is_rejected():
    """A real figure attached to a property that does not exist must not publish."""
    allowed = collect_values(SAMPLE, set())
    ok, why = check_evidence(
        _insight(property_code="999z"), allowed, property_codes=CODES, asset_keys=KEYS
    )
    assert not ok
    assert "999z" in why


def test_target_check_is_skipped_without_identifier_sets():
    allowed = collect_values(SAMPLE, set())
    ok, _ = check_evidence(_insight(property_code="999z"), allowed)
    assert ok


def test_coercion_cannot_rescue_a_missing_target():
    """Scope decides which field is authoritative; it does not invent one."""
    assert not scope_target_ok(normalise_target(_insight(scope="property", property_code=None)))
    assert not scope_target_ok(
        normalise_target(_insight(scope="asset", property_code="115r", asset_key=None))
    )


# --------------------------------------------------------------------------- #
# Evidence gate -- the check that stops a hallucinated figure reaching the page
# --------------------------------------------------------------------------- #


def test_collect_values_normalises_number_formats():
    allowed = collect_values({"a": "96.00", "b": 4013, "c": "-35465.66"}, set())
    assert "96" in allowed and "4013" in allowed and "-35465.66" in allowed


def test_evidence_present_in_the_payload_passes():
    allowed = collect_values(SAMPLE, set())
    ok, why = check_evidence(_insight(), allowed)
    assert ok, why


def test_fabricated_evidence_is_rejected():
    allowed = collect_values(SAMPLE, set())
    ok, why = check_evidence(
        _insight(evidence=[Evidence(metric="pct_occupied", value="88.4")]), allowed
    )
    assert not ok
    assert "88.4" in why


def test_formatting_differences_are_tolerated():
    """96.00 / 96 / 96% / $96.00 are the same figure, not a fabrication."""
    allowed = collect_values(SAMPLE, set())
    for written in ("96", "96.0", "96%", "$96.00"):
        ok, _ = check_evidence(
            _insight(evidence=[Evidence(metric="pct_occupied", value=written)]), allowed
        )
        assert ok, written


# --------------------------------------------------------------------------- #
# Config guard for the VRAM budget
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tag", ["qwen3.5:latest", "qwen3.5", "llama3"])
def test_unpinned_model_tags_are_refused(tag):
    """:latest for qwen3.5 is the 9B; it does not fit 6 GB and silently spills to CPU."""
    with pytest.raises(ValidationError):
        Settings(aker_insight_model=tag)


def test_pinned_tag_is_accepted():
    assert Settings(aker_insight_model="qwen3.5:4b").aker_insight_model == "qwen3.5:4b"
