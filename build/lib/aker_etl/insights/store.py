"""Artifact -> database. The second half of PLAN3 route B.

Everything in an artifact is an assertion made by a file. This module re-derives
the truth from the database and keeps only the insights that still survive it:
the payload is rebuilt from the mart views, its hash is compared against the one
the artifact claims, every cited figure is re-checked against that payload, and
every target is re-checked against core.property. The generator's own gate is
not trusted, because a file can be edited after the generator has finished with
it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..db import connect, scalar
from .artifact import ArtifactVersionError, read_artifact
from .context import build_payload, latest_snapshot
from .generate import _known_targets, _persist, check_evidence, collect_values
from .schema import Insight, normalise_target

log = logging.getLogger(__name__)


@dataclass
class ImportOutcome:
    status: str = "succeeded"  # succeeded | refused | failed
    path: str = ""
    model: str = ""
    as_of: str = ""
    snapshot_id: int | None = None
    read: int = 0  # insights in the file
    inserted: int = 0
    dropped: int = 0
    replaced: int = 0  # core.insight rows deleted first
    stale: bool = False
    artifact_sha: str = ""  # the payload hash the artifact claims
    payload_sha: str = ""   # the payload hash the database has now
    error: str | None = None
    drops: list[tuple[str, str]] = field(default_factory=list)  # (headline, reason)

    def render(self) -> str:
        if self.status == "failed":
            return f"[red]import failed:[/] {self.error}"
        if self.status == "refused":
            lines = [f"[yellow]import refused:[/] {self.error}"]
        else:
            lines = [
                f"[green]imported {self.inserted} of {self.read} insight(s)[/] from {self.path} "
                f"for {self.as_of} (snapshot {self.snapshot_id}), replaced {self.replaced}, "
                f"dropped {self.dropped} by the evidence check, model {self.model}"
            ]
            if self.stale:
                # The import succeeded against a payload the artifact was not
                # generated from. That is the whole point of --allow-stale, but
                # it is not something the command should keep to itself.
                lines.append(
                    f"  [yellow]--allow-stale:[/] artifact payload {self.artifact_sha[:12]}… "
                    f"differs from the database's {self.payload_sha[:12]}…; recorded in "
                    f"insight_run.error"
                )
        for headline, reason in self.drops:
            short = headline if len(headline) <= 60 else headline[:57] + "..."
            lines.append(f"  dropped: {short} -- {reason}")
        return "\n".join(lines)


def import_artifact(
    settings: Settings,
    path: Path,
    *,
    allow_stale: bool = False,
    allow_empty: bool = False,
) -> ImportOutcome:
    out = ImportOutcome(path=str(path))

    try:
        artifact = read_artifact(path)
    except OSError as exc:
        out.status, out.error = "failed", f"cannot read {path}: {exc}"
        return out
    except (ArtifactVersionError, ValueError) as exc:
        out.status, out.error = "failed", str(exc)
        return out

    out.model = artifact.model
    out.as_of = artifact.as_of.isoformat()
    out.read = len(artifact.insights)
    out.artifact_sha = artifact.prompt_sha256

    if out.read == 0 and not allow_empty:
        out.status = "refused"
        out.error = (
            "artifact contains no insights; importing it would delete this snapshot's "
            "insights and store nothing (pass --allow-empty)"
        )
        return out

    try:
        with connect(settings, autocommit=True) as conn:
            try:
                snapshot_id, as_of_date = latest_snapshot(conn, artifact.as_of)
            except LookupError:
                out.status = "failed"
                out.error = f"no snapshot loaded for {artifact.as_of}"
                return out
            out.snapshot_id = snapshot_id

            payload, sha = build_payload(conn, as_of_date)
            out.payload_sha = sha

            if sha != artifact.prompt_sha256:
                stale_msg = (
                    f"payload changed since generation: artifact {artifact.prompt_sha256[:12]}…, "
                    f"database {sha[:12]}… (regenerate, or pass --allow-stale)"
                )
                if not allow_stale:
                    out.status = "refused"
                    out.error = stale_msg
                    return out
                out.stale = True
                log.warning(stale_msg)

            property_codes, asset_keys = _known_targets(conn)

            allowed = collect_values(payload, set())
            kept: list[tuple[Insight, str]] = []
            for i in artifact.insights:
                base = Insight(**i.model_dump(exclude={"source_chunk"}))
                norm = normalise_target(base, property_codes=property_codes, asset_keys=asset_keys)
                ok, why = check_evidence(
                    norm, allowed, property_codes=property_codes, asset_keys=asset_keys
                )
                if ok:
                    kept.append((norm, sha))
                else:
                    out.dropped += 1
                    out.drops.append((i.headline, why or "dropped"))
                    log.warning("dropped %r: %s", i.headline, why)

            if not kept:
                out.status = "refused"
                out.error = "no insight in the artifact survived the evidence check"
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO core.insight_run
                             (snapshot_id, model, prompt_sha256, status, error, finished_at)
                           VALUES (%s,%s,%s,'refused',%s,now())""",
                        (snapshot_id, artifact.model, sha, out.error),
                    )
                return out

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM core.insight WHERE snapshot_id = %s", (snapshot_id,)
                )
                out.replaced = scalar(cur)
                cur.execute(
                    """INSERT INTO core.insight_run (snapshot_id, model, prompt_sha256, status)
                       VALUES (%s,%s,%s,'failed') RETURNING insight_run_id""",
                    (snapshot_id, artifact.model, sha),
                )
                insight_run_id = scalar(cur)

            with conn.transaction():
                out.inserted = _persist(
                    conn, snapshot_id, insight_run_id, artifact.model, kept, force=True
                )

            promote_error = (
                f"imported with --allow-stale from artifact payload {artifact.prompt_sha256[:12]}…"
                if out.stale else None
            )
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE core.insight_run SET status='succeeded', finished_at=now(), error=%s
                       WHERE insight_run_id = %s""",
                    (promote_error, insight_run_id),
                )
    except Exception as exc:  # noqa: BLE001 - database-phase failures must not crash the CLI
        out.status = "failed"
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    out.status = "succeeded"
    return out
