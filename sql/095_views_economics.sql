-- Economics tab: revenue bridge, rent positioning outliers, expiration
-- concentration. PLAN6 phase 1. Every figure here is arithmetic over rows the
-- loader already parsed from the source workbooks -- nothing is generated,
-- simulated or back-filled.

-- ---------------------------------------------------------------------------
-- F2 -- revenue bridge
-- ---------------------------------------------------------------------------

-- Gross potential rent decomposed into the six steps that reach billed charges.
-- Every term comes from core.lease / core.lease_charge, not from mart.charge_mix:
-- charge_mix is not section-filtered, and the bridge must be current-section only
-- or it will not close against mart.property_snapshot_kpi.lease_charges.
--
-- A plain VIEW, for the same reason mart.property_profitability is one: it reads
-- core directly, so it can never be stale against a matview refresh.
--
-- FROM core.snapshot x core.property, LEFT JOIN the aggregates below -- not FROM
-- the aggregates themselves. 134land, 183c and altapm carry zero current leases,
-- so building this FROM the per-property aggregates would drop them from the
-- view entirely instead of surfacing them as exclusion_reason='no_units', which
-- is the same fix mart.property_profitability already applies (sql/090, "base").
CREATE OR REPLACE VIEW mart.revenue_bridge AS
WITH const AS (SELECT 0.5::numeric AS coverage_threshold),
scope AS (
  SELECT l.snapshot_id, l.property_id, l.lease_id, l.occupancy_status, l.market_rent
  FROM core.lease l
  WHERE l.section = 'current'
),
charges AS (
  SELECT s.snapshot_id, s.property_id,
         sum(lc.amount) FILTER (WHERE cc.category = 'rent')       AS rent_charges,
         sum(lc.amount) FILTER (WHERE cc.category = 'subsidy')    AS subsidy,
         sum(lc.amount) FILTER (WHERE cc.category = 'concession') AS concessions,
         sum(lc.amount) FILTER (
           WHERE cc.category NOT IN ('rent','subsidy','concession'))  AS ancillary,
         sum(lc.amount)                                            AS billed_charges
  FROM scope s
  JOIN core.lease_charge lc ON lc.lease_id = s.lease_id
  JOIN core.charge_code cc  ON cc.charge_code = lc.charge_code
  GROUP BY 1, 2
),
rents AS (
  SELECT s.snapshot_id, s.property_id,
         sum(s.market_rent)                                       AS gross_potential_rent,
         sum(s.market_rent) FILTER (
           WHERE s.occupancy_status IN ('vacant','model','down'))  AS vacancy_loss,
         count(*)                                                 AS current_leases,
         count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM core.lease_charge lc WHERE lc.lease_id = s.lease_id)) AS leases_with_charges
  FROM scope s
  GROUP BY 1, 2
),
base AS (
  SELECT sn.snapshot_id, p.property_id, p.property_code::text AS property_code, p.property_name,
         p.book_type::text AS book_type, p.asset_key,
         coalesce(r.gross_potential_rent, 0)                          AS gross_potential_rent,
         coalesce(r.vacancy_loss, 0)                                  AS vacancy_loss,
         coalesce(r.current_leases, 0)                                AS current_leases,
         r.leases_with_charges,
         coalesce(c.rent_charges, 0)                                  AS rent_charges,
         coalesce(c.subsidy, 0)                                       AS subsidy,
         coalesce(c.concessions, 0)                                   AS concessions,
         coalesce(c.ancillary, 0)                                     AS ancillary,
         coalesce(c.billed_charges, 0)                                AS billed_charges,
         round(r.leases_with_charges::numeric / nullif(r.current_leases, 0), 3) AS charge_coverage
  FROM core.snapshot sn
  CROSS JOIN core.property p
  LEFT JOIN rents r   ON r.snapshot_id = sn.snapshot_id AND r.property_id = p.property_id
  LEFT JOIN charges c ON c.snapshot_id = sn.snapshot_id AND c.property_id = p.property_id
)
SELECT
  b.snapshot_id, b.property_id, b.property_code, b.property_name,
  b.book_type, b.asset_key,
  b.gross_potential_rent,
  b.vacancy_loss,
  b.gross_potential_rent - b.vacancy_loss - b.rent_charges           AS loss_to_lease,
  b.rent_charges, b.subsidy, b.concessions, b.ancillary, b.billed_charges,
  b.charge_coverage,
  CASE
    WHEN b.current_leases = 0                       THEN 'no_units'
    WHEN b.gross_potential_rent <= 0                THEN 'no_market_rent'
    WHEN coalesce(b.charge_coverage, 0)
           < (SELECT coverage_threshold FROM const) THEN 'no_charge_data'
  END                                                                AS exclusion_reason
FROM base b;

COMMENT ON VIEW mart.revenue_bridge IS
  'Gross potential rent -> vacancy -> loss to lease -> subsidy -> concessions -> '
  'ancillary -> billed charges, current section only. The steps sum exactly to '
  'billed_charges; exclusion_reason marks the books where the source prints no '
  'charge data and the bridge would be an artifact of that absence.';

-- ---------------------------------------------------------------------------
-- F5 -- rent PSF and unit-type pricing outliers
-- ---------------------------------------------------------------------------

