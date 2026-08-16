"""Read-only dashboard API + the single page that consumes it.

Every endpoint reads a mart view or a core table. Nothing is computed in Python
that SQL could compute, and nothing writes. Insights are read from core.insight;
the page renders fully when that table is empty.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..config import get_settings
from ..insights.fallback import positioning_fallback

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Aker Rent Roll", docs_url="/api/docs", redoc_url=None)
_settings = get_settings()

# Single source of truth for quadrant labels/hints and exclusion-reason text,
# shared by /api/matrix and the property dialog so the UI never hard-codes them.
QUADRANTS = {
    "performing": {"label": "Performing",
                   "hint": "Full and capturing rent. Protect the position."},
    "leaking":    {"label": "Leaking",
                   "hint": "Full, but revenue is lost to pricing, concessions or collections."},
    "vacancy_led": {"label": "Vacancy-led",
                    "hint": "Pricing and billing are sound; the loss is empty units."},
    "distressed": {"label": "Distressed",
                   "hint": "Empty and underpriced at the same time."},
}

EXCLUSION_REASONS = {
    "no_units":       "No units in this book",
    "no_market_rent": "Source prints no market rent (commercial book)",
    "no_charge_data": "Rent roll contains no lease-charge lines",
}


def _conn() -> psycopg.Connection:
    return psycopg.connect(_settings.dsn, autocommit=True)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _q(sql: str, params: tuple = ()) -> list[dict]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        # `description` is None only for statements that return no result set;
        # every caller here runs a SELECT.
        cols = [d.name for d in cur.description or ()]
        return [{c: _jsonable(v) for c, v in zip(cols, row, strict=True)} for row in cur.fetchall()]


def _snapshot_id(as_of: str | None) -> tuple[int, str]:
    rows = _q(
        "SELECT snapshot_id, as_of_date FROM core.snapshot "
        "WHERE (%s::date IS NULL OR as_of_date = %s::date) ORDER BY as_of_date DESC LIMIT 1",
        (as_of, as_of),
    )
    if not rows:
        raise HTTPException(404, "no snapshot loaded -- run `aker-etl load` first")
    return rows[0]["snapshot_id"], rows[0]["as_of_date"]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/snapshots")
def snapshots() -> list[dict]:
    return _q("SELECT snapshot_id, as_of_date, report_month FROM core.snapshot ORDER BY as_of_date DESC")


@app.get("/api/summary")
def summary(as_of: str | None = None) -> dict:
    snap, as_of_date = _snapshot_id(as_of)
    portfolio = _q(
        """SELECT sum(k.units) AS units, sum(k.occupied_units) AS occupied_units,
                  sum(k.notice_units) AS notice_units, sum(k.vacant_units) AS vacant_units,
                  sum(k.non_revenue_units) AS non_revenue_units,
                  sum(k.future_leases) AS future_leases,
                  round(100.0*sum(k.occupied_units)/nullif(sum(k.units),0),2) AS pct_occupied,
                  sum(k.market_rent) AS market_rent, sum(k.lease_charges) AS lease_charges,
                  sum(k.square_feet) AS square_feet, sum(k.balance) AS balance,
                  count(*) AS properties
           FROM core.property p
           LEFT JOIN mart.property_snapshot_kpi k
             ON k.property_id = p.property_id AND k.snapshot_id = %s""",
        (snap,),
    )
    # Driven from core.property, not the KPI view: 134land, 183c and altapm have
    # zero units and so produce no KPI row, but they are real books and belong in
    # the count and the table.
    properties = _q(
        """SELECT p.property_code::text AS property_code, p.property_name,
                  p.book_type::text AS book_type, p.asset_key,
                  COALESCE(k.units, 0) AS units,
                  COALESCE(k.occupied_units, 0) AS occupied_units,
                  COALESCE(k.notice_units, 0) AS notice_units,
                  COALESCE(k.vacant_units, 0) AS vacant_units,
                  COALESCE(k.non_revenue_units, 0) AS non_revenue_units,
                  COALESCE(k.future_leases, 0) AS future_leases,
                  k.pct_occupied, COALESCE(k.market_rent, 0) AS market_rent,
                  COALESCE(k.lease_charges, 0) AS lease_charges,
                  COALESCE(k.square_feet, 0) AS square_feet,
                  COALESCE(k.balance, 0) AS balance,
                  ua.available, ua.pct_leased
           FROM core.property p
           LEFT JOIN mart.property_snapshot_kpi k
             ON k.property_id = p.property_id AND k.snapshot_id = %s
           LEFT JOIN core.unit_availability ua
             ON ua.snapshot_id = %s AND ua.property_id = p.property_id
           ORDER BY p.property_code""",
        (snap, snap),
    )
    assets = _q(
        """SELECT asset_key, asset_label, book_count, property_codes, units, occupied_units,
                  vacant_units, pct_occupied, market_rent, lease_charges, balance
           FROM mart.asset_snapshot_kpi WHERE snapshot_id = %s ORDER BY units DESC""",
        (snap,),
    )
    expirations = _q(
        """SELECT expiry_month::text AS expiry_month, sum(expiring_leases) AS expiring_leases,
                  sum(charges_at_risk) AS charges_at_risk, sum(holdover_mtm) AS holdover_mtm
           FROM mart.expiration_schedule WHERE snapshot_id = %s
           GROUP BY 1 ORDER BY 1""",
        (snap,),
    )
    charge_mix = _q(
        """SELECT category::text AS category, sum(line_count) AS line_count, sum(amount) AS amount
           FROM mart.charge_mix WHERE snapshot_id = %s GROUP BY 1 ORDER BY 3 DESC""",
        (snap,),
    )
    status_mix = _q(
        """SELECT occupancy_status::text AS status, count(*) AS n
           FROM core.lease WHERE snapshot_id = %s GROUP BY 1 ORDER BY 2 DESC""",
        (snap,),
    )
    return {
        "as_of": as_of_date,
        "portfolio": portfolio[0] if portfolio else {},
        "properties": properties,
        "assets": assets,
        "expirations": expirations,
        "charge_mix": charge_mix,
        "status_mix": status_mix,
    }


@app.get("/api/matrix")
def matrix(as_of: str | None = None) -> dict:
    snap, as_of_date = _snapshot_id(as_of)
    rows = _q(
        """SELECT property_code::text AS property_code, property_name,
                  book_type::text AS book_type, asset_key, units, occupied_units,
                  notice_units, vacant_units, pct_occupied, market_rent,
                  lease_charges, revenue_capture_pct, quadrant, plottable,
                  exclusion_reason, charge_coverage, charges_to_threshold,
                  units_to_threshold, loss_to_lease, concessions,
                  ancillary_charges, units_owing, balance_owed,
                  capture_threshold, occupancy_threshold
           FROM mart.property_profitability
           WHERE snapshot_id = %s
           ORDER BY revenue_capture_pct NULLS LAST, property_code""",
        (snap,),
    )
    return {
        "as_of": as_of_date,
        # Echoed from the view's own constants (PLAN2 2.1): the thresholds are
        # declared once, in SQL. Empty when the snapshot has no rows at all, in
        # which case the plot renders its "no plottable properties" state and
        # never reads them.
        "thresholds": ({"revenue_capture": rows[0]["capture_threshold"],
                        "occupancy": rows[0]["occupancy_threshold"]} if rows else {}),
        "quadrants": QUADRANTS,
        "exclusion_reasons": EXCLUSION_REASONS,
        "points":   [r for r in rows if r["plottable"]],
        "excluded": [r for r in rows if not r["plottable"]],
    }


@app.get("/api/property/{code}")
def property_detail(code: str, as_of: str | None = None) -> dict:
    snap, _ = _snapshot_id(as_of)
    # Driven from core.property, LEFT JOIN the KPI view -- not FROM it. 134land,
    # 183c and altapm have zero units and so no KPI row; the Matrix tab's "Not
    # plotted" table makes them clickable, so this must 404 only on an unknown
    # property *code*, not an absent KPI row. Same pattern as summary() at
    # app.py:89-110.
    kpi = _q(
        """SELECT p.property_id, p.property_code::text AS property_code, p.property_name,
                  p.book_type::text AS book_type, p.asset_key,
                  COALESCE(k.units, 0) AS units,
                  COALESCE(k.occupied_units, 0) AS occupied_units,
                  COALESCE(k.notice_units, 0) AS notice_units,
                  COALESCE(k.vacant_units, 0) AS vacant_units,
                  COALESCE(k.non_revenue_units, 0) AS non_revenue_units,
                  COALESCE(k.future_leases, 0) AS future_leases,
                  k.pct_occupied, COALESCE(k.market_rent, 0) AS market_rent,
                  COALESCE(k.lease_charges, 0) AS lease_charges,
                  COALESCE(k.square_feet, 0) AS square_feet,
                  COALESCE(k.balance, 0) AS balance,
                  ua.units AS availability_units, ua.available, ua.pct_leased,
                  ua.avg_rent, ua.avg_sqft
           FROM core.property p
           LEFT JOIN mart.property_snapshot_kpi k
             ON k.property_id = p.property_id AND k.snapshot_id = %s
           LEFT JOIN core.unit_availability ua
             ON ua.snapshot_id = %s AND ua.property_id = p.property_id
           WHERE p.property_code = %s""",
        (snap, snap, code),
    )
    if not kpi:
        raise HTTPException(404, f"unknown property {code!r}")
    pid = kpi[0]["property_id"]
    matrix_rows = _q(
        """SELECT quadrant, revenue_capture_pct, pct_occupied, units, occupied_units,
                  vacant_units, notice_units, market_rent, lease_charges,
                  charges_to_threshold, units_to_threshold, loss_to_lease,
                  concessions, ancillary_charges, units_owing, balance_owed,
                  plottable, exclusion_reason, charge_coverage,
                  capture_threshold, occupancy_threshold
           FROM mart.property_profitability
           WHERE snapshot_id = %s AND property_id = %s""",
        (snap, pid),
    )
    return {
        "kpi": kpi[0],
        "matrix": matrix_rows,
        "positioning_fallback": positioning_fallback(matrix_rows[0] if matrix_rows else None),
        "insights": _q(
            """SELECT category::text AS category, priority::text AS priority,
                      headline, detail, evidence, model, generated_at
               FROM core.insight
               WHERE snapshot_id = %s AND property_id = %s
               ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                        CASE category WHEN 'positioning' THEN 0 ELSE 1 END,
                        insight_id""",
            (snap, pid),
        ),
        "charge_mix": _q(
            """SELECT category::text AS category, charge_code, line_count, amount
               FROM mart.charge_mix WHERE snapshot_id = %s AND property_id = %s
               ORDER BY amount DESC""",
            (snap, pid),
        ),
        "expirations": _q(
            """SELECT expiry_month::text AS expiry_month, expiring_leases, charges_at_risk,
                      holdover_mtm
               FROM mart.expiration_schedule WHERE snapshot_id = %s AND property_id = %s
               ORDER BY 1""",
            (snap, pid),
        ),
        "unit_types": _q(
            """SELECT ut.unit_type_code, count(*) AS units,
                      round(avg(l.market_rent), 2) AS avg_market_rent,
                      round(avg(l.unit_sqft), 0) AS avg_sqft,
                      count(*) FILTER (WHERE l.occupancy_status IN ('occupied','notice')) AS occupied
               FROM core.lease l
               JOIN core.unit_type ut ON ut.unit_type_id = l.unit_type_id
               WHERE l.snapshot_id = %s AND l.property_id = %s AND l.section = 'current'
               GROUP BY 1 ORDER BY 2 DESC""",
            (snap, pid),
        ),
        "loss_to_lease": _q(
            """SELECT count(*) AS units, sum(market_rent) AS market_rent,
                      sum(contract_rent) AS contract_rent, sum(concessions) AS concessions,
                      sum(loss_to_lease) AS loss_to_lease
               FROM mart.loss_to_lease WHERE snapshot_id = %s AND property_id = %s""",
            (snap, pid),
        ),
        "summary_groups": _q(
            """SELECT group_label, unit_count, square_footage, market_rent, lease_charges,
                      security_deposit, other_deposits, pct_unit_occupancy, balance
               FROM core.rent_roll_summary_group
               WHERE snapshot_id = %s AND property_id = %s ORDER BY group_label""",
            (snap, pid),
        ),
    }


@app.get("/api/units")
def units(
    as_of: str | None = None,
    property_code: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    snap, _ = _snapshot_id(as_of)
    rows = _q(
        """SELECT count(*) OVER () AS total,
                  p.property_code::text AS property_code, u.unit_code,
                  ut.unit_type_code, l.unit_sqft, l.occupancy_status::text AS occupancy_status,
                  l.section::text AS section, l.resident_id, r.display_name,
                  l.market_rent, l.charges_total, l.balance, l.move_in, l.lease_expiration,
                  l.move_out, l.lease_id
           FROM core.lease l
           JOIN core.property p ON p.property_id = l.property_id
           JOIN core.unit u ON u.unit_id = l.unit_id
           LEFT JOIN core.unit_type ut ON ut.unit_type_id = l.unit_type_id
           LEFT JOIN core.resident r ON r.resident_id = l.resident_id
           WHERE l.snapshot_id = %s
             AND (%s::text IS NULL OR p.property_code::text = %s::text)
             AND (%s::text IS NULL OR l.occupancy_status::text = %s::text)
             AND (%s::text IS NULL OR u.unit_code ILIKE '%%'||%s::text||'%%'
                                   OR r.display_name ILIKE '%%'||%s::text||'%%')
           ORDER BY p.property_code, u.unit_code, l.section
           LIMIT %s OFFSET %s""",
        (snap, property_code, property_code, status, status, q, q, q, limit, offset),
    )
    total = rows[0]["total"] if rows else 0
    for r in rows:
        r.pop("total", None)
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


@app.get("/api/lease/{lease_id}/charges")
def lease_charges(lease_id: int) -> list[dict]:
    return _q(
        """SELECT lc.line_no, lc.charge_code, cc.category::text AS category,
                  cc.description, cc.is_concession, lc.amount
           FROM core.lease_charge lc
           JOIN core.charge_code cc ON cc.charge_code = lc.charge_code
           WHERE lc.lease_id = %s ORDER BY lc.line_no""",
        (lease_id,),
    )


@app.get("/api/insights")
def insights(as_of: str | None = None) -> dict:
    snap, _ = _snapshot_id(as_of)
    rows = _q(
        """SELECT i.scope::text AS scope, p.property_code::text AS property_code, i.asset_key,
                  i.category::text AS category, i.priority::text AS priority,
                  i.headline, i.detail, i.evidence, i.model, i.generated_at
           FROM core.insight i
           LEFT JOIN core.property p ON p.property_id = i.property_id
           WHERE i.snapshot_id = %s
           ORDER BY CASE i.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                    i.scope, i.insight_id""",
        (snap,),
    )
    last_run = _q(
        """SELECT model, status, error, started_at, finished_at
           FROM core.insight_run WHERE snapshot_id = %s ORDER BY insight_run_id DESC LIMIT 1""",
        (snap,),
    )
    return {"insights": rows, "last_run": last_run[0] if last_run else None}


@app.get("/api/quality")
def quality(as_of: str | None = None) -> dict:
    snap, _ = _snapshot_id(as_of)
    return {
        "reconciliation": _q(
            """SELECT property_code::text AS property_code, detail_units, availability_units,
                      unit_delta, detail_occupied, availability_occupied,
                      detail_charges, report_charges, charge_delta
               FROM mart.reconciliation WHERE snapshot_id = %s ORDER BY property_code""",
            (snap,),
        ),
        "issues": _q(
            """SELECT severity::text AS severity, rule, count(*) AS n
               FROM raw.load_issue
               WHERE run_id = (SELECT max(run_id) FROM raw.ingest_run WHERE status <> 'running')
               GROUP BY severity, rule
               ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, rule"""
        ),
        "issue_detail": _q(
            """SELECT severity::text AS severity, rule, detail
               FROM raw.load_issue
               WHERE run_id = (SELECT max(run_id) FROM raw.ingest_run WHERE status <> 'running')
                 AND severity <> 'info'
               ORDER BY severity, rule LIMIT 100"""
        ),
        "runs": _q(
            """SELECT run_id, status::text AS status, started_at, finished_at, files_loaded,
                      files_skipped, files_failed, rows_loaded, tool_version
               FROM raw.ingest_run ORDER BY run_id DESC LIMIT 10"""
        ),
        "charge_codes": _q(
            """SELECT charge_code, category::text AS category, description, is_concession,
                      label_verified
               FROM core.charge_code ORDER BY category, charge_code"""
        ),
    }
