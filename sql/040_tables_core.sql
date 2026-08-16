-- ---------- snapshot ----------
-- Files are point-in-time extracts ("As Of = 02/25/2026"). Everything factual hangs
-- off a snapshot so a second month's drop loads alongside, not over, the first.
CREATE TABLE IF NOT EXISTS core.snapshot (
  snapshot_id  int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  as_of_date   date NOT NULL,
  report_month date,                        -- first day of "Month Year"; NULL for availability-only
  created_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT snapshot_uq UNIQUE (as_of_date)
);

-- ---------- dimensions ----------
CREATE TABLE IF NOT EXISTS core.property (
  property_id   smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  property_code citext NOT NULL UNIQUE,     -- '115r', '134land', 'altapm'
  property_name text   NOT NULL,
  asset_key     text   NOT NULL,            -- leading digits of the code: '115','134','altapm'
  book_type     core.book_type NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN core.property.asset_key IS
  'Groups the books of one physical asset: 134c/134land/134r -> "134".';

CREATE TABLE IF NOT EXISTS core.unit_type (
  unit_type_id   int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  property_id    smallint NOT NULL REFERENCES core.property(property_id),
  unit_type_code text NOT NULL,
  CONSTRAINT unit_type_uq UNIQUE (property_id, unit_type_code)
);

CREATE TABLE IF NOT EXISTS core.unit (
  unit_id      int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  property_id  smallint NOT NULL REFERENCES core.property(property_id),
  unit_code    text NOT NULL,
  unit_type_id int REFERENCES core.unit_type(unit_type_id),
  unit_sqft    int CHECK (unit_sqft >= 0),
  CONSTRAINT unit_uq UNIQUE (property_id, unit_code)
);

CREATE TABLE IF NOT EXISTS core.resident (
  resident_id   text PRIMARY KEY,           -- source id, verified globally unique
  display_name  text NOT NULL,              -- pre-anonymized 'Resident N'
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT resident_id_fmt CHECK (resident_id ~ '^t[A-Za-z0-9]+$')
);

CREATE TABLE IF NOT EXISTS core.charge_code (
  charge_code    text PRIMARY KEY,
  category       core.charge_category NOT NULL,
  description    text,
  is_concession  boolean NOT NULL GENERATED ALWAYS AS (charge_code LIKE 'CON%') STORED,
  label_verified boolean NOT NULL DEFAULT true   -- false = description inferred, confirm with client
);

-- ---------- facts ----------
CREATE TABLE IF NOT EXISTS core.lease (
  lease_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  snapshot_id       int      NOT NULL REFERENCES core.snapshot(snapshot_id) ON DELETE CASCADE,
  property_id       smallint NOT NULL REFERENCES core.property(property_id),
  unit_id           int      NOT NULL REFERENCES core.unit(unit_id),
  unit_type_id      int      REFERENCES core.unit_type(unit_type_id),
  resident_id       text     REFERENCES core.resident(resident_id),  -- NULL for vacant/model/down
  section           core.lease_section    NOT NULL,
  occupancy_status  core.occupancy_status NOT NULL,
  unit_sqft         int            CHECK (unit_sqft >= 0),
  market_rent       numeric(12,2)  NOT NULL DEFAULT 0,
  resident_deposit  numeric(12,2)  NOT NULL DEFAULT 0,
  other_deposit     numeric(12,2)  NOT NULL DEFAULT 0,
  balance           numeric(12,2)  NOT NULL DEFAULT 0,
  charges_total     numeric(12,2)  NOT NULL DEFAULT 0,   -- the block's "Total" row, as printed
  move_in           date,
  lease_expiration  date,
  move_out          date,
  file_id           bigint NOT NULL REFERENCES raw.source_file(file_id),
  sheet_row         int    NOT NULL,
  CONSTRAINT lease_uq UNIQUE (snapshot_id, unit_id, section),
  CONSTRAINT lease_resident_presence CHECK (
    (occupancy_status IN ('vacant','model','down') AND resident_id IS NULL)
    OR (occupancy_status IN ('occupied','notice','future') AND resident_id IS NOT NULL)),
  -- Implication, not biconditional. A notice row must carry a move-out date,
  -- but the reverse is false in the source: a future-section row or a
  -- VACANT/MODEL/DOWN sentinel can legitimately print one, and derive_status()
  -- resolves those to 'future'/'vacant'/'model'/'down' by design. The old
  -- biconditional turned one such cell into a COPY failure that rolled back the
  -- whole ingest run; the parser records it as a warning and keeps the row
  -- (rent_roll.py sentinel_field_violation), which is the intended behaviour.
  CONSTRAINT lease_notice_implies_moveout CHECK (
    occupancy_status <> 'notice' OR move_out IS NOT NULL),
  CONSTRAINT lease_date_order CHECK (
    move_out IS NULL OR move_in IS NULL OR move_out >= move_in)
);

CREATE TABLE IF NOT EXISTS core.lease_charge (
  lease_charge_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lease_id    bigint NOT NULL REFERENCES core.lease(lease_id) ON DELETE CASCADE,
  line_no     smallint NOT NULL,            -- 1-based order as printed; codes CAN repeat
  charge_code text   NOT NULL REFERENCES core.charge_code(charge_code),
  amount      numeric(12,2) NOT NULL,
  sheet_row   int    NOT NULL,
  CONSTRAINT lease_charge_uq UNIQUE (lease_id, line_no)
);

-- Report-provided rollups, kept verbatim so dashboards can be reconciled against the
-- source Excel the asset team already trusts.
CREATE TABLE IF NOT EXISTS core.rent_roll_summary_group (
  snapshot_id        int      NOT NULL REFERENCES core.snapshot(snapshot_id) ON DELETE CASCADE,
  property_id        smallint NOT NULL REFERENCES core.property(property_id),
  group_label        text     NOT NULL,
  square_footage     numeric(14,2),
  market_rent        numeric(14,2),
  lease_charges      numeric(14,2),
  security_deposit   numeric(14,2),
  other_deposits     numeric(14,2),
  unit_count         int,
  pct_unit_occupancy numeric(7,4),
  pct_sqft_occupied  numeric(7,4),
  balance            numeric(14,2),
  file_id            bigint NOT NULL REFERENCES raw.source_file(file_id),
  PRIMARY KEY (snapshot_id, property_id, group_label)
);

CREATE TABLE IF NOT EXISTS core.charge_summary (
  snapshot_id int      NOT NULL REFERENCES core.snapshot(snapshot_id) ON DELETE CASCADE,
  property_id smallint NOT NULL REFERENCES core.property(property_id),
  charge_code text     NOT NULL REFERENCES core.charge_code(charge_code),
  amount      numeric(14,2) NOT NULL,
  file_id     bigint NOT NULL REFERENCES raw.source_file(file_id),
  PRIMARY KEY (snapshot_id, property_id, charge_code)
);

CREATE TABLE IF NOT EXISTS core.unit_availability (
  snapshot_id        int      NOT NULL REFERENCES core.snapshot(snapshot_id) ON DELETE CASCADE,
  property_id        smallint NOT NULL REFERENCES core.property(property_id),
  avg_sqft           int   NOT NULL,
  avg_rent           numeric(12,2) NOT NULL,
  units              int   NOT NULL CHECK (units >= 0),
  occupied_no_notice int   NOT NULL,
  vacant_rented      int   NOT NULL,
  vacant_unrented    int   NOT NULL,
  notice_rented      int   NOT NULL,
  notice_unrented    int   NOT NULL,
  available          int   NOT NULL,
  model              int   NOT NULL,
  down               int   NOT NULL,
  admin              int   NOT NULL,
  pct_occ            numeric(7,4) NOT NULL,
  pct_occ_w_nonrev   numeric(7,4) NOT NULL,
  pct_leased         numeric(7,4) NOT NULL,
  pct_trend          numeric(7,4) NOT NULL,
  file_id            bigint NOT NULL REFERENCES raw.source_file(file_id),
  PRIMARY KEY (snapshot_id, property_id)
);

-- Deliberate omissions, so nobody "fixes" them later:
--  * No CHECK (charges_total = sum of charges) -- cross-row invariants belong in
--    validate.py, not a table constraint.
--  * No FK from lease.resident_id for sentinels -- VACANT/MODEL/DOWN are statuses,
--    not people, so resident_id is NULL and occupancy_status carries the meaning.
--  * unit.unit_sqft (latest observed) and lease.unit_sqft (as printed at the
--    snapshot) are both kept; they can legitimately drift between snapshots.

-- Migration for databases created before the constraint was weakened (BUG.md 1).
-- DROP IF EXISTS on both names, then ADD, so this is idempotent and applies
-- cleanly whether the table was just created above or already existed.
ALTER TABLE core.lease DROP CONSTRAINT IF EXISTS lease_notice_has_moveout;
ALTER TABLE core.lease DROP CONSTRAINT IF EXISTS lease_notice_implies_moveout;
ALTER TABLE core.lease ADD CONSTRAINT lease_notice_implies_moveout
  CHECK (occupancy_status <> 'notice' OR move_out IS NOT NULL);