-- Every occupied or notice unit against the median asking rent of its own unit
-- type in its own book.
--
-- percentile_disc, not percentile_cont: disc returns an actually observed rent
-- and preserves numeric, where cont interpolates and returns double precision,
-- which then cannot be round()ed with a scale. An observed comparable is also
-- the more defensible number to put in front of an asset manager.
CREATE OR REPLACE VIEW mart.unit_rent_outlier AS
WITH const AS (SELECT 3::int AS min_peer_units),
base AS (
  SELECT l.snapshot_id, l.property_id, l.unit_id, l.unit_type_id, l.lease_id,
         u.unit_code, ut.unit_type_code, l.unit_sqft, l.market_rent,
         l.occupancy_status::text AS occupancy_status, l.lease_expiration,
         -- NULL, not 0, when the lease prints no charge lines at all: six books
         -- carry none, and a 0 there would render as "the resident pays nothing"
         -- against a real market rent. A lease that does carry lines but none in
         -- the rent category is a genuine 0 and keeps it.
         CASE WHEN EXISTS (SELECT 1 FROM core.lease_charge lc WHERE lc.lease_id = l.lease_id)
              THEN coalesce((SELECT sum(lc.amount)
                             FROM core.lease_charge lc
                             JOIN core.charge_code cc ON cc.charge_code = lc.charge_code
                             WHERE lc.lease_id = l.lease_id AND cc.category = 'rent'), 0)
         END                                                          AS contract_rent
  FROM core.lease l
  JOIN core.unit u       ON u.unit_id = l.unit_id
  JOIN core.unit_type ut ON ut.unit_type_id = l.unit_type_id
  WHERE l.section = 'current'
    AND l.occupancy_status IN ('occupied','notice')
    AND l.unit_sqft   > 0      -- 26 rows print 0 sq ft; PSF has no meaning for them
    AND l.market_rent > 0
),
peers AS (
  SELECT snapshot_id, property_id, unit_type_id,
         count(*) AS peer_units,
         percentile_disc(0.5) WITHIN GROUP (ORDER BY market_rent) AS median_market_rent,
         percentile_disc(0.5) WITHIN GROUP (ORDER BY round(market_rent / unit_sqft, 4))
           AS median_rent_psf
  FROM base
  GROUP BY 1, 2, 3
)
SELECT b.snapshot_id, b.property_id, p.property_code::text AS property_code,
       b.unit_id, b.unit_code, b.unit_type_code, b.unit_sqft, b.occupancy_status,
       b.lease_expiration, b.lease_id,
       b.market_rent, b.contract_rent,
       round(b.market_rent / b.unit_sqft, 4)                          AS rent_psf,
       pe.peer_units, pe.median_market_rent, pe.median_rent_psf,
       b.market_rent - pe.median_market_rent                          AS market_vs_median,
       round(100.0 * (b.market_rent - pe.median_market_rent)
             / nullif(pe.median_market_rent, 0), 2)                   AS pct_vs_median,
       b.contract_rent - b.market_rent                                AS contract_vs_market
FROM base b
JOIN peers pe USING (snapshot_id, property_id, unit_type_id)
JOIN core.property p ON p.property_id = b.property_id
CROSS JOIN const c
WHERE pe.peer_units >= c.min_peer_units;

COMMENT ON VIEW mart.unit_rent_outlier IS
  'Each occupied/notice unit against the median market rent of its own unit type '
  'in its own book, for types with at least 3 units. contract_vs_market is what '
  'the resident actually pays against the asking rent, NULL where the source '
  'prints no charge lines for the lease; market_vs_median is how the asking '
  'rent itself is positioned.';

-- ---------------------------------------------------------------------------
-- F6 -- expiration concentration
-- ---------------------------------------------------------------------------

-- Which months carry too much of a book's renewal risk.
--
-- Two constants, both measured off the corpus (PLAN6 section F6.1): books average
-- 24.3 distinct expiry months, so an even spread is about 4.1% per month and the
-- 15% line sits at roughly 3.6x even, just above the p95 of 13.3%. The 50-lease
-- floor exists because a 6-of-18 month is a percentage, not a problem.
--
-- Reads mart.expiration_schedule, which is a materialized view, so this is only
-- as fresh as the last refresh_marts(). Every load refreshes.
CREATE OR REPLACE VIEW mart.expiration_concentration AS
WITH const AS (
  SELECT 0.15::numeric AS concentration_threshold,
         50::int       AS min_dated_leases
),
dated AS (
  SELECT l.snapshot_id, l.property_id, count(*) AS dated_leases
  FROM core.lease l
  WHERE l.occupancy_status IN ('occupied','notice') AND l.lease_expiration IS NOT NULL
  GROUP BY 1, 2
)
SELECT e.snapshot_id, e.property_id, p.property_code::text AS property_code, p.property_name,
       e.expiry_month, e.expiring_leases, e.charges_at_risk, e.holdover_mtm,
       d.dated_leases, c.concentration_threshold, c.min_dated_leases,
       round(e.expiring_leases::numeric / nullif(d.dated_leases, 0), 4) AS share_of_book,
       (d.dated_leases >= c.min_dated_leases
        AND e.expiring_leases::numeric / nullif(d.dated_leases, 0)
              >= c.concentration_threshold)                             AS concentrated,
       greatest(0, e.expiring_leases
                   - floor(d.dated_leases * c.concentration_threshold)::int)
                                                                        AS leases_to_shift
FROM mart.expiration_schedule e
JOIN core.property p ON p.property_id = e.property_id
JOIN dated d ON d.snapshot_id = e.snapshot_id AND d.property_id = e.property_id
CROSS JOIN const c;

COMMENT ON VIEW mart.expiration_concentration IS
  'Share of a book''s dated leases expiring in each month, with the months that '
  'exceed the concentration threshold flagged. leases_to_shift is how many '
  'renewals would have to move to bring the peak back to the line.';
