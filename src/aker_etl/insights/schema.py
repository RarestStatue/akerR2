"""The LLM output contract. Mirrors core.insight exactly.

Kept flat and shallow on purpose: deeply nested schemas degrade output quality on
small models noticeably more than on large ones, and this schema is enforced by
the sampler (grammar-constrained decoding), not checked after the fact.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Scope = Literal["portfolio", "asset", "property"]
Category = Literal["occupancy", "revenue", "concession", "expiration", "delinquency",
                   "unit_mix", "data_quality", "trend"]
Priority = Literal["low", "medium", "high"]


class Evidence(BaseModel):
    metric: str = Field(description="Name of the metric, copied from the input data.")
    value: str = Field(description="The value, copied verbatim from the input data.")
    comparison: str | None = Field(
        default=None, description="Optional context, e.g. 'vs portfolio 94.1%'."
    )


class Insight(BaseModel):
    scope: Scope
    property_code: str | None = Field(
        default=None, description="Set only when scope='property'."
    )
    asset_key: str | None = Field(default=None, description="Set only when scope='asset'.")
    category: Category
    priority: Priority
    headline: str = Field(min_length=8, max_length=120)
    detail: str = Field(min_length=10, max_length=600)
    # The prompt asks for up to 4. The cap is looser than the ask because a
    # chatty reply used to fail *the whole batch* on `too_long`, losing five good
    # insights to one verbose sixth. Every entry is checked against the payload
    # either way, so accepting extras costs nothing but a few tokens.
    evidence: list[Evidence] = Field(min_length=1, max_length=10)


class InsightBatch(BaseModel):
    # Same reasoning as the evidence cap: the prompt asks for at most 6, and the
    # cap here is looser so that an over-eager reply is trimmed by the ranking
    # further down rather than thrown away whole. The reduce pass returned 10
    # well-formed insights and lost all 10 to a `too_long` on this field.
    insights: list[Insight] = Field(default_factory=list, max_length=12)


def normalise_target(
    insight: Insight,
    *,
    property_codes: frozenset[str] = frozenset(),
    asset_keys: frozenset[str] = frozenset(),
) -> Insight:
    """Clear whichever target field the scope does not own, and fix a swapped scope.

    Two model behaviours to absorb, both observed on qwen3.5:4b:

    1. It picks the right scope and fills the matching target, then helpfully
       fills the other one too -- `scope='property'` with `property_code='115r'`
       *and* `asset_key='115'`. That used to fail `scope_target_ok` and drop an
       otherwise sound insight; it was 32 of 33 drops on the first full run.
    2. It puts a property code in `asset_key` and labels the insight
       `scope='asset'`, so `143c` -- a property -- was published as an asset.

    Case 1 is resolved by letting the scope decide which field is authoritative;
    that invents nothing, since a property already implies its asset. Case 2 is
    resolved by believing the *identifier* over the scope label, because the
    identifier is checkable against the database and the label is not.

    Pass the known identifier sets to get case 2. With them empty this does case
    1 only.
    """
    code, key = insight.property_code, insight.asset_key

    # Only re-scope when the value is unambiguous: absent from the set its own
    # scope implies, and present in the other one.
    if insight.scope == "asset" and key and key not in asset_keys and key in property_codes:
        return insight.model_copy(
            update={"scope": "property", "property_code": key, "asset_key": None}
        )
    if insight.scope == "property" and code and code not in property_codes and code in asset_keys:
        return insight.model_copy(
            update={"scope": "asset", "asset_key": code, "property_code": None}
        )

    if insight.scope == "portfolio":
        return insight.model_copy(update={"property_code": None, "asset_key": None})
    if insight.scope == "asset":
        return insight.model_copy(update={"property_code": None})
    return insight.model_copy(update={"asset_key": None})


def target_is_known(
    insight: Insight, property_codes: frozenset[str], asset_keys: frozenset[str]
) -> bool:
    """Does the insight point at something that actually exists?

    The evidence gate proves every *figure* came from the payload. It says
    nothing about the *subject*, so an insight could attach a real number to an
    invented property. This closes that gap. Empty sets disable the check, so
    callers without database access behave as before.
    """
    if insight.scope == "portfolio":
        return True
    if insight.scope == "asset":
        return not asset_keys or insight.asset_key in asset_keys
    return not property_codes or insight.property_code in property_codes


def scope_target_ok(insight: Insight) -> bool:
    """Same rule as the insight_scope_target CHECK, applied before the insert."""
    if insight.scope == "portfolio":
        return insight.property_code is None and insight.asset_key is None
    if insight.scope == "asset":
        return bool(insight.asset_key) and not insight.property_code
    return bool(insight.property_code) and not insight.asset_key
