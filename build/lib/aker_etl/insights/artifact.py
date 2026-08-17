"""The generate -> import file contract. PLAN3 section 4.

An artifact is one generation run's output, frozen. It is deliberately
self-describing: `import` must be able to work out which snapshot the insights
belong to, and whether the figures they cite are still the figures the database
holds, without being told either on the command line.

Every field except `insights` is diagnostic. The gate that decides what reaches
core.insight runs at import time against the live database, not against anything
asserted in here -- see store.import_artifact.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schema import Insight

ARTIFACT_VERSION = 1


class ArtifactVersionError(ValueError):
    """The file is a valid artifact of a version this build does not read."""


class ArtifactGenerator(BaseModel):
    ollama_host: str
    num_ctx_map: int
    num_ctx_reduce: int
    num_predict: int
    positioning: bool
    seed: int


class ArtifactStats(BaseModel):
    chunks: int = 0
    calls: int = 0
    map_calls: int = 0
    positioning_calls: int = 0
    reduce_calls: int = 0
    insights_kept: int = 0
    insights_dropped: int = 0
    elapsed_s: float = 0.0


class ArtifactInsight(Insight):
    model_config = ConfigDict(extra="forbid")

    # Audit trail only. PLAN3 D6: the import gate deliberately does not use it,
    # so a hand-written artifact that omits it still imports.
    source_chunk: str | None = None


class InsightArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_version: int = ARTIFACT_VERSION
    as_of: dt.date
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str
    generated_at: dt.datetime
    payload_source: Literal["database", "file"]
    generator: ArtifactGenerator | None = None
    stats: ArtifactStats = Field(default_factory=ArtifactStats)
    insights: list[ArtifactInsight] = Field(default_factory=list)


def write_artifact(artifact: InsightArtifact, path: Path) -> int:
    """Write atomically and return the byte count.

    tmp + os.replace: a half-written artifact that still parses is worse than no
    artifact, because `import` would accept it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = artifact.model_dump_json(indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return len(body.encode("utf-8"))


def read_artifact(path: Path) -> InsightArtifact:
    """Parse and version-check. Raises ArtifactVersionError, ValueError, OSError."""
    raw = Path(path).read_text(encoding="utf-8")
    probe = json.loads(raw)  # raises ValueError on malformed JSON
    if not isinstance(probe, dict):
        raise ValueError("artifact root must be a JSON object")
    version = probe.get("artifact_version")
    if version != ARTIFACT_VERSION:
        raise ArtifactVersionError(
            f"artifact_version {version!r}, this build reads {ARTIFACT_VERSION}"
        )
    return InsightArtifact.model_validate(probe)
