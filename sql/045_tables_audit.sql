-- Who changed a charge code's classification, when, and from what. A category is
-- an interpretation, not a fact from the workbooks: sql/060 seeds five codes as
-- inferred and label_verified=false says so out loud. This table is what turns
-- "confirm with the client" from a comment into a record.
CREATE TABLE IF NOT EXISTS core.charge_code_audit (
  audit_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  charge_code     text NOT NULL REFERENCES core.charge_code(charge_code),
  old_category    core.charge_category NOT NULL,
  new_category    core.charge_category NOT NULL,
  old_description text,
  new_description text,
  old_verified    boolean NOT NULL,
  new_verified    boolean NOT NULL,
  note            text,
  changed_by      text NOT NULL,
  changed_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT charge_code_audit_changed_something CHECK (
       old_category    IS DISTINCT FROM new_category
    OR old_description IS DISTINCT FROM new_description
    OR old_verified    IS DISTINCT FROM new_verified)
);
CREATE INDEX IF NOT EXISTS charge_code_audit_code_ix
  ON core.charge_code_audit (charge_code, changed_at DESC);
