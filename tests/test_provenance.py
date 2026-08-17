"""Evidence provenance: F9. PLAN6 phase 4. No database, no model."""

from __future__ import annotations

from aker_etl.insights.provenance import find_paths, walk_paths


def test_walk_paths_finds_a_nested_value():
    payload = {"properties": [{"property_code": "115r", "pct_occupied": 96.0}]}
    paths = dict(walk_paths(payload))
    assert paths["$.properties[0].property_code"] == "115r"
    assert paths["$.properties[0].pct_occupied"] == "96"


def test_matching_uses_the_same_normalisation_as_the_gate():
    payload = {"a": "96%", "b": "$96.00", "c": 96.0, "d": "96"}
    for value in ("96%", "$96.00", 96.0, "96"):
        paths = find_paths(payload, str(value))
        assert set(paths) == {"$.a", "$.b", "$.c", "$.d"}


def test_an_absent_value_returns_no_paths():
    payload = {"a": 1, "b": {"c": 2}}
    assert find_paths(payload, "999") == []


def test_paths_are_capped():
    payload = {"rows": [{"v": 42} for _ in range(50)]}
    paths = find_paths(payload, "42", limit=12)
    assert len(paths) == 12
