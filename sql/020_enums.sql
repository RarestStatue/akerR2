-- CREATE TYPE has no IF NOT EXISTS, and `init-db` must be re-runnable, so each
-- type is guarded. Idempotency here is what lets init-db be safe to repeat.
DO $$
BEGIN
  CREATE TYPE core.dataset_kind AS ENUM ('rent_roll', 'unit_availability');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  CREATE TYPE core.lease_section AS ENUM ('current', 'future');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  CREATE TYPE core.occupancy_status AS ENUM ('occupied','notice','vacant','model','down','future');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  CREATE TYPE core.book_type AS ENUM ('residential','affordable','commercial','land','other');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  CREATE TYPE core.charge_category AS ENUM (
    'rent','subsidy','concession','parking','garage','storage','pet',
    'utility','amenity','service','tax','cam','insurance','fee','other');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  CREATE TYPE raw.run_status AS ENUM ('running','succeeded','failed','partial');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  CREATE TYPE raw.issue_severity AS ENUM ('info','warning','error');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- AI-insight layer (PLAN.md 6)
DO $$
BEGIN
  CREATE TYPE core.insight_scope AS ENUM ('portfolio','asset','property');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  CREATE TYPE core.insight_category AS ENUM (
    'occupancy','revenue','concession','expiration','delinquency',
    'unit_mix','data_quality','trend');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  CREATE TYPE core.insight_priority AS ENUM ('low','medium','high');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Quadrant-movement advice for the profitability matrix (PLAN2 section 2.7).
-- ADD VALUE IF NOT EXISTS is idempotent, and on PostgreSQL 12+ it is legal
-- inside the implicit transaction that init_db runs each file in, provided the
-- new value is not *used* in the same transaction. It is not: only the Python
-- insert path uses it, in a later transaction.
ALTER TYPE core.insight_category ADD VALUE IF NOT EXISTS 'positioning';
