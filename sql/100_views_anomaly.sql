-- Deterministic outlier detection: each book's metric against the distribution of
-- the same metric across the portfolio, for the same snapshot. No model involved,
-- so this renders with Ollama down and needs no evidence gate -- the numbers are
-- the input, not an assertion about the input.
--
-- Rates and per-unit figures only. Books run from 19 to 775 units, so an
-- unnormalised total would rank by size and call the largest book an anomaly
-- every time.
CREATE OR REPLACE VIEW mart.property_anomaly AS
WITH const AS (
  SELECT 1.75::numeric AS z_threshold,   -- 22 books: |z| cannot exceed about 4.5
         3::int        AS min_books,     -- a z-score over two books is noise
         2.00::numeric AS medium_z,
         2.50::numeric AS high_z
),
metric_meta(metric, label, unit, worse_when) AS (VALUES
  ('pct_occupied'::text,     'Physical occupancy'::text,       'pct'::text,   'low'::text),
  ('revenue_capture_pct',    'Revenue capture',                'pct',         'low'),
  ('balance_per_unit',       'Resident balance per unit',      'money',       'low'),
  ('concession_per_unit',    'Concessions per unit',           'money',       'low'),
  ('loss_to_lease_per_unit', 'Loss to lease per unit',         'money',       'high'),
  ('notice_rate',            'Units on notice',                'pct',         'high'),
  ('vacancy_rate',           'Vacant units',                   'pct',         'high')
),
base AS (
  SELECT pp.snapshot_id, pp.property_id, pp.property_code::text AS property_code,
         pp.property_name, pp.units,
         pp.pct_occupied,
         -- The three charge-derived metrics are only meaningful where charge data
         -- exists. NULL them out elsewhere and the unpivot below drops those rows
         -- per metric, rather than dropping the whole book from every metric.
         CASE WHEN pp.plottable THEN pp.revenue_capture_pct END AS revenue_capture_pct,
         CASE WHEN pp.plottable
              THEN round(pp.concessions   / nullif(pp.units, 0), 2) END AS concession_per_unit,
         CASE WHEN pp.plottable
              THEN round(pp.loss_to_lease / nullif(pp.units, 0), 2) END AS loss_to_lease_per_unit,
         round(pp.balance / nullif(pp.units, 0), 2)                     AS balance_per_unit,
         round(100.0 * pp.notice_units / nullif(pp.units, 0), 2)        AS notice_rate,
         round(100.0 * pp.vacant_units / nullif(pp.units, 0), 2)        AS vacancy_rate
  FROM mart.property_profitability pp
  WHERE pp.units > 0
),
long AS (
  SELECT b.snapshot_id, b.property_id, b.property_code, b.property_name, b.units,
         v.metric, v.value
  FROM base b
  CROSS JOIN LATERAL (VALUES
    ('pct_occupied'::text,      b.pct_occupied),
    ('revenue_capture_pct',     b.revenue_capture_pct),
    ('balance_per_unit',        b.balance_per_unit),
    ('concession_per_unit',     b.concession_per_unit),
    ('loss_to_lease_per_unit',  b.loss_to_lease_per_unit),
    ('notice_rate',             b.notice_rate),
    ('vacancy_rate',            b.vacancy_rate)
  ) AS v(metric, value)
  WHERE v.value IS NOT NULL
),
scored AS (
  SELECT l.*,
         count(*)                   OVER w AS peer_books,
         round(avg(l.value)         OVER w, 4) AS peer_mean,
         round(stddev_samp(l.value) OVER w, 4) AS peer_sd,
         round((l.value - avg(l.value) OVER w)
               / nullif(stddev_samp(l.value) OVER w, 0), 2) AS z
  FROM long l
  WINDOW w AS (PARTITION BY l.snapshot_id, l.metric)
)
SELECT s.snapshot_id, s.property_id, s.property_code, s.property_name, s.units,
       s.metric, m.label, m.unit, m.worse_when,
       s.value, s.peer_mean, s.peer_sd, s.peer_books, s.z,
       ((m.worse_when = 'low'  AND s.z < 0)
     OR (m.worse_when = 'high' AND s.z > 0))                     AS adverse,
       CASE WHEN abs(s.z) >= c.high_z   THEN 'high'
            WHEN abs(s.z) >= c.medium_z THEN 'medium'
            ELSE 'low' END                                       AS priority
FROM scored s
JOIN metric_meta m ON m.metric = s.metric
CROSS JOIN const c
WHERE s.peer_books >= c.min_books
  AND s.z IS NOT NULL
  AND abs(s.z) >= c.z_threshold;

COMMENT ON VIEW mart.property_anomaly IS
  'Per-snapshot z-score of each book against the portfolio, per metric. Rates and '
  'per-unit figures only. `adverse` says whether the outlier is the bad direction; '
  'priority bands |z| at 2.0 and 2.5.';
