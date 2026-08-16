"""Matrix / positioning-insight tests. No database, no model. PLAN2 section 5.1."""

from __future__ import annotations

import re

from aker_etl.insights.context import positioning_chunks
from aker_etl.insights.fallback import positioning_fallback

BANNED_WORDS = ("NOI", "cap rate", "expense", "margin", "profit margin")

DISTRESSED_ROW = {
    "plottable": True, "quadrant": "distressed",
    "charges_to_threshold": 31169.30, "units_to_threshold": 3,
    "market_rent": 63954.00, "units": 23,
    "vacant_units": 2, "notice_units": 1,
    "concessions": 500.0, "loss_to_lease": 1000.0, "balance_owed": 200.0,
}

LEAKING_ROW = {
    "plottable": True, "quadrant": "leaking",
    "charges_to_threshold": 18305.52, "units_to_threshold": 0,
    "market_rent": 1108579.00, "units": 284,
    "vacant_units": 5, "notice_units": 3,
    "concessions": 2000.0, "loss_to_lease": 9000.0, "balance_owed": 500.0,
}

VACANCY_LED_ROW = {
    "plottable": True, "quadrant": "vacancy_led",
    "charges_to_threshold": 0.0, "units_to_threshold": 1,
    "market_rent": 32936.00, "units": 19,
    "vacant_units": 1, "notice_units": 0,
    "concessions": 0.0, "loss_to_lease": 0.0, "balance_owed": 0.0,
}

PERFORMING_ROW = {
    "plottable": True, "quadrant": "performing",
    "charges_to_threshold": 0.0, "units_to_threshold": 0,
    "market_rent": 86114.00, "units": 63,
    "vacant_units": 0, "notice_units": 2,
    "concessions": 0.0, "loss_to_lease": 0.0, "balance_owed": 0.0,
}

EXCLUDED_ROWS = [
    {"plottable": False, "exclusion_reason": "no_units"},
    {"plottable": False, "exclusion_reason": "no_market_rent"},
    {"plottable": False, "exclusion_reason": "no_charge_data"},
]

_NUM_RE = re.compile(r"-?\$?[\d,]*\.?\d+")


def _numbers_in(text: str) -> set[str]:
    """Bare numeric substrings, stripped of $ and commas, for membership checks."""
    return {m.strip("$,").replace(",", "") for m in _NUM_RE.findall(text)}


def _evidence_values(evidence: list[dict]) -> set[str]:
    return {str(e["value"]).strip("$,").replace(",", "") for e in evidence}


def test_positioning_fallback_covers_every_quadrant():
    for row in (DISTRESSED_ROW, LEAKING_ROW, VACANCY_LED_ROW, PERFORMING_ROW):
        out = positioning_fallback(row)
        assert out["generic"] is True
        assert out["quadrant"] == row["quadrant"]
        assert out["headline"]
        assert out["detail"]


def test_positioning_fallback_covers_every_exclusion_reason_and_none():
    for row in [*EXCLUDED_ROWS, None]:
        out = positioning_fallback(row)
        assert out["generic"] is True
        assert out["quadrant"] is None
        assert out["headline"].startswith("Not scored")
        assert out["evidence"] == []


def test_every_number_in_detail_is_in_evidence():
    """Same contract the evidence gate enforces on the LLM path."""
    for row in (DISTRESSED_ROW, LEAKING_ROW, VACANCY_LED_ROW, PERFORMING_ROW, *EXCLUDED_ROWS, None):
        out = positioning_fallback(row)
        detail_numbers = _numbers_in(out["detail"])
        evidence_numbers = _evidence_values(out["evidence"])
        assert detail_numbers <= evidence_numbers, (row, out)


def test_positioning_fallback_never_mentions_banned_terms():
    for row in (DISTRESSED_ROW, LEAKING_ROW, VACANCY_LED_ROW, PERFORMING_ROW, *EXCLUDED_ROWS, None):
        out = positioning_fallback(row)
        text = f"{out['headline']} {out['detail']}"
        for word in BANNED_WORDS:
            assert word.lower() not in text.lower(), (word, text)


def test_positioning_chunks_one_per_matrix_row():
    payload = {
        "as_of": "2026-02-25",
        "portfolio": {"pct_occupied": "94.5"},
        "matrix": [
            {"property_code": "153a", "capture_threshold": 95.0, "occupancy_threshold": 95.0},
            {"property_code": "115r", "capture_threshold": 95.0, "occupancy_threshold": 95.0},
        ],
    }
    chunks = positioning_chunks(payload)
    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk["as_of"] == "2026-02-25"
        assert chunk["property"]["property_code"] in {"153a", "115r"}
        assert chunk["portfolio_context"]["capture_threshold"] == 95.0
        assert chunk["portfolio_context"]["occupancy_threshold"] == 95.0
        assert chunk["portfolio_context"]["property_count"] == 2


