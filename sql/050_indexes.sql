CREATE INDEX IF NOT EXISTS lease_snap_prop_ix   ON core.lease (snapshot_id, property_id);
CREATE INDEX IF NOT EXISTS lease_snap_status_ix ON core.lease (snapshot_id, occupancy_status);
CREATE INDEX IF NOT EXISTS lease_unit_ix        ON core.lease (unit_id);
CREATE INDEX IF NOT EXISTS lease_resident_ix    ON core.lease (resident_id) WHERE resident_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS lease_expiry_ix      ON core.lease (lease_expiration)
                                                WHERE occupancy_status IN ('occupied','notice');
CREATE INDEX IF NOT EXISTS lease_moveout_ix     ON core.lease (move_out) WHERE move_out IS NOT NULL;
CREATE INDEX IF NOT EXISTS lease_movein_ix      ON core.lease (move_in)  WHERE move_in  IS NOT NULL;
CREATE INDEX IF NOT EXISTS lease_balance_ix     ON core.lease (snapshot_id, balance) WHERE balance <> 0;
CREATE INDEX IF NOT EXISTS lease_type_ix        ON core.lease (unit_type_id);
-- Supports the FK to raw.source_file and the run_id lookup that /api/quality and
-- the insight payload both run per request (dashboard/app.py, insights/context.py).
CREATE INDEX IF NOT EXISTS lease_file_ix ON core.lease (file_id);

-- covering: charge-mix aggregations never touch the heap
CREATE INDEX IF NOT EXISTS lease_charge_code_ix  ON core.lease_charge (charge_code) INCLUDE (amount, lease_id);
CREATE INDEX IF NOT EXISTS lease_charge_lease_ix ON core.lease_charge (lease_id) INCLUDE (charge_code, amount);

CREATE INDEX IF NOT EXISTS unit_prop_ix      ON core.unit (property_id);
CREATE INDEX IF NOT EXISTS unit_code_trgm_ix ON core.unit USING gin (unit_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS property_asset_ix ON core.property (asset_key);
