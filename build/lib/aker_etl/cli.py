"""Typer entrypoint. `aker-etl --help` for the full surface."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import get_settings
from .logging_conf import configure

app = typer.Typer(add_completion=False, help="Aker rent roll / unit availability ETL.")
insights_app = typer.Typer(help="Local-model insight layer (optional).")
app.add_typer(insights_app, name="insights")
console = Console()
log = logging.getLogger(__name__)

# PLAN.md section 0. `status` and `load --dry-run` check against these, so a
# change in the source format is one command away from being visible.
GOLDEN = {
    "rent_roll_files": 25,
    "availability_files": 25,
    "leases": 4106,
    "leases_current": 4013,
    "leases_future": 93,
    "units": 4013,
    "unit_types": 448,
    "residents": 3917,
    "charges": 9177,
    "charge_codes": 32,
    "summary_groups": 150,
    "charge_summaries": 117,
    "availability_rows": 25,
    "properties": 25,
}

EXIT_OK, EXIT_ERRORS, EXIT_STRUCTURAL = 0, 2, 3


def _settings(data_dir: Optional[Path] = None):
    s = get_settings()
    if data_dir:
        s.aker_data_dir = data_dir
    configure(s.aker_log_level)
    return s


@app.command("init-db")
def init_db_cmd(
    drop: bool = typer.Option(
        False, "--drop",
        help="DROP SCHEMA mart, core, raw CASCADE before applying. Destroys every row."),
    yes: bool = typer.Option(False, "--yes", help="Required with --drop."),
) -> None:
    """Run sql/*.sql in filename order. Idempotent -- safe to repeat.

    `--drop` rebuilds the schema from scratch. sql/ carries no ALTER statements
    by design, so this is how a schema change is applied; re-load afterwards with
    `aker-etl load`.
    """
    from .db import init_db

    if drop and not yes:
        console.print(
            "[red]--drop destroys every row in raw.*, core.* and mart.* "
            "(re-loadable from the workbooks in ~1s). Re-run with --yes.[/]"
        )
        raise typer.Exit(EXIT_STRUCTURAL)

    s = _settings()
    dropped, applied = init_db(s, drop=drop)
    if dropped:
        console.print(f"[yellow]dropped schemas mart, core, raw[/] (via {', '.join(dropped)})")
    console.print(f"[green]applied {len(applied)} migration(s):[/] {', '.join(applied)}")


@app.command()
def load(
    data_dir: Optional[Path] = typer.Option(None, "--data-dir"),
    force: bool = typer.Option(False, "--force", help="Reload files even if unchanged."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and check only; no writes."),
    jobs: Optional[int] = typer.Option(None, "--jobs", "-j"),
    only: Optional[str] = typer.Option(None, "--only", help="rent_roll | unit_availability"),
) -> None:
    """Parse every workbook and load it."""
    from .loader import load as run_load

    if only and only not in ("rent_roll", "unit_availability"):
        console.print("[red]--only must be rent_roll or unit_availability[/]")
        raise typer.Exit(EXIT_STRUCTURAL)

    s = _settings(data_dir)
    try:
        result = run_load(s, force=force, dry_run=dry_run, jobs=jobs, only=only)
    except Exception as exc:  # noqa: BLE001 - structural failures abort the run
        console.print(f"[red]structural failure:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(EXIT_STRUCTURAL) from exc

    _print_load_result(result, dry_run=dry_run, only=only)
    raise typer.Exit(EXIT_ERRORS if result.errors or result.files_failed else EXIT_OK)


def _print_load_result(result, *, dry_run: bool, only: Optional[str] = None) -> None:
    t = Table(title="dry run (nothing written)" if dry_run else f"ingest run {result.run_id}",
              show_header=True)
    t.add_column("metric")
    t.add_column("value", justify="right")
    t.add_column("expected", justify="right")
    # --only halves the expected file count (one of the two file kinds is
    # skipped entirely), so the golden 50 is only meaningful on a full load.
    files_seen_expected = "" if only else GOLDEN["rent_roll_files"] + GOLDEN["availability_files"]
    rows = [
        ("files seen", result.files_seen, files_seen_expected),
        ("files loaded", result.files_loaded, ""),
        ("files skipped", result.files_skipped, ""),
        ("files failed", result.files_failed, 0),
        ("leases", result.leases, GOLDEN["leases"]),
        ("lease charges", result.charges, GOLDEN["charges"]),
        ("summary groups", result.summary_groups, GOLDEN["summary_groups"]),
        ("charge summaries", result.charge_summaries, GOLDEN["charge_summaries"]),
        ("availability rows", result.availability, GOLDEN["availability_rows"]),
        ("rows loaded", result.rows_loaded, ""),
    ]
    # The expected column only means anything on a full load. When every file was
    # skipped as unchanged, these counts are 0 by design, not by failure.
    full_load = result.files_loaded == result.files_seen and result.files_seen > 0
    for name, value, expected in rows:
        match = ""
        if not full_load and name not in ("files seen", "files failed"):
            expected = ""
        if expected != "" and value != expected:
            match = f"[yellow]{expected}[/]"
        elif expected != "":
            match = f"[green]{expected}[/]"
        t.add_row(name, str(value), match)
    console.print(t)
    for sev in ("error", "warning", "info"):
        n = result.issue_counts.get(sev, 0)
        if n:
            colour = {"error": "red", "warning": "yellow", "info": "dim"}[sev]
            console.print(f"[{colour}]{sev}s: {n}[/]")
    for path, err in result.failures:
        console.print(f"[red]failed:[/] {Path(path).name} -- {err}")
    console.print(f"status [bold]{result.status}[/] in {result.elapsed_s:.2f}s")


@app.command()
def validate(
    strict: bool = typer.Option(False, "--strict", help="Treat warnings as errors (CI)."),
    run_id: Optional[int] = typer.Option(None, "--run-id"),
) -> None:
    """Report the issues recorded by a load. Does not re-parse."""
    from .db import connect

    s = _settings()
    with connect(s, autocommit=True) as conn, conn.cursor() as cur:
        if run_id is None:
            cur.execute("SELECT max(run_id) FROM raw.ingest_run")
            row = cur.fetchone()
            run_id = row[0] if row else None
        if run_id is None:
            console.print("[yellow]no ingest runs recorded[/]")
            raise typer.Exit(EXIT_OK)
        cur.execute(
            """SELECT severity::text, rule, count(*)
               FROM raw.load_issue WHERE run_id = %s
               GROUP BY 1,2 ORDER BY 1,2""",
            (run_id,),
        )
        rows = cur.fetchall()

    t = Table(title=f"issues for run {run_id}")
    t.add_column("severity")
    t.add_column("rule")
    t.add_column("count", justify="right")
    for sev, rule, n in rows:
        colour = {"error": "red", "warning": "yellow", "info": "dim"}.get(sev, "")
        t.add_row(f"[{colour}]{sev}[/]", rule, str(n))
    console.print(t if rows else "[green]no issues recorded[/]")

    errors = sum(n for sev, _, n in rows if sev == "error")
    warnings = sum(n for sev, _, n in rows if sev == "warning")
    if errors or (strict and warnings):
        raise typer.Exit(EXIT_ERRORS)


@app.command()
def status() -> None:
    """Last runs, issue counts, and the golden-number check."""
    from .db import connect, scalar

    s = _settings()
    with connect(s, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT run_id, status::text, started_at, finished_at, files_loaded,
                      files_skipped, files_failed, rows_loaded
               FROM raw.ingest_run ORDER BY run_id DESC LIMIT 5"""
        )
        runs = cur.fetchall()
        counts = {}
        for key, sql in {
            "properties": "SELECT count(*) FROM core.property",
            "leases": "SELECT count(*) FROM core.lease",
            "leases_current": "SELECT count(*) FROM core.lease WHERE section='current'",
            "leases_future": "SELECT count(*) FROM core.lease WHERE section='future'",
            "units": "SELECT count(*) FROM core.unit",
            "unit_types": "SELECT count(*) FROM core.unit_type",
            "residents": "SELECT count(*) FROM core.resident",
            "charges": "SELECT count(*) FROM core.lease_charge",
            "charge_codes": "SELECT count(DISTINCT charge_code) FROM core.lease_charge",
            "summary_groups": "SELECT count(*) FROM core.rent_roll_summary_group",
            "charge_summaries": "SELECT count(*) FROM core.charge_summary",
            "availability_rows": "SELECT count(*) FROM core.unit_availability",
        }.items():
            cur.execute(sql)
            counts[key] = scalar(cur)
        cur.execute("SELECT severity::text, count(*) FROM raw.load_issue GROUP BY 1")
        issues = dict(cur.fetchall())
        cur.execute("SELECT as_of_date, report_month FROM core.snapshot ORDER BY as_of_date")
        snapshots = cur.fetchall()

    rt = Table(title="recent ingest runs")
    for col in ("run", "status", "started", "loaded", "skipped", "failed", "rows"):
        rt.add_column(col)
    for r in runs:
        rt.add_row(str(r[0]), r[1], r[2].strftime("%Y-%m-%d %H:%M:%S"),
                   str(r[4]), str(r[5]), str(r[6]), str(r[7]))
    console.print(rt)

    gt = Table(title="golden numbers")
    gt.add_column("metric")
    gt.add_column("in db", justify="right")
    gt.add_column("expected", justify="right")
    gt.add_column("", justify="center")
    ok = True
    for key, value in counts.items():
        expected = GOLDEN.get(key)
        if expected is None:
            gt.add_row(key, str(value), "-", "")
            continue
        good = value == expected
        ok &= good
        gt.add_row(key, str(value), str(expected), "[green]ok[/]" if good else "[red]DIFF[/]")
    console.print(gt)
    console.print(f"snapshots: {', '.join(f'{a} (month {m})' for a, m in snapshots) or 'none'}")
    console.print("issues: " + (", ".join(f"{k}={v}" for k, v in sorted(issues.items())) or "none"))
    if not ok:
        raise typer.Exit(EXIT_ERRORS)


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", help="Required. Truncates all data.")) -> None:
    """TRUNCATE core.* and raw.*. Never drops schemas or types."""
    from .db import connect, refresh_marts

    if not yes:
        console.print("[red]refusing without --yes[/]")
        raise typer.Exit(EXIT_STRUCTURAL)
    s = _settings()
    with connect(s, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """TRUNCATE core.lease_charge, core.lease, core.unit, core.unit_type,
                            core.resident, core.rent_roll_summary_group, core.charge_summary,
                            core.unit_availability, core.insight, core.insight_run,
                            core.snapshot, core.property, raw.load_issue, raw.source_file,
                            raw.ingest_run RESTART IDENTITY CASCADE"""
            )
        # TRUNCATE does not touch a materialized view. Without this the dashboard
        # renders a full Portfolio tab over an empty database.
        refresh_marts(conn)
    console.print("[green]core.* and raw.* truncated, mart views refreshed[/] "
                  "(charge_code seed retained)")


# --------------------------------------------------------------------------- #
# insights
# --------------------------------------------------------------------------- #


@insights_app.command("generate")
def insights_generate(
    snapshot: Optional[str] = typer.Option(None, "--snapshot", help="YYYY-MM-DD; default latest."),
    force: bool = typer.Option(False, "--force", help="Regenerate even if the payload is unchanged."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print chunks + token counts, no inference."),
    from_payload: Optional[Path] = typer.Option(
        None, "--from",
        help="Read the context payload from a file written by `export-json` instead of the "
             "database. Implies no database connection; requires --out."),
    out: Optional[Path] = typer.Option(
        None, "--out",
        help="Write insights to this JSON file instead of the database. Import them later "
             "with `insights import`."),
) -> None:
    """Build the context payload and generate insights with the local model."""
    if from_payload and not out:
        console.print(
            "[red]--from requires --out: without a database there is nowhere to store insights[/]"
        )
        raise typer.Exit(EXIT_STRUCTURAL)
    if from_payload and snapshot:
        console.print(
            "[red]--from and --snapshot conflict: the payload file already fixes the "
            "snapshot (its \"as_of\" field)[/]"
        )
        raise typer.Exit(EXIT_STRUCTURAL)
    if from_payload and not from_payload.is_file():
        console.print(f"[red]--from {from_payload}: no such file[/]")
        raise typer.Exit(EXIT_STRUCTURAL)
    if out and out.suffix.lower() != ".json":
        console.print("[red]--out must be a .json path[/]")
        raise typer.Exit(EXIT_STRUCTURAL)
    if out and force:
        console.print(
            "[yellow]--force has no effect with --out: the idempotency guard is a database check[/]"
        )

    s = _settings()
    as_of = dt.date.fromisoformat(snapshot) if snapshot else None

    if out:
        from .insights.generate import generate_to_file

        try:
            outcome = generate_to_file(s, out, as_of=as_of, payload_file=from_payload, dry_run=dry_run)
        except ValueError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(EXIT_STRUCTURAL) from exc
    else:
        from .insights.generate import generate

        outcome = generate(s, as_of=as_of, force=force, dry_run=dry_run)

    console.print(outcome.render())
    raise typer.Exit(EXIT_ERRORS if outcome.status == "failed" else EXIT_OK)


@insights_app.command("import")
def insights_import(
    path: Path = typer.Argument(..., help="Artifact written by `insights generate --out`."),
    allow_stale: bool = typer.Option(
        False, "--allow-stale",
        help="Import even though the database payload changed since generation."),
    allow_empty: bool = typer.Option(
        False, "--allow-empty",
        help="Import an artifact with zero insights, clearing the snapshot's insights."),
) -> None:
    """Verify a generated artifact against the database and store it."""
    from .insights.store import import_artifact

    s = _settings()
    outcome = import_artifact(s, path, allow_stale=allow_stale, allow_empty=allow_empty)
    console.print(outcome.render())
    if outcome.status == "failed":
        raise typer.Exit(EXIT_STRUCTURAL)
    if outcome.status == "refused":
        raise typer.Exit(EXIT_ERRORS)


@insights_app.command("show")
def insights_show(
    snapshot: Optional[str] = typer.Option(None, "--snapshot"),
    scope: Optional[str] = typer.Option(None, "--scope", help="portfolio | asset | property"),
) -> None:
    """Print stored insights."""
    from .db import connect

    s = _settings()
    as_of = dt.date.fromisoformat(snapshot) if snapshot else None
    with connect(s, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT i.scope::text, COALESCE(p.property_code::text, i.asset_key, '-'),
                      i.category::text, i.priority::text, i.headline, i.detail, i.evidence, i.model
               FROM core.insight i
               JOIN core.snapshot s ON s.snapshot_id = i.snapshot_id
               LEFT JOIN core.property p ON p.property_id = i.property_id
               WHERE (%s::date IS NULL OR s.as_of_date = %s::date)
                 AND (%s::text IS NULL OR i.scope::text = %s::text)
               ORDER BY CASE i.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                        i.scope, i.insight_id""",
            (as_of, as_of, scope, scope),
        )
        rows = cur.fetchall()
    if not rows:
        console.print("[yellow]no insights stored for that filter[/]")
        return
    for scope_, target, category, priority, headline, detail, evidence, model in rows:
        colour = {"high": "red", "medium": "yellow", "low": "dim"}.get(priority, "")
        console.print(f"[{colour}]{priority.upper():6}[/] [bold]{headline}[/]")
        console.print(f"        {scope_}/{target} · {category} · {model}")
        console.print(f"        {detail}")
        for ev in evidence:
            console.print(f"        · {ev.get('metric')} = {ev.get('value')} {ev.get('comparison') or ''}")
        console.print()


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the dashboard."""
    import uvicorn

    s = _settings()
    console.print(f"[green]dashboard on http://{host}:{port}[/]")
    uvicorn.run("aker_etl.dashboard.app:app", host=host, port=port, log_level=s.aker_log_level.lower())


@app.command("export-json")
def export_json(out: Path = typer.Argument(..., help="Write the insight context payload here.")) -> None:
    """Dump the analytical payload -- handy for eyeballing what the model is given."""
    from .insights.context import build_payload

    s = _settings()
    from .db import connect

    with connect(s, autocommit=True) as conn:
        payload, sha = build_payload(conn)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    # sha is over canonical_json(payload), not this pretty-printed file -- the file
    # is written indented for eyeballing, so its own bytes hash to something else.
    console.print(
        f"[green]wrote {out}[/] ({out.stat().st_size / 1024:.1f} KB, payload sha256 {sha[:12]}…)"
    )


if __name__ == "__main__":
    app()
