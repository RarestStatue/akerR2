-- Occupancy + revenue KPIs computed from the detail, comparable against the
-- report's own rollups and against unit_availability.
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.property_snapshot_kpi AS
SELECT
  l.snapshot_id, s.as_of_date, p.property_id, p.property_code, p.property_name,
  p.book_type, p.asset_key,
  count(*) FILTER (WHERE l.section = 'current')                          AS units,
  count(*) FILTER (WHERE l.occupancy_status IN ('occupied','notice'))    AS occupied_units,
  count(*) FILTER (WHERE l.occupancy_status = 'notice')                  AS notice_units,
  count(*) FILTER (WHERE l.occupancy_status = 'vacant')                  AS vacant_units,
  count(*) FILTER (WHERE l.occupancy_status IN ('model','down'))         AS non_revenue_units,
  count(*) FILTER (WHERE l.section = 'future')                           AS future_leases,
  round(100.0 * count(*) FILTER (WHERE l.occupancy_status IN ('occupied','notice'))
        / nullif(count(*) FILTER (WHERE l.section = 'current'), 0), 2)   AS pct_occupied,
  sum(l.market_rent)  FILTER (WHERE l.section = 'current')               AS market_rent,
  sum(l.charges_total) FILTER (WHERE l.section = 'current')              AS lease_charges,
  sum(l.unit_sqft)    FILTER (WHERE l.section = 'current')               AS square_feet,
  sum(l.balance)      FILTER (WHERE l.section = 'current')               AS balance,
  sum(l.resident_deposit) FILTER (WHERE l.section = 'current')           AS deposits
FROM core.lease l
JOIN core.property p USING (property_id)
JOIN core.snapshot s USING (snapshot_id)
GROUP BY l.snapshot_id, s.as_of_date, p.property_id;
CREATE UNIQUE INDEX IF NOT EXISTS property_snapshot_kpi_uq
  ON mart.property_snapshot_kpi (snapshot_id, property_id);

-- Charge mix: what each property actually bills, by category.
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.charge_mix AS
SELECT l.snapshot_id, l.property_id, c.category, lc.charge_code,
       count(*) AS line_count, sum(lc.amount) AS amount
FROM core.lease_charge lc
JOIN core.lease l       ON l.lease_id = lc.lease_id
JOIN core.charge_code c ON c.charge_code = lc.charge_code
GROUP BY l.snapshot_id, l.property_id, c.category, lc.charge_code;
CREATE UNIQUE INDEX IF NOT EXISTS charge_mix_uq
  ON mart.charge_mix (snapshot_id, property_id, category, charge_code);

-- Lease expiration ladder: the single most useful asset-management view.
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.expiration_schedule AS
SELECT l.snapshot_id, l.property_id,
       date_trunc('month', l.lease_expiration)::date AS expiry_month,
       count(*) AS expiring_leases,
       sum(l.charges_total) AS charges_at_risk,
       count(*) FILTER (WHERE l.lease_expiration < s.as_of_date) AS holdover_mtm
FROM core.lease l
JOIN core.snapshot s USING (snapshot_id)
WHERE l.occupancy_status IN ('occupied','notice') AND l.lease_expiration IS NOT NULL
GROUP BY l.snapshot_id, l.property_id, 3;
CREATE UNIQUE INDEX IF NOT EXISTS expiration_schedule_uq
  ON mart.expiration_schedule (snapshot_id, property_id, expiry_month);

-- Loss-to-lease / trade-out: contract rent vs market rent per occupied unit.
CREATE OR REPLACE VIEW mart.loss_to_lease AS
SELECT l.snapshot_id, l.property_id, l.unit_id, u.unit_code, l.unit_type_id,
       l.market_rent,
       coalesce(sum(lc.amount) FILTER (WHERE cc.category = 'rent'), 0)       AS contract_rent,
       coalesce(sum(lc.amount) FILTER (WHERE cc.category = 'concession'), 0) AS concessions,
       l.market_rent - coalesce(sum(lc.amount) FILTER (WHERE cc.category = 'rent'), 0)
                                                                            AS loss_to_lease
FROM core.lease l
JOIN core.unit u ON u.unit_id = l.unit_id
LEFT JOIN core.lease_charge lc ON lc.lease_id = l.lease_id
LEFT JOIN core.charge_code  cc ON cc.charge_code = lc.charge_code
WHERE l.occupancy_status IN ('occupied','notice')
GROUP BY l.lease_id, u.unit_code;

-- Detail-vs-report reconciliation, surfaced as data rather than buried in logs.
CREATE OR REPLACE VIEW mart.reconciliation AS
SELECT k.snapshot_id, k.property_code,
       k.units          AS detail_units,
       ua.units         AS availability_units,
       k.units - ua.units                          AS unit_delta,
       k.occupied_units AS detail_occupied,
       ua.occupied_no_notice + ua.notice_rented + ua.notice_unrented AS availability_occupied,
       k.lease_charges  AS detail_charges,
       g.lease_charges  AS report_charges,
       k.lease_charges - g.lease_charges           AS charge_delta
FROM mart.property_snapshot_kpi k
LEFT JOIN core.unit_availability ua
       ON ua.snapshot_id = k.snapshot_id AND ua.property_id = k.property_id
LEFT JOIN core.rent_roll_summary_group g
       ON g.snapshot_id = k.snapshot_id AND g.property_id = k.property_id
      AND g.group_label = 'Current/Notice/Vacant Residents';

-- Asset rollup: the books of one physical property, summed.
CREATE OR REPLACE VIEW mart.asset_snapshot_kpi AS
SELECT k.snapshot_id, k.as_of_date, k.asset_key,
       min(k.property_name)                                    AS asset_label,
       count(*)                                                AS book_count,
       array_agg(k.property_code::text ORDER BY k.property_code) AS property_codes,
       sum(k.units) AS units, sum(k.occupied_units) AS occupied_units,
       sum(k.vacant_units) AS vacant_units,
       sum(k.non_revenue_units) AS non_revenue_units,
       round(100.0 * sum(k.occupied_units) / nullif(sum(k.units),0), 2) AS pct_occupied,
       sum(k.market_rent) AS market_rent, sum(k.lease_charges) AS lease_charges,
       sum(k.square_feet) AS square_feet, sum(k.balance) AS balance
FROM mart.property_snapshot_kpi k
GROUP BY k.snapshot_id, k.as_of_date, k.asset_key;

-- Period-over-period movement per property. Empty until snapshot #2 exists.
CREATE OR REPLACE VIEW mart.property_trend AS
SELECT k.property_id, k.property_code, k.property_name,
       p.as_of_date AS prior_as_of, k.as_of_date AS current_as_of,
       k.pct_occupied,   k.pct_occupied  - p.pct_occupied   AS d_pct_occupied,
       k.market_rent,    k.market_rent   - p.market_rent    AS d_market_rent,
       k.lease_charges,  k.lease_charges - p.lease_charges  AS d_lease_charges,
       k.balance,        k.balance       - p.balance        AS d_balance,
       k.notice_units,   k.notice_units  - p.notice_units   AS d_notice_units
FROM mart.property_snapshot_kpi k
JOIN LATERAL (
  SELECT * FROM mart.property_snapshot_kpi pk
  WHERE pk.property_id = k.property_id AND pk.as_of_date < k.as_of_date
  ORDER BY pk.as_of_date DESC LIMIT 1
) p ON true;
