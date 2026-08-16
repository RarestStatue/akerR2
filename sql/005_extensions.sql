-- Also present in docker/initdb/00_extensions.sql, which only runs when the
-- data volume is created. Repeated here so `aker-etl init-db` works against any
-- database, including one that already existed.
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gist;
