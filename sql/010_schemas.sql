CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

COMMENT ON SCHEMA raw  IS 'Ingest audit trail: runs, source files, load issues.';
COMMENT ON SCHEMA core IS 'Dimensions and facts parsed from the source workbooks.';
COMMENT ON SCHEMA mart IS 'Read models for the presentation layer.';