def test_positioning_chunks_empty_matrix():
    payload = {"as_of": "2026-02-25", "portfolio": {}, "matrix": []}
    assert positioning_chunks(payload) == []


def test_call_forces_positioning_routing_before_the_evidence_gate():
    """A reply routed to a non-existent asset is rewritten, not dropped.

    Regression test for the real bug: forcing the fields after `_call` returned
    was too late, because check_evidence drops an unknown target first.
    """
    import json

    from aker_etl.config import Settings
    from aker_etl.insights.generate import QUADRANT_PRIORITY, _call
    from aker_etl.insights.schema import Insight

    reply = json.dumps({"insights": [{
        "scope": "asset", "asset_key": "999", "property_code": None,
        "category": "revenue", "priority": "low",
        "headline": "Occupancy is the binding constraint here",
        "detail": "Three more leased units would cross the occupancy line.",
        "evidence": [{"metric": "units_to_threshold", "value": "3"}],
    }]})

    class _StubClient:
        def chat(self, **kwargs):
            return {"message": {"content": reply}}

    chunk = {"as_of": "2026-02-25",
             "property": {"property_code": "153a", "quadrant": "distressed",
                          "units_to_threshold": 3},
             "portfolio_context": {"capture_threshold": 95.0, "occupancy_threshold": 95.0}}

    def force(insight: Insight) -> Insight:
        return insight.model_copy(update={
            "scope": "property", "property_code": "153a", "asset_key": None,
            "category": "positioning", "priority": QUADRANT_PRIORITY["distressed"],
        })

    kept, dropped, err = _call(
        _StubClient(), Settings(), chunk, "positioning:153a",
        num_ctx=4096, think=False,
        property_codes=frozenset({"153a"}), asset_keys=frozenset({"153"}),
        rewrite=force,
    )

    assert err is None
    assert dropped == 0
    assert len(kept) == 1
    assert kept[0].scope == "property"
    assert kept[0].property_code == "153a"
    assert kept[0].asset_key is None
    assert kept[0].category == "positioning"
    assert kept[0].priority == "high"


def test_priority_mapping_matches_quadrant():
    from aker_etl.insights.generate import QUADRANT_PRIORITY

    assert QUADRANT_PRIORITY == {
        "distressed": "high",
        "leaking": "medium",
        "vacancy_led": "medium",
        "performing": "low",
    }


def test_dsn_quotes_a_password_containing_a_space():
    """BUG.md 3: libpq splits on whitespace, so values must be quoted."""
    import psycopg

    from aker_etl.config import Settings

    s = Settings()
    s.postgres_password = "p@ss word"
    assert psycopg.conninfo.conninfo_to_dict(s.dsn)["password"] == "p@ss word"


def test_a_miscased_property_code_is_canonicalised_not_dropped():
    """BUG.md 5: property_code is citext, so case must not decide existence."""
    from aker_etl.insights.generate import check_evidence
    from aker_etl.insights.schema import Insight, normalise_target

    codes = frozenset({"115r"})
    raw = Insight(scope="property", property_code="115R", category="occupancy",
                  priority="low", headline="Occupancy note here",
                  detail="Detail text long enough.",
                  evidence=[{"metric": "pct_occupied", "value": "96.0"}])
    fixed = normalise_target(raw, property_codes=codes, asset_keys=frozenset())
    assert fixed.property_code == "115r"
    ok, why = check_evidence(fixed, {"96"}, property_codes=codes, asset_keys=frozenset())
    assert ok, why


def test_an_unknown_property_code_is_still_rejected():
    """The canonicalisation must not turn the existence check into a no-op."""
    from aker_etl.insights.generate import check_evidence
    from aker_etl.insights.schema import Insight, normalise_target

    codes = frozenset({"115r"})
    raw = Insight(scope="property", property_code="999z", category="occupancy",
                  priority="low", headline="Occupancy note here",
                  detail="Detail text long enough.",
                  evidence=[{"metric": "pct_occupied", "value": "96.0"}])
    fixed = normalise_target(raw, property_codes=codes, asset_keys=frozenset())
    ok, why = check_evidence(fixed, {"96"}, property_codes=codes, asset_keys=frozenset())
    assert not ok
    assert "does not exist" in (why or "")
