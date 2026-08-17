"""Read-only dashboard API + the single page that consumes it.

Every endpoint reads a mart view or a core table. Nothing is computed in Python
that SQL could compute, and nothing writes. Insights are read from core.insight;
the page renders fully when that table is empty.
"""

from __future__ import annotations

import atexit
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from psycopg_pool import ConnectionPool

from ..config import get_settings
from ..insights.context import build_payload
from ..insights.fallback import _EXCLUSION_DETAIL, positioning_fallback
from ..insights.provenance import find_paths
from .export import DATASETS, to_csv, to_xlsx

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Aker Rent Roll", docs_url="/api/docs", redoc_url=None)
_settings = get_settings()
# One pool for the process, not one connection per _q() call: a hot endpoint like
# property_detail runs eight queries, and connecting fresh for each is churn as
# well as eight separate transaction snapshots that a concurrent load can straddle.
_pool = ConnectionPool(_settings.dsn, kwargs={"autocommit": True}, open=True)
atexit.register(_pool.close)

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

# One entry, keyed by snapshot_id, deliberately never invalidated mid-session: a
# payload change between two requests is exactly the staleness that
# /api/insight/{id}/provenance already reports via its own `stale` field.
_PAYLOAD_CACHE: dict[int, tuple[str, dict]] = {}


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _q(sql: str, params: tuple = ()) -> list[dict]:
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        # `description` is None only for statements that return no result set;
        # every caller here runs a SELECT.
        cols = [d.name for d in cur.description or ()]
        return [{c: _jsonable(v) for c, v in zip(cols, row, strict=True)} for row in cur.fetchall()]


def _snapshot_id(as_of: str | None) -> tuple[int, str]:
    # Validated here rather than at each call site: seven endpoints take `as_of`
    # and all seven reach the database through this one function. Without it the
    # string goes straight into `%s::date` and psycopg's parser raises, which
    # FastAPI turns into a 500 -- a client typo is a 400.
    day: dt.date | None = None
    if as_of is not None:
        try:
            day = dt.date.fromisoformat(as_of)
        except ValueError:
            raise HTTPException(
                400, f"as_of must be an ISO date (YYYY-MM-DD), got {as_of!r}"
            ) from None
    rows = _q(
        "SELECT snapshot_id, as_of_date FROM core.snapshot "
        "WHERE (%s::date IS NULL OR as_of_date = %s::date) ORDER BY as_of_date DESC LIMIT 1",
        (day, day),
    )
    if not rows:
        raise HTTPException(404, "no snapshot loaded -- run `aker-etl load` first")
    return rows[0]["snapshot_id"], rows[0]["as_of_date"]


