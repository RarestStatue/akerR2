"""Staging + COPY + merge. One run = one transaction.

Deviations from PLAN.md 5.3, both forced and both deliberate:

* `core.lease` rows are deleted and re-inserted per (snapshot, property) rather
  than upserted on (snapshot_id, unit_id, section). An upsert leaves behind any
  lease that disappeared from a changed file -- and a changed file is the only
  case where a reload happens at all, since unchanged files are skipped by hash.
  Delete-scoped-by-property is the only form that cannot leave stale rows.
* The mart refresh happens after COMMIT, not inside the run transaction.
  `REFRESH MATERIALIZED VIEW CONCURRENTLY` is rejected inside a transaction
  block, so it cannot be part of the atomic unit no matter how it is ordered.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import psycopg

from . import __version__
from .config import Settings
from .db import LOAD_LOCK_KEY, connect, refresh_marts, scalar
from .models import (
    AvailabilityFile,
    RentRollFile,
    derive_asset_key,
    derive_book_type,
)
from .parsers import parse_availability, parse_rent_roll
from .validate import run_validations

log = logging.getLogger(__name__)

RENT_ROLL = "rent_roll"
UNIT_AVAILABILITY = "unit_availability"

# The two shapes `parse_files` can return. Both carry `sheet_rows`, `parsed_rows`
# and `issues`; everything past that is discriminated with `isinstance`.
ParsedFile = RentRollFile | AvailabilityFile


@dataclass
class FileMeta:
    path: Path
    dataset: str
    sha256: str
    byte_size: int
    modified_at: dt.datetime


@dataclass
class LoadResult:
    run_id: int | None = None
    files_seen: int = 0
    files_loaded: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    rows_loaded: int = 0
    leases: int = 0
    charges: int = 0
    availability: int = 0
    summary_groups: int = 0
    charge_summaries: int = 0
    issue_counts: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str]] = field(default_factory=list)
    elapsed_s: float = 0.0
    status: str = "running"

    @property
    def errors(self) -> int:
        return self.issue_counts.get("error", 0)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_files(settings: Settings, only: str | None = None) -> list[FileMeta]:
    out: list[FileMeta] = []
    targets = [(RENT_ROLL, settings.rent_roll_dir), (UNIT_AVAILABILITY, settings.availability_dir)]
    for dataset, directory in targets:
        if only and dataset != only:
            continue
        if not directory.is_dir():
            log.warning("missing data directory: %s", directory)
            continue
        for path in sorted(directory.glob("*.xlsx")):
            if path.name.startswith("~$"):      # Excel lock files
                continue
            st = path.stat()
            out.append(
                FileMeta(
                    path=path,
                    dataset=dataset,
                    sha256=sha256_of(path),
                    byte_size=st.st_size,
                    modified_at=dt.datetime.fromtimestamp(st.st_mtime, tz=dt.timezone.utc),
                )
            )
    return out


def _parse_one(args: tuple[str, str]) -> tuple[str, str, ParsedFile]:
    """Runs in a worker process, so it returns plain pydantic models -- no DB handles."""
    dataset, path_str = args
    path = Path(path_str)
    if dataset == RENT_ROLL:
        return dataset, path_str, parse_rent_roll(path)
    return dataset, path_str, parse_availability(path)


def parse_files(
    metas: list[FileMeta], jobs: int | None = None
) -> tuple[dict[str, ParsedFile], list[tuple[str, str]]]:
    """Parse in parallel. Parsing is CPU-bound XML work and ~95% of wall time."""
    parsed: dict[str, ParsedFile] = {}
    failures: list[tuple[str, str]] = []
    if not metas:
        return parsed, failures

    workers = jobs or min(8, os.cpu_count() or 1)
    payload = [(m.dataset, str(m.path)) for m in metas]

    if workers <= 1 or len(payload) == 1:
        for item in payload:
            try:
                dataset, path_str, result = _parse_one(item)
                parsed[path_str] = result
            except Exception as exc:  # noqa: BLE001 - recorded, run continues
                failures.append((item[1], f"{type(exc).__name__}: {exc}"))
        return parsed, failures

    with cf.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_parse_one, item): item for item in payload}
        for fut in cf.as_completed(futures):
            item = futures[fut]
            try:
                _dataset, path_str, result = fut.result()
                parsed[path_str] = result
            except Exception as exc:  # noqa: BLE001
                failures.append((item[1], f"{type(exc).__name__}: {exc}"))
    return parsed, failures


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #


def load(
    settings: Settings,
    *,
    force: bool = False,
    dry_run: bool = False,
    jobs: int | None = None,
    only: str | None = None,
) -> LoadResult:
    started = time.monotonic()
    result = LoadResult()

    metas = scan_files(settings, only=only)
    result.files_seen = len(metas)
    log.info("found %d files under %s", len(metas), settings.aker_data_dir)

    if dry_run:
        parsed, failures = parse_files(metas, jobs=jobs)
        result.failures = failures
        result.files_failed = len(failures)
        result.files_loaded = len(parsed)
        _tally(result, parsed.values())
        result.status = "failed" if failures else "succeeded"
        result.elapsed_s = time.monotonic() - started
        return result

    with connect(settings, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (LOAD_LOCK_KEY,))
        try:
            _load_txn(conn, settings, metas, result, force=force, jobs=jobs)
            conn.commit()
        except Exception:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOAD_LOCK_KEY,))
            conn.commit()
            raise

        # Outside the transaction: CONCURRENTLY is rejected inside one.
        conn.autocommit = True
        try:
            refresh_marts(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOAD_LOCK_KEY,))

    result.elapsed_s = time.monotonic() - started
    return result


def _tally(result: LoadResult, parsed) -> None:
    for obj in parsed:
        if isinstance(obj, RentRollFile):
            result.leases += len(obj.leases)
            result.charges += sum(len(x.charges) for x in obj.leases)
            result.summary_groups += len(obj.summary_groups)
            result.charge_summaries += len(obj.charge_summary)
        elif isinstance(obj, AvailabilityFile):
            result.availability += 1
        for issue in getattr(obj, "issues", []):
            result.issue_counts[issue.severity] = result.issue_counts.get(issue.severity, 0) + 1
    result.rows_loaded = (
        result.leases + result.charges + result.summary_groups
        + result.charge_summaries + result.availability
    )


def _load_txn(
    conn: psycopg.Connection,
    settings: Settings,
    metas: list[FileMeta],
    result: LoadResult,
    *,
    force: bool,
    jobs: int | None,
) -> None:
    cur = conn.cursor()

    cur.execute(
        """INSERT INTO raw.ingest_run (data_dir, tool_version, files_seen)
           VALUES (%s, %s, %s) RETURNING run_id""",
        (str(settings.aker_data_dir), __version__, len(metas)),
    )
    run_id = scalar(cur)
    result.run_id = run_id
    log.info("ingest run %d", run_id)

    # --- 1. skip files already loaded byte-for-byte -------------------------- #
    pending: list[FileMeta] = []
    for meta in metas:
        if force:
            pending.append(meta)
            continue
        cur.execute(
            """SELECT 1 FROM raw.source_file
               WHERE dataset = %s AND file_name = %s AND sha256 = %s LIMIT 1""",
            (meta.dataset, meta.path.name, meta.sha256),
        )
        if cur.fetchone():
            result.files_skipped += 1
        else:
            pending.append(meta)
    if result.files_skipped:
        log.info("%d file(s) unchanged since a previous run, skipped", result.files_skipped)

    # --- 2. parse ------------------------------------------------------------ #
    t0 = time.monotonic()
    parsed, failures = parse_files(pending, jobs=jobs)
    result.failures = failures
    result.files_failed = len(failures)
    for path_str, err in failures:
        log.error("parse failed: %s -- %s", Path(path_str).name, err)
    log.info("parsed %d file(s) in %.2fs", len(parsed), time.monotonic() - t0)

    # Before the early return: when every file fails, section 11 is never reached,
    # and raw.load_issue -- the durable record the Quality tab reads -- would carry
    # no reason at all for the failed run.
    if not parsed:
        for path_str, err in failures:
            _issue(cur, run_id, None, "error", "file_parse_failed", None,
                   {"file": Path(path_str).name, "error": err})
        _finish_run(cur, run_id, result)
        return

    # --- 3. raw.source_file --------------------------------------------------- #
    file_ids: dict[str, int] = {}
    for meta in pending:
        obj = parsed.get(str(meta.path))
        if obj is None:
            continue
        cur.execute(
            """INSERT INTO raw.source_file
                 (run_id, dataset, file_name, file_path, sha256, byte_size,
                  modified_at, sheet_rows, parsed_rows)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (dataset, file_name, sha256) DO UPDATE
                 SET run_id = EXCLUDED.run_id, loaded_at = now(),
                     parsed_rows = EXCLUDED.parsed_rows
               RETURNING file_id""",
            (run_id, meta.dataset, meta.path.name, str(meta.path), meta.sha256,
             meta.byte_size, meta.modified_at, obj.sheet_rows, obj.parsed_rows),
        )
        file_ids[str(meta.path)] = scalar(cur)
    result.files_loaded = len(file_ids)

    rent_rolls = {p: o for p, o in parsed.items() if isinstance(o, RentRollFile)}
    avails = {p: o for p, o in parsed.items() if isinstance(o, AvailabilityFile)}

    # --- 4. snapshots --------------------------------------------------------- #
    months: dict[dt.date, dt.date | None] = {}
    for obj in list(rent_rolls.values()) + list(avails.values()):
        month = getattr(obj, "report_month", None)
        months.setdefault(obj.as_of_date, None)
        if month and months[obj.as_of_date] is None:
            months[obj.as_of_date] = month
    snapshot_ids: dict[dt.date, int] = {}
    for as_of, month in months.items():
        cur.execute(
            """INSERT INTO core.snapshot (as_of_date, report_month) VALUES (%s, %s)
               ON CONFLICT (as_of_date) DO UPDATE
                 SET report_month = COALESCE(EXCLUDED.report_month, core.snapshot.report_month)
               RETURNING snapshot_id""",
            (as_of, month),
        )
        snapshot_ids[as_of] = scalar(cur)

    # --- 5. property ---------------------------------------------------------- #
    props: dict[str, str] = {}
    for obj in list(rent_rolls.values()) + list(avails.values()):
        props[obj.property_code] = obj.property_name
    property_ids: dict[str, int] = {}
    for code, name in sorted(props.items()):
        cur.execute(
            """INSERT INTO core.property (property_code, property_name, asset_key, book_type)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (property_code) DO UPDATE
                 SET property_name = EXCLUDED.property_name,
                     asset_key = EXCLUDED.asset_key,
                     book_type = EXCLUDED.book_type
               RETURNING property_id""",
            (code, name, derive_asset_key(code), derive_book_type(code)),
        )
        property_ids[code] = scalar(cur)

    # --- 6. unit_type / unit / resident (staged, then merged) ----------------- #
    unit_types: set[tuple[int, str]] = set()
    units: dict[tuple[int, str], tuple[str | None, int | None]] = {}
    residents: dict[str, str] = {}
    for obj in rent_rolls.values():
        pid = property_ids[obj.property_code]
        for lease in obj.leases:
            if lease.unit_type_code:
                unit_types.add((pid, lease.unit_type_code))
            key = (pid, lease.unit_code)
            prev = units.get(key, (None, None))
            units[key] = (
                lease.unit_type_code or prev[0],
                lease.unit_sqft if lease.unit_sqft is not None else prev[1],
            )
            if lease.resident_id:
                residents[lease.resident_id] = lease.resident_name or lease.resident_id

    if unit_types:
        cur.execute("CREATE TEMP TABLE stage_unit_type (property_id smallint, unit_type_code text) ON COMMIT DROP")
        with cur.copy("COPY stage_unit_type (property_id, unit_type_code) FROM STDIN") as cp:
            for pid, code in sorted(unit_types):
                cp.write_row((pid, code))
        cur.execute(
            """INSERT INTO core.unit_type (property_id, unit_type_code)
               SELECT DISTINCT property_id, unit_type_code FROM stage_unit_type
               ON CONFLICT (property_id, unit_type_code) DO NOTHING"""
        )
    cur.execute("SELECT property_id, unit_type_code, unit_type_id FROM core.unit_type")
    unit_type_ids = {(r[0], r[1]): r[2] for r in cur.fetchall()}

    if units:
        cur.execute(
            """CREATE TEMP TABLE stage_unit
                 (property_id smallint, unit_code text, unit_type_id int, unit_sqft int)
               ON COMMIT DROP"""
        )
        with cur.copy("COPY stage_unit (property_id, unit_code, unit_type_id, unit_sqft) FROM STDIN") as cp:
            for (pid, unit_code), (type_code, sqft) in sorted(units.items()):
                cp.write_row((pid, unit_code, unit_type_ids.get((pid, type_code)), sqft))
        cur.execute(
            """INSERT INTO core.unit (property_id, unit_code, unit_type_id, unit_sqft)
               SELECT property_id, unit_code, unit_type_id, unit_sqft FROM stage_unit
               ON CONFLICT (property_id, unit_code) DO UPDATE
                 SET unit_type_id = COALESCE(EXCLUDED.unit_type_id, core.unit.unit_type_id),
                     unit_sqft    = COALESCE(EXCLUDED.unit_sqft,    core.unit.unit_sqft)"""
        )
    cur.execute(
        "SELECT property_id, unit_code, unit_id FROM core.unit WHERE property_id = ANY(%s)",
        (list(property_ids.values()),),
    )
    unit_ids = {(r[0], r[1]): r[2] for r in cur.fetchall()}

    if residents:
        cur.execute("CREATE TEMP TABLE stage_resident (resident_id text, display_name text) ON COMMIT DROP")
        with cur.copy("COPY stage_resident (resident_id, display_name) FROM STDIN") as cp:
            for rid, name in sorted(residents.items()):
                cp.write_row((rid, name))
        cur.execute(
            """INSERT INTO core.resident (resident_id, display_name)
               SELECT resident_id, display_name FROM stage_resident
               ON CONFLICT (resident_id) DO UPDATE SET display_name = EXCLUDED.display_name"""
        )

    # --- 7. charge codes: never guess, but never drop data either ------------- #
    seen_codes = {c.charge_code for o in rent_rolls.values() for x in o.leases for c in x.charges}
    seen_codes |= {cs.charge_code for o in rent_rolls.values() for cs in o.charge_summary}
    cur.execute("SELECT charge_code FROM core.charge_code")
    known = {r[0] for r in cur.fetchall()}
    unknown = sorted(seen_codes - known)
    for code in unknown:
        cur.execute(
            """INSERT INTO core.charge_code (charge_code, category, description, label_verified)
               VALUES (%s, 'other', NULL, false) ON CONFLICT (charge_code) DO NOTHING""",
            (code,),
        )
        _issue(cur, run_id, None, "warning", "unknown_charge_code", None, {"charge_code": code})
        log.warning("unknown charge code %r loaded as category 'other'", code)

    # --- 8. leases + charges -------------------------------------------------- #
    touched: set[tuple[int, int]] = set()
    for path_str, obj in rent_rolls.items():
        touched.add((snapshot_ids[obj.as_of_date], property_ids[obj.property_code]))
    for snap_id, pid in sorted(touched):
        cur.execute(
            "DELETE FROM core.lease WHERE snapshot_id = %s AND property_id = %s", (snap_id, pid)
        )

    lease_cols = ("snapshot_id, property_id, unit_id, unit_type_id, resident_id, section, "
                  "occupancy_status, unit_sqft, market_rent, resident_deposit, other_deposit, "
                  "balance, charges_total, move_in, lease_expiration, move_out, file_id, sheet_row")
    n_leases = 0
    with cur.copy(f"COPY core.lease ({lease_cols}) FROM STDIN") as cp:
        for path_str, obj in sorted(rent_rolls.items()):
            snap_id = snapshot_ids[obj.as_of_date]
            pid = property_ids[obj.property_code]
            fid = file_ids[path_str]
            for lease in obj.leases:
                cp.write_row((
                    snap_id, pid, unit_ids[(pid, lease.unit_code)],
                    unit_type_ids.get((pid, lease.unit_type_code)) if lease.unit_type_code else None,
                    lease.resident_id, lease.section, lease.occupancy_status, lease.unit_sqft,
                    lease.market_rent, lease.resident_deposit, lease.other_deposit, lease.balance,
                    lease.charges_total, lease.move_in, lease.lease_expiration, lease.move_out,
                    fid, lease.sheet_row,
                ))
                n_leases += 1
    result.leases = n_leases

    cur.execute(
        """SELECT snapshot_id, unit_id, section::text, lease_id FROM core.lease
           WHERE (snapshot_id, property_id) IN (SELECT * FROM unnest(%s::int[], %s::smallint[]))""",
        ([t[0] for t in sorted(touched)], [t[1] for t in sorted(touched)]),
    )
    lease_ids = {(r[0], r[1], r[2]): r[3] for r in cur.fetchall()}

    n_charges = 0
    with cur.copy("COPY core.lease_charge (lease_id, line_no, charge_code, amount, sheet_row) FROM STDIN") as cp:
        for path_str, obj in sorted(rent_rolls.items()):
            snap_id = snapshot_ids[obj.as_of_date]
            pid = property_ids[obj.property_code]
            for lease in obj.leases:
                lid = lease_ids[(snap_id, unit_ids[(pid, lease.unit_code)], lease.section)]
                for charge in lease.charges:
                    cp.write_row((lid, charge.line_no, charge.charge_code, charge.amount, charge.sheet_row))
                    n_charges += 1
    result.charges = n_charges

    # --- 9. report-provided rollups ------------------------------------------ #
    for path_str, obj in sorted(rent_rolls.items()):
        snap_id = snapshot_ids[obj.as_of_date]
        pid = property_ids[obj.property_code]
        fid = file_ids[path_str]
        for g in obj.summary_groups:
            cur.execute(
                """INSERT INTO core.rent_roll_summary_group
                     (snapshot_id, property_id, group_label, square_footage, market_rent,
                      lease_charges, security_deposit, other_deposits, unit_count,
                      pct_unit_occupancy, pct_sqft_occupied, balance, file_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (snapshot_id, property_id, group_label) DO UPDATE SET
                     square_footage = EXCLUDED.square_footage, market_rent = EXCLUDED.market_rent,
                     lease_charges = EXCLUDED.lease_charges,
                     security_deposit = EXCLUDED.security_deposit,
                     other_deposits = EXCLUDED.other_deposits, unit_count = EXCLUDED.unit_count,
                     pct_unit_occupancy = EXCLUDED.pct_unit_occupancy,
                     pct_sqft_occupied = EXCLUDED.pct_sqft_occupied,
                     balance = EXCLUDED.balance, file_id = EXCLUDED.file_id""",
                (snap_id, pid, g.group_label, g.square_footage, g.market_rent, g.lease_charges,
                 g.security_deposit, g.other_deposits, g.unit_count, g.pct_unit_occupancy,
                 g.pct_sqft_occupied, g.balance, fid),
            )
            result.summary_groups += 1
        for cs in obj.charge_summary:
            cur.execute(
                """INSERT INTO core.charge_summary
                     (snapshot_id, property_id, charge_code, amount, file_id)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (snapshot_id, property_id, charge_code) DO UPDATE
                     SET amount = EXCLUDED.amount, file_id = EXCLUDED.file_id""",
                (snap_id, pid, cs.charge_code, cs.amount, fid),
            )
            result.charge_summaries += 1

    # --- 10. unit availability ------------------------------------------------ #
    for path_str, obj in sorted(avails.items()):
        snap_id = snapshot_ids[obj.as_of_date]
        pid = property_ids[obj.property_code]
        cur.execute(
            """INSERT INTO core.unit_availability
                 (snapshot_id, property_id, avg_sqft, avg_rent, units, occupied_no_notice,
                  vacant_rented, vacant_unrented, notice_rented, notice_unrented, available,
                  model, down, admin, pct_occ, pct_occ_w_nonrev, pct_leased, pct_trend, file_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (snapshot_id, property_id) DO UPDATE SET
                 avg_sqft = EXCLUDED.avg_sqft, avg_rent = EXCLUDED.avg_rent,
                 units = EXCLUDED.units, occupied_no_notice = EXCLUDED.occupied_no_notice,
                 vacant_rented = EXCLUDED.vacant_rented, vacant_unrented = EXCLUDED.vacant_unrented,
                 notice_rented = EXCLUDED.notice_rented, notice_unrented = EXCLUDED.notice_unrented,
                 available = EXCLUDED.available, model = EXCLUDED.model, down = EXCLUDED.down,
                 admin = EXCLUDED.admin, pct_occ = EXCLUDED.pct_occ,
                 pct_occ_w_nonrev = EXCLUDED.pct_occ_w_nonrev, pct_leased = EXCLUDED.pct_leased,
                 pct_trend = EXCLUDED.pct_trend, file_id = EXCLUDED.file_id""",
            (snap_id, pid, obj.avg_sqft, obj.avg_rent, obj.units, obj.occupied_no_notice,
             obj.vacant_rented, obj.vacant_unrented, obj.notice_rented, obj.notice_unrented,
             obj.available, obj.model, obj.down, obj.admin, obj.pct_occ, obj.pct_occ_w_nonrev,
             obj.pct_leased, obj.pct_trend, file_ids[path_str]),
        )
        result.availability += 1

    # --- 11. parse-time issues ------------------------------------------------ #
    for path_str, obj in parsed.items():
        issue_fid = file_ids.get(path_str)
        for issue in getattr(obj, "issues", []):
            _issue(cur, run_id, issue_fid, issue.severity, issue.rule, issue.sheet_row, issue.detail)
    for path_str, err in failures:
        _issue(cur, run_id, None, "error", "file_parse_failed", None,
               {"file": Path(path_str).name, "error": err})

    # --- 12. validations ------------------------------------------------------ #
    run_validations(cur, run_id, sorted(snapshot_ids.values()))

    result.rows_loaded = (result.leases + result.charges + result.summary_groups
                          + result.charge_summaries + result.availability)
    _finish_run(cur, run_id, result)

    cur.execute("ANALYZE core.lease, core.lease_charge, core.unit, core.resident, "
                "core.unit_availability, core.rent_roll_summary_group, core.charge_summary")


def _issue(cur, run_id: int, file_id: int | None, severity: str, rule: str,
           sheet_row: int | None, detail: dict) -> None:
    from psycopg.types.json import Jsonb

    cur.execute(
        """INSERT INTO raw.load_issue (run_id, file_id, severity, rule, sheet_row, detail)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (run_id, file_id, severity, rule, sheet_row, Jsonb(_json_safe(detail))),
    )


def _json_safe(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, (dt.date, dt.datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _finish_run(cur, run_id: int, result: LoadResult) -> None:
    cur.execute(
        """SELECT severity::text, count(*) FROM raw.load_issue
           WHERE run_id = %s GROUP BY 1""",
        (run_id,),
    )
    result.issue_counts = {r[0]: r[1] for r in cur.fetchall()}

    if result.errors or result.files_failed:
        status = "failed" if result.files_failed == result.files_seen else "partial"
    elif result.issue_counts.get("warning"):
        status = "partial"
    else:
        status = "succeeded"
    result.status = status

    cur.execute(
        """UPDATE raw.ingest_run
              SET finished_at = now(), status = %s, files_loaded = %s, files_skipped = %s,
                  files_failed = %s, rows_loaded = %s
            WHERE run_id = %s""",
        (status, result.files_loaded, result.files_skipped, result.files_failed,
         result.rows_loaded, run_id),
    )
