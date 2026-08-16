CREATE TABLE IF NOT EXISTS raw.ingest_run (
  run_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  status        raw.run_status NOT NULL DEFAULT 'running',
  data_dir      text        NOT NULL,
  tool_version  text        NOT NULL,
  files_seen    int         NOT NULL DEFAULT 0,
  files_loaded  int         NOT NULL DEFAULT 0,
  files_skipped int         NOT NULL DEFAULT 0,
  files_failed  int         NOT NULL DEFAULT 0,
  rows_loaded   bigint      NOT NULL DEFAULT 0,
  notes         text
);

CREATE TABLE IF NOT EXISTS raw.source_file (
  file_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id        bigint NOT NULL REFERENCES raw.ingest_run(run_id) ON DELETE CASCADE,
  dataset       core.dataset_kind NOT NULL,
  file_name     text   NOT NULL,
  file_path     text   NOT NULL,
  sha256        char(64) NOT NULL,
  byte_size     bigint NOT NULL,
  modified_at   timestamptz NOT NULL,
  sheet_rows    int    NOT NULL,
  parsed_rows   int    NOT NULL DEFAULT 0,
  loaded_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT source_file_uq UNIQUE (dataset, file_name, sha256)
);
CREATE INDEX IF NOT EXISTS source_file_sha_ix ON raw.source_file (sha256);

-- Every anomaly that does not stop the load lands here. Nothing is silently dropped.
CREATE TABLE IF NOT EXISTS raw.load_issue (
  issue_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id     bigint NOT NULL REFERENCES raw.ingest_run(run_id) ON DELETE CASCADE,
  file_id    bigint REFERENCES raw.source_file(file_id) ON DELETE CASCADE,
  severity   raw.issue_severity NOT NULL,
  rule       text   NOT NULL,          -- e.g. 'block_total_mismatch'
  sheet_row  int,
  detail     jsonb  NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS load_issue_run_sev_ix ON raw.load_issue (run_id, severity);
CREATE INDEX IF NOT EXISTS load_issue_rule_ix    ON raw.load_issue (rule);
