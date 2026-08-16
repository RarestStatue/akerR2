-- Insights are generated once per snapshot and stored, never generated per page
-- view. That keeps the dashboard fast and the output reproducible -- and with a
-- local model it also keeps a page load from waiting on inference.
CREATE TABLE IF NOT EXISTS core.insight (
  insight_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  snapshot_id   int      NOT NULL REFERENCES core.snapshot(snapshot_id) ON DELETE CASCADE,
  scope         core.insight_scope    NOT NULL,
  property_id   smallint REFERENCES core.property(property_id),   -- NULL unless scope='property'
  asset_key     text,                                             -- NULL unless scope='asset'
  category      core.insight_category NOT NULL,
  priority      core.insight_priority NOT NULL,
  headline      text NOT NULL CHECK (length(headline) BETWEEN 8 AND 120),
  detail        text NOT NULL,
  evidence      jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{metric, value, comparison}, ...]
  model         text NOT NULL,                       -- local model tag, e.g. 'qwen3.5:4b'
  prompt_sha256 char(64) NOT NULL,                   -- hash of the serialized context payload
  generated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT insight_scope_target CHECK (
       (scope = 'portfolio' AND property_id IS NULL AND asset_key IS NULL)
    OR (scope = 'asset'     AND property_id IS NULL AND asset_key IS NOT NULL)
    OR (scope = 'property'  AND property_id IS NOT NULL AND asset_key IS NULL))
);
CREATE INDEX IF NOT EXISTS insight_snap_ix     ON core.insight (snapshot_id, scope, priority);
CREATE INDEX IF NOT EXISTS insight_property_ix ON core.insight (property_id) WHERE property_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS insight_evidence_ix ON core.insight USING gin (evidence jsonb_path_ops);

-- One generation attempt = one row. Failures are visible, not silent.
CREATE TABLE IF NOT EXISTS core.insight_run (
  insight_run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  snapshot_id    int  NOT NULL REFERENCES core.snapshot(snapshot_id) ON DELETE CASCADE,
  model          text NOT NULL,
  prompt_sha256  char(64) NOT NULL,
  input_tokens   int,
  output_tokens  int,
  status         text NOT NULL CHECK (status IN ('succeeded','failed','refused')),
  error          text,
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz
);
CREATE INDEX IF NOT EXISTS insight_run_snap_ix ON core.insight_run (snapshot_id, started_at DESC);
