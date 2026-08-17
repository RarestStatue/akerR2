"""Deterministic quadrant guidance -- pure function of one matrix row. PLAN2 2.7.4.

The dashboard must never show an empty insight panel: when no stored
`positioning` insight exists for a property (Ollama unreachable, generation not
yet run, dropped by the evidence gate, or the property is excluded from the
matrix), the property dialog renders this instead. It is text templating over
figures SQL already computed -- not computation -- so it does not violate
ground rule 1. Pure function of one dict; no DB, no model, so it is testable
without either.
"""

from __future__ import annotations

from typing import Any

_EXCLUSION_HEADLINES = {
    "no_units":       "Not scored: no units in this book",
    "no_market_rent": "Not scored: no market rent printed",
    "no_charge_data": "Not scored: no lease-charge lines",
}

_EXCLUSION_DETAIL = {
    "no_units": (
        "This book has no units in the current rent roll, so there is nothing to "
        "score. Occupancy and unit-mix figures below apply if any are recorded."
    ),
    "no_market_rent": (
        "The source workbook for this book prints no market rent, so revenue "
        "capture has no denominator and cannot be computed. Occupancy and unit "
        "mix are still shown below."
    ),
    "no_charge_data": (
        "The rent roll for this book contains no lease-charge lines, so revenue "
        "capture cannot be computed -- a zero reading here would be an artifact "
        "of missing data, not a fact about the asset. Occupancy and unit mix are "
        "still shown below."
    ),
}

_DEFAULT_EXCLUSION_HEADLINE = "Not scored: no data available"
_DEFAULT_EXCLUSION_DETAIL = "This book has no revenue-capture data for this snapshot."


def _or0(v: Any) -> Any:
    return 0 if v is None else v


def _fmt(value: Any) -> str:
    """Same format for `detail` and `evidence`, so every cited number matches."""
    if isinstance(value, float):
        return f"${value:,.2f}"
    return str(value)


def positioning_fallback(row: dict[str, Any] | None) -> dict:
    """Deterministic quadrant guidance. Pure function; no DB, no model.

    Returns {"headline": str, "detail": str, "evidence": [{metric, value}],
             "quadrant": str | None, "generic": True}.
    """
    if row is None or not row.get("plottable"):
        reason = str((row or {}).get("exclusion_reason") or "")
        return {
            "headline": _EXCLUSION_HEADLINES.get(reason, _DEFAULT_EXCLUSION_HEADLINE),
            "detail": _EXCLUSION_DETAIL.get(reason, _DEFAULT_EXCLUSION_DETAIL),
            "evidence": [],
            "quadrant": None,
            "generic": True,
        }

    quadrant = row.get("quadrant")
    evidence: list[dict[str, str]] = []

    def cite(metric: str, value: Any) -> str:
        s = _fmt(value)
        evidence.append({"metric": metric, "value": s})
        return s

    if quadrant == "performing":
        notice = cite("notice_units", _or0(row.get("notice_units")))
        zero = cite("charges_to_threshold", _or0(row.get("charges_to_threshold")))
        headline = "Performing: defend the position"
        detail = (
            f"Revenue capture and occupancy are both at or above threshold, with "
            f"{notice} units on notice. {zero} of additional billed revenue is "
            f"needed to stay above the capture line -- notice units are the thing "
            f"to watch, since losing them is what would move this book off "
            f"performing."
        )

    elif quadrant == "vacancy_led":
        units_gap = cite("units_to_threshold", _or0(row.get("units_to_threshold")))
        vacant = cite("vacant_units", _or0(row.get("vacant_units")))
        notice = cite("notice_units", _or0(row.get("notice_units")))
        headline = "Vacancy-led: lease up the empty units"
        detail = (
            f"Pricing and billing are already sound -- {units_gap} more occupied "
            f"units would cross the occupancy line. {vacant} units are vacant and "
            f"{notice} are on notice."
        )

    elif quadrant == "leaking":
        charges_gap = cite("charges_to_threshold", _or0(row.get("charges_to_threshold")))
        components = {
            "concessions":   _or0(row.get("concessions")),
            "loss_to_lease": _or0(row.get("loss_to_lease")),
            "balance_owed":  _or0(row.get("balance_owed")),
        }
        biggest_key = max(components, key=lambda k: components[k])
        biggest_val = cite(biggest_key, components[biggest_key])
        headline = "Leaking: full but under-collecting"
        detail = (
            f"The book is full but {charges_gap} short of the capture line. The "
            f"largest contributor is {biggest_key.replace('_', ' ')} at {biggest_val}."
        )

    else:  # distressed
        charges_gap_v = _or0(row.get("charges_to_threshold"))
        units_gap_v = _or0(row.get("units_to_threshold"))
        market_rent = _or0(row.get("market_rent"))
        units = _or0(row.get("units"))
        charge_frac = (charges_gap_v / market_rent) if market_rent else 0
        unit_frac = (units_gap_v / units) if units else 0
        charges_gap = cite("charges_to_threshold", charges_gap_v)
        units_gap = cite("units_to_threshold", units_gap_v)
        headline = "Distressed: empty and underpriced"
        if charge_frac >= unit_frac:
            detail = (
                f"The larger gap is pricing/collections: {charges_gap} of "
                f"additional billed revenue is needed to cross the capture line, "
                f"versus {units_gap} more occupied units to cross the occupancy "
                f"line."
            )
        else:
            detail = (
                f"The larger gap is vacancy: {units_gap} more occupied units are "
                f"needed to cross the occupancy line, versus {charges_gap} of "
                f"additional billed revenue to cross the capture line."
            )

    return {"headline": headline, "detail": detail, "evidence": evidence,
            "quadrant": quadrant, "generic": True}