def _like(term: str) -> str:
    """Escape LIKE metacharacters so a search for '_' means '_'.

    Paired with `ESCAPE '\\'` in the query. Backslash first, or the escapes
    introduced by the next two replacements get escaped in turn.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = STATIC / "index.html"
    if not page.is_file():
        raise HTTPException(500, f"dashboard page not found at {page}")
    return page.read_text(encoding="utf-8")


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


@app.get("/api/economics")
def economics(as_of: str | None = None) -> dict:
    snap, as_of_date = _snapshot_id(as_of)
    return {
        "as_of": as_of_date,
        "exclusion_reasons": EXCLUSION_REASONS,
        # The long form, from insights/fallback.py, so the Economics tab and a
        # model-written positioning fallback explain an excluded book in the same
        # words rather than in two independently drifting ones.
        "exclusion_detail": _EXCLUSION_DETAIL,
        "bridge": _q(
            """SELECT property_code, property_name, book_type, asset_key,
                      gross_potential_rent, vacancy_loss, loss_to_lease, rent_charges,
                      subsidy, concessions, ancillary, billed_charges, charge_coverage,
                      exclusion_reason
               FROM mart.revenue_bridge WHERE snapshot_id = %s ORDER BY property_code""",
            (snap,),
        ),
        "bridge_portfolio": _q(
            """SELECT sum(gross_potential_rent) AS gross_potential_rent,
                      sum(vacancy_loss)  AS vacancy_loss,
                      sum(loss_to_lease) AS loss_to_lease,
                      sum(rent_charges)  AS rent_charges,
                      sum(subsidy)       AS subsidy,
                      sum(concessions)   AS concessions,
                      sum(ancillary)     AS ancillary,
                      sum(billed_charges) AS billed_charges,
                      count(*)           AS books
               FROM mart.revenue_bridge
               WHERE snapshot_id = %s AND exclusion_reason IS NULL""",
            (snap,),
        ),
        "outliers": _q(
            """SELECT property_code, unit_code, unit_type_code, unit_sqft, occupancy_status,
                      lease_expiration, lease_id, market_rent, contract_rent, rent_psf,
                      peer_units, median_market_rent, median_rent_psf, market_vs_median,
                      pct_vs_median, contract_vs_market
               FROM mart.unit_rent_outlier WHERE snapshot_id = %s
               ORDER BY abs(pct_vs_median) DESC, property_code, unit_code""",
            (snap,),
        ),
        "concentration": _q(
            """SELECT property_code, property_name, expiry_month::text AS expiry_month,
                      expiring_leases, charges_at_risk, holdover_mtm, dated_leases,
                      share_of_book, leases_to_shift, concentration_threshold, min_dated_leases
               FROM mart.expiration_concentration
               WHERE snapshot_id = %s AND concentrated
               ORDER BY share_of_book DESC""",
            (snap,),
        ),
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
            """SELECT insight_id, category::text AS category, priority::text AS priority,
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
    # Bounded at the edge, not in SQL: FastAPI rejects an out-of-range value with
    # a 422 naming the field, where PostgreSQL's `OFFSET -1` is a 500 with no
    # useful body. 1000 is five pages of the UI's own 200-row page size.
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    snap, _ = _snapshot_id(as_of)
    pattern = f"%{_like(q)}%" if q else None
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
             AND (%s::text IS NULL OR u.unit_code ILIKE %s ESCAPE '\\'
                                   OR r.display_name ILIKE %s ESCAPE '\\')
           ORDER BY p.property_code, u.unit_code, l.section
           LIMIT %s OFFSET %s""",
        (snap, property_code, property_code, status, status, pattern, pattern, pattern, limit, offset),
    )
    total = rows[0]["total"] if rows else 0
    for r in rows:
        r.pop("total", None)
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


@app.get("/api/lease/{lease_id}")
def lease_detail(lease_id: int) -> dict:
    """The header fields openLease() needs but a bare lease id does not carry.

    F8 deep links only put `lease_id` in the URL hash; every other caller of
    openLease() already has the full row from a table it clicked. This is what
    lets a pasted #lease=<id> link resume in a fresh tab.
    """
    rows = _q(
        """SELECT l.lease_id, p.property_code::text AS property_code, u.unit_code,
                  l.occupancy_status::text AS occupancy_status, l.resident_id, r.display_name,
                  l.market_rent, l.charges_total, l.balance
           FROM core.lease l
           JOIN core.property p ON p.property_id = l.property_id
           JOIN core.unit u ON u.unit_id = l.unit_id
           LEFT JOIN core.resident r ON r.resident_id = l.resident_id
           WHERE l.lease_id = %s""",
        (lease_id,),
    )
    if not rows:
        raise HTTPException(404, f"unknown lease_id {lease_id}")
    return rows[0]


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


@app.get("/api/leases/expiring")
def leases_expiring(month: str, as_of: str | None = None, property_code: str | None = None) -> dict:
    """Every lease behind one bar of the expiration ladder.

    The WHERE clause is a deliberate copy of mart.expiration_schedule's own
    predicate (sql/070_views_mart.sql) -- including the fact that it does *not*
    filter on section -- so `total` here always equals the `expiring_leases` the
    chart drew for that month, *for a refreshed snapshot*: mart.expiration_schedule
    is a materialized view, so a load that skips the refresh can leave it stale
    against core.lease even with identical predicates. tests/test_dashboard_expiring.py
    asserts the equality for every month, which is what catches the two drifting apart.

    No LIMIT: the tallest bar in the current corpus is 516 leases, and a bar the
    user just clicked is exactly the set they asked to see. Truncating it would
    contradict the count printed on the bar's own tooltip.
    """
    # Validated before the snapshot lookup so a malformed month is a 400 whatever
    # state the database is in, rather than a 500 out of psycopg's date parser.
    try:
        first = dt.date.fromisoformat(month)
    except ValueError:
        raise HTTPException(400, f"month must be an ISO date, got {month!r}") from None
    if first.day != 1:
        raise HTTPException(400, f"month must be the first day of a month, got {month!r}")
    snap, as_of_date = _snapshot_id(as_of)
    rows = _q(
        """SELECT p.property_code::text AS property_code, p.property_name,
                  u.unit_code, ut.unit_type_code, l.unit_sqft,
                  l.occupancy_status::text AS occupancy_status,
                  l.section::text AS section, l.resident_id, r.display_name,
                  l.market_rent, l.charges_total, l.balance,
                  l.move_in, l.lease_expiration, l.move_out,
                  (l.lease_expiration < s.as_of_date) AS holdover,
                  l.lease_id
           FROM core.lease l
           JOIN core.snapshot s ON s.snapshot_id = l.snapshot_id
           JOIN core.property p ON p.property_id = l.property_id
           JOIN core.unit u     ON u.unit_id = l.unit_id
           LEFT JOIN core.unit_type ut ON ut.unit_type_id = l.unit_type_id
           LEFT JOIN core.resident r   ON r.resident_id = l.resident_id
           WHERE l.snapshot_id = %s
             AND l.occupancy_status IN ('occupied','notice')
             AND l.lease_expiration IS NOT NULL
             AND date_trunc('month', l.lease_expiration)::date = %s::date
             AND (%s::text IS NULL OR p.property_code::text = %s::text)
           ORDER BY p.property_code, u.unit_code""",
        (snap, first, property_code, property_code),
    )
    return {"month": first.isoformat(), "as_of": as_of_date, "total": len(rows), "rows": rows}


@app.get("/api/anomalies")
def anomalies(as_of: str | None = None) -> dict:
    """Deterministic outliers. Independent of core.insight and of Ollama."""
    snap, as_of_date = _snapshot_id(as_of)
    rows = _q(
        """SELECT property_code, property_name, units, metric, label, unit, worse_when,
                  value, peer_mean, peer_sd, peer_books, z, adverse, priority
           FROM mart.property_anomaly
           WHERE snapshot_id = %s
           ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                    abs(z) DESC, property_code""",
        (snap,),
    )
    return {"as_of": as_of_date, "rows": rows}


@app.get("/api/insights")
def insights(as_of: str | None = None) -> dict:
    snap, _ = _snapshot_id(as_of)
    rows = _q(
        """SELECT i.insight_id, i.scope::text AS scope, p.property_code::text AS property_code,
                  i.asset_key,
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
               WHERE run_id IN (SELECT DISTINCT sf.run_id
                                FROM core.lease l
                                JOIN raw.source_file sf ON sf.file_id = l.file_id
                                WHERE l.snapshot_id = %s)
               GROUP BY severity, rule
               ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, rule""",
            (snap,),
        ),
        "issue_detail": _q(
            """SELECT severity::text AS severity, rule, detail
               FROM raw.load_issue
               WHERE run_id IN (SELECT DISTINCT sf.run_id
                                FROM core.lease l
                                JOIN raw.source_file sf ON sf.file_id = l.file_id
                                WHERE l.snapshot_id = %s)
                 AND severity <> 'info'
               ORDER BY severity, rule LIMIT 100""",
            (snap,),
        ),
        "runs": _q(
            """SELECT run_id, status::text AS status, started_at, finished_at, files_loaded,
                      files_skipped, files_failed, rows_loaded, tool_version
               FROM raw.ingest_run ORDER BY run_id DESC LIMIT 10"""
        ),
        "charge_codes": _q(
            """SELECT cc.charge_code, cc.category::text AS category, cc.description,
                      cc.is_concession, cc.label_verified,
                      count(lc.*) FILTER (WHERE l.lease_id IS NOT NULL)        AS line_count,
                      coalesce(sum(lc.amount) FILTER (WHERE l.lease_id IS NOT NULL), 0) AS amount,
                      count(DISTINCT l.property_id)                            AS properties
               FROM core.charge_code cc
               LEFT JOIN core.lease_charge lc ON lc.charge_code = cc.charge_code
               LEFT JOIN core.lease l         ON l.lease_id = lc.lease_id AND l.snapshot_id = %s
               GROUP BY cc.charge_code, cc.category, cc.description, cc.is_concession,
                        cc.label_verified
               ORDER BY cc.category, cc.charge_code""",
            (snap,),
        ),
        "charge_code_audit": _q(
            """SELECT charge_code, old_category::text AS old_category,
                      new_category::text AS new_category, old_description, new_description,
                      old_verified, new_verified, note, changed_by, changed_at
               FROM core.charge_code_audit ORDER BY changed_at DESC LIMIT 20"""
        ),
    }


@app.get("/api/export/{dataset}.{fmt}")
def export(dataset: str, fmt: str, as_of: str | None = None) -> Response:
    if dataset not in DATASETS:
        raise HTTPException(
            404, f"unknown dataset {dataset!r}; expected one of {', '.join(sorted(DATASETS))}"
        )
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(404, f"format must be csv or xlsx, got {fmt!r}")
    snap, as_of_date = _snapshot_id(as_of)
    stem, sql = DATASETS[dataset]
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (snap,))
        cols = [d.name for d in cur.description or ()]
        rows = cur.fetchall()
    payload = to_csv(cols, rows) if fmt == "csv" else to_xlsx(stem, cols, rows)
    filename = f"aker-{stem}-{as_of_date}.{fmt}"
    return Response(
        content=payload,
        media_type=(
            "text/csv; charset=utf-8" if fmt == "csv"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _cached_payload(conn, snapshot_id: int, as_of_date: dt.date) -> tuple[str, dict]:
    cached = _PAYLOAD_CACHE.get(snapshot_id)
    if cached is not None:
        return cached
    payload, sha = build_payload(conn, as_of_date)
    _PAYLOAD_CACHE[snapshot_id] = (sha, payload)
    return sha, payload


@app.get("/api/insight/{insight_id}/provenance")
def insight_provenance(insight_id: int) -> dict:
    """For each cited figure, where it appears in the payload the model was given.

    The payload is rebuilt from the mart views at request time. If its hash no
    longer matches the one recorded against the insight, say so: the figures may
    still all be found, but they are being found in a payload the model never saw.
    """
    rows = _q(
        """SELECT i.snapshot_id, i.prompt_sha256, i.evidence, s.as_of_date
           FROM core.insight i JOIN core.snapshot s ON s.snapshot_id = i.snapshot_id
           WHERE i.insight_id = %s""",
        (insight_id,),
    )
    if not rows:
        raise HTTPException(404, f"unknown insight_id {insight_id}")
    row = rows[0]
    with _pool.connection() as conn:
        payload_sha, payload = _cached_payload(conn, row["snapshot_id"], row["as_of_date"])

    findings = []
    for ev in row["evidence"] or []:
        paths = find_paths(payload, str(ev.get("value")))
        findings.append({
            "metric": ev.get("metric"), "value": ev.get("value"),
            "found": bool(paths), "paths": paths,
        })
    return {
        "insight_id": insight_id,
        "evidence": findings,
        "payload_current": payload_sha,
        "payload_at_generation": row["prompt_sha256"],
        "stale": payload_sha != row["prompt_sha256"],
    }
