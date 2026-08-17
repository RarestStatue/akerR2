-- The reset button for an ALTER-free schema. Run by `aker-etl init-db --drop`,
-- never by a plain `init-db`: db.migration_files() globs sql/*.sql, which is
-- non-recursive, so nothing in this sub-directory is ever picked up by accident.
--
-- CASCADE takes the tables, the enums, the materialized views and mart.quadrant
-- with the schemas, which is the point: every object this project creates lives
-- in one of these three. The extensions in sql/005_extensions.sql live in
-- `public` and survive, so `init-db --drop` does not need superuser rights it
-- did not already need.
--
-- mart first, then core, then raw: CASCADE would sort it out either way, but
-- dropping in dependency order keeps the NOTICE output short enough to read.
DROP SCHEMA IF EXISTS mart CASCADE;
DROP SCHEMA IF EXISTS core CASCADE;
DROP SCHEMA IF EXISTS raw  CASCADE;
