# Aker Round 2 - Rent Roll Database, ETL and Dashboard

Loads 50 Excel workbooks (25 rent rolls + 25 unit availability reports) into
PostgreSQL and serves a dashboard over them, with an optional insight panel
written by a local LLM.

Everything below has been run end to end on this machine. A full load is
**50 files in ~1.1 seconds**, and every count reconciles against the source
workbooks exactly.

| | |
|---|---|
| Properties | 25 books (22 with units, 3 empty) |
| Lease blocks | 4,106 (4,013 current + 93 future) |
| Charge lines | 9,177 across 32 charge codes |
| Errors on load | 0 |

---

## 1. Prerequisites

* Docker (with Compose)
* Python 3.12+
* Optional, for the insight panel only: [Ollama](https://ollama.com)

Nothing else is required. The dashboard and the ETL do not need Ollama.

---

## 2. Run it, step by step

### Step 1 - configure

```bash
cd /home/max/vsCODE/Work/akerR2
cp .env.example .env
$EDITOR .env          # set POSTGRES_PASSWORD to anything you like
```

`POSTGRES_PORT` defaults to **5434** because 5432 and 5433 were already taken on
this machine. Change it if you prefer another port.

### Step 2 - start PostgreSQL

```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d db
```

Wait for it to report healthy (about 10 seconds):

```bash
docker inspect -f '{{.State.Health.Status}}' aker_pg     # -> healthy
```

### Step 3 - install the Python package

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Step 4 - create the schema

```bash
aker-etl init-db
```

Applies `sql/*.sql` in filename order. Every file is idempotent, so this is safe
to repeat.

### Step 5 - load the data

```bash
aker-etl load
```

Expected output: 50 files, 0 failed, 4,106 leases, 9,177 charges, 150 summary
groups, 117 charge summaries, 25 availability rows, in about a second.

The exit code is `0` when there are no errors, `2` when the load recorded error
issues, `3` on a structural failure (wrong sheet, changed header).

### Step 6 - check it

```bash
aker-etl status        # golden-number check: all 12 metrics should read "ok"
aker-etl validate      # issue counts by rule and severity
```

### Step 7 - open the dashboard

```bash
aker-etl serve                       # http://127.0.0.1:8000
```

Six tabs: **Portfolio** (KPIs, occupancy, expiration ladder, profitability
ranking), **Properties** (click any row for unit types, charge mix, expirations
and the report's own rollups), **Matrix** (revenue capture vs. occupancy
four-quadrant scatter; see below), **Units** (filter and search 4,106 lease
blocks; click a row for its charge lines), **Insights**, **Data quality**.

**The Matrix tab.** The source workbooks carry no expense data, so true
profitability (NOI, cap rate) cannot be computed. `mart.property_profitability`
uses **revenue capture** (billed charges ÷ gross potential rent) as a
revenue-efficiency proxy instead, plotted against physical occupancy. Six books
with no lease-charge lines at all (1,057 units) would show a false 0% and are
excluded rather than plotted, along with three zero-unit books and four
commercial books with no market rent - 13 of 25 in all, listed with their
reason on the "Not plotted" table. The same view drives the Portfolio tab's
profitability ranking and, per property, a quadrant-movement insight.

### Step 8 - optional: the insight panel

```bash
ollama pull qwen3.5:4b               # 3.4 GB. NOT qwen3.5:latest, that is the 9B
aker-etl insights generate --dry-run # per-chunk token counts, no inference
aker-etl insights generate
aker-etl insights show --scope portfolio
```

Insights are generated once per snapshot and stored, so the dashboard never
waits on inference. If Ollama is unreachable, `insights generate` exits 0 with a
warning and the dashboard renders normally with an empty panel.

A third pass (`AKER_INSIGHT_POSITIONING`, on by default) makes one call per
plottable property naming the lever that would move it to a better matrix
quadrant. Every property dialog shows this even without Ollama: when no stored
`positioning` insight exists, the panel falls back to deterministic,
SQL-templated guidance and is marked **computed** rather than with a model tag.

### Shutting down

```bash
docker compose -f docker/docker-compose.yml --env-file .env down      # keep data
docker compose -f docker/docker-compose.yml --env-file .env down -v   # drop data
```

---

## 3. Command reference

```
aker-etl init-db                   Apply sql/*.sql in order (idempotent)
aker-etl load [--data-dir PATH] [--force] [--dry-run] [--jobs N]
              [--only rent_roll|unit_availability]
aker-etl validate [--strict] [--run-id N]
aker-etl status                    Recent runs + the golden-number check
aker-etl serve [--host H] [--port P]
aker-etl insights generate [--snapshot DATE] [--force] [--dry-run]
aker-etl insights show [--snapshot DATE] [--scope portfolio|asset|property]
aker-etl export-json PATH          Dump the analytical payload
aker-etl reset --yes               TRUNCATE core.* and raw.* (never drops schemas)
```

`--dry-run` parses and reconciles without writing to the database - the quickest
way to see whether a new drop of files still matches the expected format.

Re-running `aker-etl load` on unchanged files is nearly free: files are matched
by SHA-256 and skipped, so a second run touches nothing. Use `--force` to reload
anyway.

Every one of these is also a `make` target - `make help` lists them. The Makefile
is a convenience wrapper, not a build system; nothing depends on it.

---

## 4. Tests

```bash
pytest -m "not integration"        # 69 tests, ~1.5s, no database needed
pytest -m integration              # 19 tests, needs the container running
pytest                             # everything
make check                         # ruff + mypy + unit tests
```

`.github/workflows/ci.yml` runs the same things: a `unit` job (ruff, mypy, unit
tests, no services) and an `integration` job that stands up `postgres:17`,
applies the schema, runs the integration tests and does a full load with the
golden-number check. `validate --strict` runs there too but is non-blocking -
see section 4.1 of `REMAINING.md` for why.

Integration tests `TRUNCATE` the database, so do not run them while a load or an
insight generation is in flight.

The unit tests parse the real corpus and assert the golden numbers, so a change
in the source format fails before anything reaches the database. The integration
tests assert idempotency (a second load skips all 50 files) and that a `--force`
reload produces identical row counts.

---

## 5. How it is put together

```
docker/            Compose file + initdb extensions
sql/               Schema, in dependency order (005 … 090)
src/aker_etl/
  parsers/         Two workbook grammars, no database dependency
  models.py        Row models + the shared Decimal/date coercers
  loader.py        Advisory lock -> parse in parallel -> COPY -> merge -> validate
  validate.py      Post-load reconciliation rules
  insights/        Context payload, output schema, local-model call
  dashboard/       FastAPI read API + a single self-contained page
tests/
```

**Three schemas.** `raw` holds the ingest audit trail (runs, files, issues),
`core` holds the dimensions and facts, `mart` holds read models for the
dashboard. Every fact hangs off a `snapshot`, so next month's drop of files
loads *alongside* this one rather than over it.

**Money is `NUMERIC`, never float.** Detail rows arrive as int/float and the
summary blocks arrive as comma-formatted strings (`'260,778.00'`); both go
through the same `Decimal` coercer. All 4,106 blocks reconcile against their
printed `Total` row to within half a cent.

**Nothing is silently dropped.** Anything anomalous that does not stop the load
is written to `raw.load_issue` with the file and sheet row, and surfaced on the
dashboard's Data quality tab.

**The model never computes a number.** SQL does every calculation, and ranking
happens in code before the payload is built. Each generated insight must cite
its figures, and any value that is not present verbatim in the payload it was
given is dropped before it can reach the page.

Two deliberate choices worth knowing about:

* `synchronous_commit=off` is set on the container. The xlsx files are the
  system of record, so a crash costs a re-run rather than data.
* `core.lease` rows are deleted and re-inserted per (snapshot, property) on
  reload rather than upserted. An upsert would leave behind any lease that
  disappeared from a changed file - and a changed file is the only case where
  a reload happens at all.
