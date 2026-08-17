"""Post-load reconciliation. PLAN.md 5.4.

Every rule writes to raw.load_issue; severity decides the exit code. These run
inside the load transaction, against core tables only (not the mart views, which
cannot be refreshed until after COMMIT).
"""

from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

# Report label -> the detail predicate it should equal.
SUMMARY_GROUP_PREDICATES = {
    "Current/Notice/Vacant Residents": "l.section = 'current'",
    "Future Residents/Applicants": "l.section = 'future'",
    "Occupied Units": "l.occupancy_status IN ('occupied','notice')",
    "Total Vacant Units": "l.occupancy_status = 'vacant'",
    "Total Non Rev Units": "l.occupancy_status IN ('model','down')",
    # 'Totals:' is deliberately excluded: in the source it mixes scopes -- its
    # balance covers current+future while its market_rent covers current only --
    # so it is stored verbatim but never reconciled against.
}


def _emit(cur, run_id: int, severity: str, rule: str, detail: dict) -> None:
    cur.execute(
        """INSERT INTO raw.load_issue (run_id, severity, rule, detail)
           VALUES (%s,%s,%s,%s)""",
        (run_id, severity, rule, Jsonb(detail)),
    )


def run_validations(cur, run_id: int, snapshot_ids: list[int]) -> dict[str, int]:
    """Returns {rule: count}. Callers decide what to do with the counts."""
    counts: dict[str, int] = {}

    def emit_all(rule: str, severity: str, rows: list[dict]) -> None:
        for row in rows:
            _emit(cur, run_id, severity, rule, row)
        if rows:
            counts[rule] = len(rows)

    if not snapshot_ids:
        return counts

    # --- charge summary block vs the detail charge lines ---------------------- #
    cur.execute(
        """
        WITH detail AS (
          SELECT l.snapshot_id, l.property_id, lc.charge_code, sum(lc.amount) AS amount
          FROM core.lease l
          JOIN core.lease_charge lc ON lc.lease_id = l.lease_id
          WHERE l.snapshot_id = ANY(%s)
            AND l.occupancy_status IN ('occupied','notice')
          GROUP BY 1,2,3
        )
        SELECT p.property_code, cs.charge_code, cs.amount,
               COALESCE(d.amount, 0) AS detail_amount
        FROM core.charge_summary cs
        JOIN core.property p ON p.property_id = cs.property_id
        LEFT JOIN detail d ON d.snapshot_id = cs.snapshot_id
                          AND d.property_id = cs.property_id
                          AND d.charge_code = cs.charge_code
        WHERE cs.snapshot_id = ANY(%s)
          AND abs(cs.amount - COALESCE(d.amount, 0)) > 0.01
        """,
        (snapshot_ids, snapshot_ids),
    )
    emit_all("charge_summary_vs_detail", "error", [
        {"property_code": r[0], "charge_code": r[1],
         "report_amount": str(r[2]), "detail_amount": str(r[3])}
        for r in cur.fetchall()
    ])

    # --- summary-group unit counts and charge totals vs the detail ------------ #
    for label, predicate in SUMMARY_GROUP_PREDICATES.items():
        cur.execute(
            f"""
            WITH detail AS (
              SELECT l.snapshot_id, l.property_id,
                     count(*) FILTER (WHERE {predicate}) AS n,
                     sum(l.charges_total) FILTER (WHERE {predicate}) AS charges
              FROM core.lease l
              WHERE l.snapshot_id = ANY(%s)
              GROUP BY 1,2
            )
            SELECT p.property_code, g.unit_count, COALESCE(d.n, 0),
                   g.lease_charges, COALESCE(d.charges, 0)
            FROM core.rent_roll_summary_group g
            JOIN core.property p ON p.property_id = g.property_id
            LEFT JOIN detail d ON d.snapshot_id = g.snapshot_id AND d.property_id = g.property_id
            WHERE g.snapshot_id = ANY(%s) AND g.group_label = %s
            """,  # noqa: S608 - predicate comes from a module-level literal map
            (snapshot_ids, snapshot_ids, label),
        )
        unit_bad, charge_bad = [], []
        for code, reported_units, detail_units, reported_charges, detail_charges in cur.fetchall():
            if reported_units is not None and int(reported_units) != int(detail_units):
                unit_bad.append({"property_code": code, "group_label": label,
                                 "report_units": int(reported_units),
                                 "detail_units": int(detail_units)})
            if reported_charges is not None and abs(reported_charges - detail_charges) > 0.01:
                charge_bad.append({"property_code": code, "group_label": label,
                                   "report_charges": str(reported_charges),
                                   "detail_charges": str(detail_charges)})
        emit_all("summary_group_units_vs_detail", "error", unit_bad)
        emit_all("summary_group_charges_vs_detail", "error", charge_bad)

    # --- availability vs detail (warning: 153c is a real upstream discrepancy) - #
    # snapshot_id is in the GROUP BY on purpose: unit_availability has one row per
    # (snapshot, property), so grouping on (property_code, units) alone collapses
    # two snapshots that report the same unit count into one group and doubles the
    # lease count, firing this warning on every property from the second load on.
    cur.execute(
        """
        SELECT ua.snapshot_id, p.property_code, ua.units,
               count(*) FILTER (WHERE l.section = 'current') AS detail_units
        FROM core.unit_availability ua
        JOIN core.property p ON p.property_id = ua.property_id
        LEFT JOIN core.lease l ON l.snapshot_id = ua.snapshot_id
                              AND l.property_id = ua.property_id
        WHERE ua.snapshot_id = ANY(%s)
        GROUP BY ua.snapshot_id, p.property_code, ua.units
        HAVING ua.units <> count(*) FILTER (WHERE l.section = 'current')
        ORDER BY 1, 2
        """,
        (snapshot_ids,),
    )
    emit_all("availability_units_vs_detail", "warning", [
        {"snapshot_id": r[0], "property_code": r[1],
         "availability_units": r[2], "detail_units": r[3]}
        for r in cur.fetchall()
    ])

    # The two reports can also disagree on how the same unit count splits between
    # occupied / notice / vacant, without disagreeing on the total. Six properties
    # do today (139c and 143c have zeroed availability columns on a commercial
    # book; 175r, 176r, 185r differ by 2, 2 and 1 unit). Informational: the rent
    # roll is the detail record, and mart.reconciliation exposes the delta.
    cur.execute(
        """
        WITH detail AS (
          SELECT l.snapshot_id, l.property_id,
                 count(*) FILTER (WHERE l.occupancy_status IN ('occupied','notice')) AS occupied
          FROM core.lease l WHERE l.snapshot_id = ANY(%s) GROUP BY 1,2
        )
        SELECT p.property_code,
               ua.occupied_no_notice + ua.notice_rented + ua.notice_unrented,
               COALESCE(d.occupied, 0)
        FROM core.unit_availability ua
        JOIN core.property p ON p.property_id = ua.property_id
        LEFT JOIN detail d ON d.snapshot_id = ua.snapshot_id AND d.property_id = ua.property_id
        WHERE ua.snapshot_id = ANY(%s)
          AND ua.occupied_no_notice + ua.notice_rented + ua.notice_unrented
              <> COALESCE(d.occupied, 0)
        ORDER BY 1
        """,
        (snapshot_ids, snapshot_ids),
    )
    emit_all("availability_occupancy_split_vs_detail", "info", [
        {"property_code": r[0], "availability_occupied": r[1], "detail_occupied": r[2]}
        for r in cur.fetchall()
    ])

    # --- informational: shape of the portfolio, not defects ------------------- #
    cur.execute(
        """SELECT p.property_code, count(*)
           FROM core.lease l
           JOIN core.property p ON p.property_id = l.property_id
           JOIN core.snapshot s ON s.snapshot_id = l.snapshot_id
           WHERE l.snapshot_id = ANY(%s)
             AND l.occupancy_status IN ('occupied','notice')
             AND l.lease_expiration < s.as_of_date
           GROUP BY 1 ORDER BY 1""",
        (snapshot_ids,),
    )
    emit_all("expired_lease_no_moveout", "info", [
        {"property_code": r[0], "count": r[1]} for r in cur.fetchall()
    ])

    cur.execute(
        """SELECT p.property_code, count(*)
           FROM core.lease l
           JOIN core.property p ON p.property_id = l.property_id
           JOIN core.snapshot s ON s.snapshot_id = l.snapshot_id
           WHERE l.snapshot_id = ANY(%s) AND l.move_out < s.as_of_date
           GROUP BY 1 ORDER BY 1""",
        (snapshot_ids,),
    )
    emit_all("moveout_before_asof", "info", [
        {"property_code": r[0], "count": r[1]} for r in cur.fetchall()
    ])

    cur.execute(
        """SELECT p.property_code
           FROM core.property p
           WHERE NOT EXISTS (SELECT 1 FROM core.lease l
                             WHERE l.property_id = p.property_id AND l.snapshot_id = ANY(%s))
             AND EXISTS (SELECT 1 FROM core.unit_availability ua
                         WHERE ua.property_id = p.property_id AND ua.snapshot_id = ANY(%s))
           ORDER BY 1""",
        (snapshot_ids, snapshot_ids),
    )
    emit_all("zero_unit_property", "info", [{"property_code": r[0]} for r in cur.fetchall()])

    cur.execute(
        """SELECT p.property_code, count(*)
           FROM (SELECT lease_id, charge_code, count(*) AS n
                 FROM core.lease_charge GROUP BY 1,2 HAVING count(*) > 1) d
           JOIN core.lease l ON l.lease_id = d.lease_id
           JOIN core.property p ON p.property_id = l.property_id
           WHERE l.snapshot_id = ANY(%s)
           GROUP BY 1 ORDER BY 1""",
        (snapshot_ids,),
    )
    emit_all("duplicate_charge_code_in_block", "info", [
        {"property_code": r[0], "blocks_with_repeats": r[1]} for r in cur.fetchall()
    ])

    for rule, n in sorted(counts.items()):
        log.info("validation %s: %d", rule, n)
    return counts
