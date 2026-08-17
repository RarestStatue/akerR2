"""mart views -> compact JSON payload. PLAN.md 6.2.

SQL does every calculation. The model receives finished figures and produces
interpretation -- it never computes, ranks, or sorts anything. Ranking is an
ORDER BY, not a judgment call, so it happens here.

Serialization is byte-stable by construction (Decimal -> str, date -> ISO-8601,
sorted keys, nulls dropped) because `prompt_sha256` idempotency depends on it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any

import psycopg

from ..db import scalar


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _rows(cur, sql: str, params: tuple = ()) -> list[dict]:
    cur.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [
        {k: _jsonable(v) for k, v in zip(cols, row, strict=True) if v is not None}
        for row in cur.fetchall()
    ]


def canonical_json(payload: dict) -> str:
    """The exact bytes that get hashed and sent. Separators pinned for stability."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """~4 chars/token is close enough to size a context window with 30% headroom."""
    return max(1, len(text) // 4)


def latest_snapshot(conn: psycopg.Connection, as_of: dt.date | None = None) -> tuple[int, dt.date]:
    with conn.cursor() as cur:
        if as_of:
            cur.execute("SELECT snapshot_id, as_of_date FROM core.snapshot WHERE as_of_date = %s",
                        (as_of,))
        else:
            cur.execute("SELECT snapshot_id, as_of_date FROM core.snapshot "
                        "ORDER BY as_of_date DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        raise LookupError(f"no snapshot for {as_of}" if as_of else "no snapshots loaded")
    return row[0], row[1]


def build_payload(
    conn: psycopg.Connection, as_of: dt.date | None = None
) -> tuple[dict, str]:
    snapshot_id, as_of_date = latest_snapshot(conn, as_of)
    with conn.cursor() as cur:
        cur.execute("SELECT report_month FROM core.snapshot WHERE snapshot_id = %s", (snapshot_id,))
        report_month = scalar(cur)

        portfolio = _rows(cur, """
            SELECT sum(units) AS units, sum(occupied_units) AS occupied_units,
                   sum(vacant_units) AS vacant_units,
                   sum(non_revenue_units) AS non_revenue_units,
                   sum(future_leases) AS future_leases,
                   round(100.0*sum(occupied_units)/nullif(sum(units),0),2) AS pct_occupied,
                   sum(market_rent) AS market_rent, sum(lease_charges) AS lease_charges,
                   sum(square_feet) AS square_feet, sum(balance) AS balance,
                   count(*) AS property_count
            FROM mart.property_snapshot_kpi WHERE snapshot_id = %s
        """, (snapshot_id,))

        properties = _rows(cur, """
            SELECT property_code::text AS property_code, property_name, book_type::text AS book_type,
                   asset_key, units, occupied_units, notice_units, vacant_units,
                   non_revenue_units, future_leases, pct_occupied, market_rent,
                   lease_charges, square_feet, balance
            FROM mart.property_snapshot_kpi WHERE snapshot_id = %s
            ORDER BY property_code
        """, (snapshot_id,))

        assets = _rows(cur, """
            SELECT asset_key, asset_label, book_count, property_codes, units, occupied_units,
                   vacant_units, non_revenue_units, pct_occupied, market_rent, lease_charges,
                   square_feet, balance
            FROM mart.asset_snapshot_kpi WHERE snapshot_id = %s ORDER BY asset_key
        """, (snapshot_id,))

        charge_mix = _rows(cur, """
            SELECT p.property_code::text AS property_code, cm.category::text AS category,
                   sum(cm.line_count) AS line_count, sum(cm.amount) AS amount
            FROM mart.charge_mix cm JOIN core.property p USING (property_id)
            WHERE cm.snapshot_id = %s
            GROUP BY 1,2 ORDER BY 1,2
        """, (snapshot_id,))

        expirations = _rows(cur, """
            SELECT p.property_code::text AS property_code, e.expiry_month::text AS expiry_month,
                   e.expiring_leases, e.charges_at_risk, e.holdover_mtm
            FROM mart.expiration_schedule e JOIN core.property p USING (property_id)
            WHERE e.snapshot_id = %s
              AND e.expiry_month <= (%s::date + interval '12 months')
            ORDER BY 1,2
        """, (snapshot_id, as_of_date))

        loss_to_lease = _rows(cur, """
            SELECT p.property_code::text AS property_code, count(*) AS units,
                   sum(l.market_rent) AS market_rent, sum(l.contract_rent) AS contract_rent,
                   sum(l.concessions) AS concessions, sum(l.loss_to_lease) AS loss_to_lease
            FROM mart.loss_to_lease l JOIN core.property p USING (property_id)
            WHERE l.snapshot_id = %s GROUP BY 1 ORDER BY 1
        """, (snapshot_id,))

        reconciliation = _rows(cur, """
            SELECT property_code::text AS property_code, detail_units, availability_units,
                   unit_delta, detail_occupied, availability_occupied, charge_delta
            FROM mart.reconciliation
            WHERE snapshot_id = %s
              AND (coalesce(unit_delta,0) <> 0 OR coalesce(charge_delta,0) <> 0
                   OR detail_occupied <> coalesce(availability_occupied, detail_occupied))
            ORDER BY 1
        """, (snapshot_id,))

        data_quality = _rows(cur, """
            SELECT rule, severity::text AS severity, count(*) AS n
            FROM raw.load_issue
            WHERE run_id IN (SELECT DISTINCT sf.run_id
                             FROM core.lease l
                             JOIN raw.source_file sf ON sf.file_id = l.file_id
                             WHERE l.snapshot_id = %s)
            GROUP BY 1,2 ORDER BY 1,2
        """, (snapshot_id,))

        trend = _rows(cur, """
            SELECT property_code::text AS property_code, prior_as_of::text AS prior_as_of,
                   d_pct_occupied, d_market_rent, d_lease_charges, d_balance, d_notice_units
            FROM mart.property_trend WHERE current_as_of = %s ORDER BY property_code
        """, (as_of_date,))

        matrix = _rows(cur, """
            SELECT property_code::text AS property_code, property_name,
                   book_type::text AS book_type, quadrant, revenue_capture_pct,
                   pct_occupied, units, occupied_units, vacant_units, notice_units,
                   market_rent, lease_charges, charges_to_threshold, units_to_threshold,
                   loss_to_lease, concessions, ancillary_charges, units_owing,
                   balance_owed, capture_threshold, occupancy_threshold
            FROM mart.property_profitability
            WHERE snapshot_id = %s AND plottable
            ORDER BY revenue_capture_pct
        """, (snapshot_id,))

    # Rankings are computed here, in code, from the SQL results -- never left to
    # the model. It writes about an already-ordered list.
    def rank(key: str, *, reverse: bool, limit: int = 5) -> list[dict]:
        usable = [p for p in properties if p.get(key) is not None and p.get("units")]
        ordered = sorted(usable, key=lambda p: Decimal(str(p[key])), reverse=reverse)
        return [{"property_code": p["property_code"], key: p[key], "units": p["units"]}
                for p in ordered[:limit]]

    payload = {
        "as_of": as_of_date.isoformat(),
        "report_month": report_month.isoformat() if report_month else None,
        "portfolio": portfolio[0] if portfolio else {},
        "properties": properties,
        "assets": assets,
        "charge_mix": charge_mix,
        "expirations": expirations,
        "loss_to_lease": loss_to_lease,
        "matrix": matrix,
        "reconciliation": reconciliation,
        "data_quality": data_quality,
        "trend": trend,
        "rankings": {
            "lowest_occupancy": rank("pct_occupied", reverse=False),
            "highest_occupancy": rank("pct_occupied", reverse=True),
            "largest_negative_balance": rank("balance", reverse=False),
            "largest_market_rent": rank("market_rent", reverse=True),
        },
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return payload, payload_sha256(payload)


def targets_from_payload(payload: dict) -> tuple[frozenset[str], frozenset[str]]:
    """The property codes and asset keys present in a payload.

    The offline stand-in for generate._known_targets(), which reads core.property.
    This set is drawn from mart.property_snapshot_kpi for one snapshot and can in
    principle be a subset of the table; that only ever makes the offline target
    check stricter, and import re-checks against the database anyway, so the
    database stays authoritative.
    """
    codes = frozenset(
        str(p["property_code"]) for p in payload.get("properties", []) if p.get("property_code")
    )
    keys = frozenset(
        str(a["asset_key"]) for a in payload.get("assets", []) if a.get("asset_key")
    )
    return codes, keys


def map_chunks(payload: dict) -> list[dict]:
    """One chunk per asset: its books, charge mix, expirations, LTL, reconciliation.

    Small, self-contained slices -- the regime where a 4B model is actually
    reliable. PLAN.md 6.3.
    """
    by_asset: dict[str, dict] = {}
    for asset in payload.get("assets", []):
        by_asset[asset["asset_key"]] = {
            "as_of": payload["as_of"],
            "asset": asset,
            "properties": [],
            "charge_mix": [],
            "expirations": [],
            "loss_to_lease": [],
            "reconciliation": [],
            "portfolio_context": {
                k: payload["portfolio"].get(k)
                for k in ("pct_occupied", "units", "property_count")
                if payload["portfolio"].get(k) is not None
            },
        }
    code_to_asset = {p["property_code"]: p["asset_key"] for p in payload.get("properties", [])}
    for prop in payload.get("properties", []):
        chunk = by_asset.get(prop["asset_key"])
        if chunk:
            chunk["properties"].append(prop)
    for key in ("charge_mix", "expirations", "loss_to_lease", "reconciliation"):
        for row in payload.get(key, []):
            asset_key = code_to_asset.get(row.get("property_code"))
            chunk = by_asset.get(asset_key) if asset_key else None
            if chunk:
                chunk[key].append(row)
    return [by_asset[k] for k in sorted(by_asset)]


def positioning_chunks(payload: dict) -> list[dict]:
    """One chunk per plottable property: its own matrix row plus portfolio
    reference points. Small and self-contained, the regime a 4B model is
    reliable in - same reasoning as map_chunks.
    """
    rows = payload.get("matrix", [])
    portfolio = {
        "pct_occupied": payload["portfolio"].get("pct_occupied"),
        "property_count": len(rows),
        "capture_threshold": rows[0]["capture_threshold"] if rows else None,
        "occupancy_threshold": rows[0]["occupancy_threshold"] if rows else None,
    }
    return [{"as_of": payload["as_of"], "property": r,
             "portfolio_context": portfolio} for r in rows]


def reduce_chunk(payload: dict, map_headlines: list[dict]) -> dict:
    """Portfolio aggregate + rankings + every map-pass headline.

    Headlines rather than raw rows, so the reduce pass never re-derives a number.
    """
    return {
        "as_of": payload["as_of"],
        "portfolio": payload["portfolio"],
        "rankings": payload["rankings"],
        "reconciliation": payload.get("reconciliation", []),
        "data_quality": payload.get("data_quality", []),
        "asset_findings": map_headlines,
    }
