-- Profitability matrix. The source workbooks carry no expense data, so
-- "profitability" here is revenue capture (economic occupancy): billed charges
-- as a share of gross potential rent. It is a revenue-efficiency proxy and is
-- never to be presented as NOI.
--
-- A plain VIEW on purpose: refresh_marts() refreshes matviews in alphabetical
-- order, and 'property_profitability' < 'property_snapshot_kpi', so a matview
-- here would be refreshed against stale input.

-- The quadrant rule, extracted so it is directly testable. Tie rule (PLAN2 2.2):
-- a value EQUAL to a threshold counts as the HIGH side, hence >= on both axes.
-- Nothing on snapshot 1 lands exactly on a line (144r is 95.02), but the rule
-- must be deterministic, and a test that re-types the CASE tests only itself.
CREATE OR REPLACE FUNCTION mart.quadrant(
  capture numeric, occ numeric, capture_th numeric, occ_th numeric
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN capture IS NULL OR occ IS NULL             THEN NULL
    WHEN capture >= capture_th AND occ >= occ_th    THEN 'performing'
    WHEN capture <  capture_th AND occ >= occ_th    THEN 'leaking'
    WHEN capture >= capture_th AND occ <  occ_th    THEN 'vacancy_led'
    ELSE 'distressed'
  END
$$;

COMMENT ON FUNCTION mart.quadrant(numeric, numeric, numeric, numeric) IS
  'Quadrant rule for mart.property_profitability. Equal-to-threshold counts as '
  'the high side. Returns NULL when either axis is NULL.';

CREATE OR REPLACE VIEW mart.property_profitability AS
WITH const AS (
  SELECT 95.0::numeric AS capture_threshold,
         95.0::numeric AS occupancy_threshold,
         0.5::numeric  AS coverage_threshold
),
coverage AS (
  -- Six books (175r, 176r, 183a, 183r, 184r, 185r) have no lease-charge lines at
  -- all. Their lease_charges of 0 is an artifact of the source workbook, not a
  -- fact about the asset, so they must not be plotted at 0% capture.
  SELECT l.property_id,
         l.snapshot_id,
         count(*) FILTER (WHERE l.section = 'current') AS current_leases,
         count(*) FILTER (
           WHERE l.section = 'current'
             AND EXISTS (SELECT 1 FROM core.lease_charge lc WHERE lc.lease_id = l.lease_id)
         ) AS leases_with_charges
  FROM core.lease l
  GROUP BY l.property_id, l.snapshot_id
),
ltl AS (
  SELECT snapshot_id, property_id,
         sum(loss_to_lease) AS loss_to_lease,
         sum(concessions)   AS concessions
  FROM mart.loss_to_lease
  GROUP BY 1, 2
),
mix AS (
  SELECT snapshot_id, property_id,
         sum(amount) FILTER (
           WHERE category NOT IN ('rent', 'concession', 'subsidy')
         ) AS ancillary_charges
  FROM mart.charge_mix
  GROUP BY 1, 2
),
delinquency AS (
  SELECT snapshot_id, property_id,
         count(*) FILTER (WHERE balance > 0)      AS units_owing,
         sum(balance) FILTER (WHERE balance > 0)  AS balance_owed
  FROM core.lease
  WHERE section = 'current'
  GROUP BY 1, 2
),
base AS (
  -- FROM core.property x core.snapshot, LEFT JOIN the KPI view -- not FROM the
  -- KPI view itself. property_snapshot_kpi has no row at all for the three
  -- zero-unit books (134land, 183c, altapm), so building base FROM it would
  -- silently drop them instead of classifying them 'no_units'. Same fix as the
  -- property_detail 404 (PLAN2 section 3.2): 404/absence-by-join on a book with
  -- zero leases is a bug, not a feature.
  SELECT
    sn.snapshot_id, sn.as_of_date, p.property_id, p.property_code, p.property_name,
    p.book_type, p.asset_key,
    coalesce(k.units, 0) AS units, coalesce(k.occupied_units, 0) AS occupied_units,
    coalesce(k.notice_units, 0) AS notice_units, coalesce(k.vacant_units, 0) AS vacant_units,
    coalesce(k.non_revenue_units, 0) AS non_revenue_units,
    k.pct_occupied, k.market_rent, k.lease_charges, k.square_feet, k.balance,
    c.capture_threshold, c.occupancy_threshold,
    round(cov.leases_with_charges::numeric / nullif(cov.current_leases, 0), 3)
      AS charge_coverage,
    coalesce(ltl.loss_to_lease, 0)     AS loss_to_lease,
    coalesce(ltl.concessions, 0)       AS concessions,
    coalesce(mix.ancillary_charges, 0) AS ancillary_charges,
    coalesce(d.units_owing, 0)         AS units_owing,
    coalesce(d.balance_owed, 0)        AS balance_owed
  FROM core.snapshot sn
  CROSS JOIN core.property p
  CROSS JOIN const c
  LEFT JOIN mart.property_snapshot_kpi k
    ON k.property_id = p.property_id AND k.snapshot_id = sn.snapshot_id
  LEFT JOIN coverage cov
    ON cov.property_id = p.property_id AND cov.snapshot_id = sn.snapshot_id
  LEFT JOIN ltl ON ltl.property_id = p.property_id AND ltl.snapshot_id = sn.snapshot_id
  LEFT JOIN mix ON mix.property_id = p.property_id AND mix.snapshot_id = sn.snapshot_id
  LEFT JOIN delinquency d
    ON d.property_id = p.property_id AND d.snapshot_id = sn.snapshot_id
),
scored AS (
  SELECT b.*,
    CASE WHEN b.market_rent > 0
         THEN round(100.0 * b.lease_charges / b.market_rent, 2) END AS revenue_capture_pct,
    CASE
      WHEN b.units = 0                     THEN 'no_units'
      WHEN b.market_rent IS NULL
        OR b.market_rent <= 0              THEN 'no_market_rent'
      WHEN coalesce(b.charge_coverage, 0)
             < (SELECT coverage_threshold FROM const) THEN 'no_charge_data'
    END AS exclusion_reason
  FROM base b
)
SELECT
  s.*,
  (s.exclusion_reason IS NULL) AS plottable,
  CASE WHEN s.exclusion_reason IS NULL THEN
    mart.quadrant(s.revenue_capture_pct, s.pct_occupied,
                  s.capture_threshold, s.occupancy_threshold)
  END AS quadrant,
  -- Movement deltas: exactly what it takes to cross each line. Pure arithmetic,
  -- computed here so neither the LLM nor the browser ever derives them.
  CASE WHEN s.exclusion_reason IS NULL THEN
    greatest(0, round(s.market_rent * s.capture_threshold / 100.0 - s.lease_charges, 2))
  END AS charges_to_threshold,
  CASE WHEN s.exclusion_reason IS NULL THEN
    greatest(0, ceil(s.units * s.occupancy_threshold / 100.0)::int - s.occupied_units)
  END AS units_to_threshold
FROM scored s;

COMMENT ON VIEW mart.property_profitability IS
  'Revenue capture (economic occupancy) x physical occupancy, with quadrant, '
  'movement deltas and an explicit exclusion reason for every book that cannot '
  'be scored. No expense data exists in the source, so this is a revenue '
  'efficiency proxy and never NOI.';
