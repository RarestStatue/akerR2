"""Ollama call + evidence gate + persistence. PLAN.md 6.3 / 6.5.

Two rules the rest of this module exists to enforce:

1. The model never computes a number. SQL computed every metric; ranking and
   sorting happened in context.py. The model reads finished figures and names
   what is notable.
2. Schema-constrained decoding guarantees shape, not truth. Every evidence value
   is compared against the chunk it came from, and an insight citing a figure
   that is not in its own input is dropped. A missing insight is fine; a
   fabricated one is not.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from ..config import Settings
from ..db import connect, scalar
from .artifact import ArtifactGenerator, ArtifactInsight, ArtifactStats, InsightArtifact, write_artifact
from .context import (
    build_payload,
    canonical_json,
    estimate_tokens,
    latest_snapshot,
    map_chunks,
    payload_sha256,
    positioning_chunks,
    reduce_chunk,
    targets_from_payload,
)
from .schema import Insight, InsightBatch, normalise_target, scope_target_ok, target_is_known

log = logging.getLogger(__name__)

CTX_HEADROOM = 1.3

# A full batch is 6 insights x (120-char headline + 600-char detail + evidence),
# roughly 1,800 tokens. The old 1,024 truncated the reply mid-object, and a
# truncated object has no closing brace, so `extract_json` returned nothing and
# the whole chunk was logged as "response contained no JSON object".
NUM_PREDICT = 3072

SYSTEM_PROMPT = """You are an asset-management analyst reading a multifamily rent roll summary.

The JSON you are given contains FINISHED figures. Never calculate, re-derive, sum,
rank, or estimate anything. Every number you mention must be copied verbatim from
the JSON.

Write short, concrete observations an asset manager would act on: occupancy
outliers, concession load, lease expiration clustering, negative balances,
unit-mix effects, data-quality gaps. Skip anything unremarkable -- returning
fewer insights is better than padding. Return at most six insights.

For each insight cite the figures you relied on in `evidence` -- at most four
entries -- copying each value exactly as it appears in the JSON. Do not mention a
number in `detail` that is not also in `evidence`.

Reply with a single JSON object and nothing else -- no prose, no markdown fence,
no explanation before or after. It must match this schema exactly:

{"insights": [{"scope": "portfolio" | "asset" | "property",
               "property_code": string or null, "asset_key": string or null,
               "category": "occupancy" | "revenue" | "concession" | "expiration" |
                           "delinquency" | "unit_mix" | "data_quality" | "trend",
               "priority": "low" | "medium" | "high",
               "headline": string (8-120 chars),
               "detail": string (10-600 chars),
               "evidence": [{"metric": string, "value": string,
                             "comparison": string or null}]}]}

Set `property_code` only when scope is "property" and `asset_key` only when scope
is "asset"; otherwise use null. If nothing is worth reporting, reply {"insights": []}."""

POSITIONING_PROMPT = """You are an asset-management analyst reading one property's position on a
profitability matrix.

The X axis is revenue capture: billed lease charges as a share of gross potential
rent. The Y axis is physical occupancy. The two thresholds split the portfolio
into four quadrants:

  performing  - capture and occupancy both at or above threshold
  leaking     - occupancy at threshold, capture below it: the property is full but
                revenue is lost to pricing, concessions or collections
  vacancy_led - capture at threshold, occupancy below it: pricing and billing are
                sound, the loss is empty units
  distressed  - both below threshold

The JSON you are given contains FINISHED figures for exactly one property. Never
calculate, re-derive, sum, rank or estimate anything. Every number you mention
must be copied verbatim from the JSON.

`charges_to_threshold` is the additional monthly billed revenue that would move
this property across the capture line. `units_to_threshold` is the number of
additional leased units that would move it across the occupancy line. A zero
means that line is already crossed.

Write ONE insight naming the concrete lever that would move this property to a
better quadrant, or - if it is already `performing` - the lever that would defend
its position. Be specific: name whether the gap is concessions, loss to lease,
uncollected balances, ancillary revenue, or vacant and notice units, and say so
using the figures supplied. Do not recommend anything the data does not support,
and never mention operating expenses, NOI, cap rate or valuation - the source
data contains none of them.

Return at most one insight. Reply with a single JSON object and nothing else --
no prose, no markdown fence, no explanation before or after. It must match this
schema exactly:

{"insights": [{"scope": "property", "property_code": string, "asset_key": null,
               "category": "positioning", "priority": "low" | "medium" | "high",
               "headline": string (8-120 chars), "detail": string (10-600 chars),
               "evidence": [{"metric": string, "value": string,
                             "comparison": string or null}]}]}

Set "scope" to "property" and "property_code" to the code in the input. If
nothing is worth reporting, reply {"insights": []}."""

# Priority is derived from the quadrant, in code -- never asked of the model.
# Consistent with the rule that ranking is an ORDER BY, not a judgment call.
QUADRANT_PRIORITY = {
    "distressed": "high",
    "leaking": "medium",
    "vacancy_led": "medium",
    "performing": "low",
}


@dataclass
class GenerateOutcome:
    status: str = "succeeded"
    model: str = ""
    prompt_sha256: str = ""
    chunks: int = 0
    calls: int = 0
    map_calls: int = 0
    positioning_calls: int = 0
    reduce_calls: int = 0
    insights_kept: int = 0
    insights_dropped: int = 0
    skipped_reason: str | None = None
    elapsed_s: float = 0.0
    dry_run_report: list[tuple[str, int, int]] = field(default_factory=list)
    error: str | None = None
    artifact_path: str = ""
    artifact_bytes: int = 0

    def render(self) -> str:
        if self.skipped_reason:
            return f"[yellow]insights skipped:[/] {self.skipped_reason}"
        if self.dry_run_report:
            lines = ["[bold]dry run -- no inference performed[/]",
                     f"payload sha256 {self.prompt_sha256[:16]}…",
                     f"{'chunk':<28}{'tokens':>8}{'num_ctx':>10}"]
            for name, tokens, ctx in self.dry_run_report:
                flag = "" if ctx >= tokens * CTX_HEADROOM else "   <-- TOO SMALL"
                lines.append(f"{name:<28}{tokens:>8}{ctx:>10}{flag}")
            return "\n".join(lines)
        if self.artifact_path:
            if self.status == "refused":
                return (f"[yellow]no insight survived the evidence check; "
                        f"wrote {self.artifact_path} with 0 insights[/]")
            kb = self.artifact_bytes / 1024
            return (f"[green]wrote {self.artifact_path}[/] ({self.insights_kept} insight(s), "
                    f"{kb:.1f} KB, payload sha256 {self.prompt_sha256[:12]}…) -- "
                    f"import with: aker-etl insights import {self.artifact_path}")
        if self.status != "succeeded":
            return f"[red]insights {self.status}:[/] {self.error}"
        return (f"[green]{self.insights_kept} insight(s) stored[/] from {self.map_calls} map + "
                f"{self.positioning_calls} positioning + {self.reduce_calls} reduce call(s) "
                f"with {self.model} ({self.insights_dropped} dropped by the evidence check) "
                f"in {self.elapsed_s:.1f}s")


# --------------------------------------------------------------------------- #
# Evidence gate
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"^-?\$?\(?-?[\d,]*\.?\d+\)?%?$")


def _normalise(value: Any) -> str:
    """Compare 96.00 / '96.0' / '96' / '96%' / '$96.00' as the same figure."""
    s = str(value).strip()
    if not s:
        return ""
    if _NUM_RE.match(s):
        negative = s.startswith("(") and s.endswith(")")
        cleaned = s.strip("()").replace(",", "").replace("$", "").replace("%", "")
        try:
            d = Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return s.casefold()
        if negative:
            d = -d
        d = d.normalize()
        return format(d, "f")
    return s.casefold()


def collect_values(obj: Any, into: set[str]) -> set[str]:
    """Every scalar in the chunk, normalised. The permitted vocabulary of figures."""
    if isinstance(obj, dict):
        for v in obj.values():
            collect_values(v, into)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            collect_values(v, into)
    elif obj is not None and not isinstance(obj, bool):
        into.add(_normalise(obj))
    return into


def extract_json(content: str) -> str | None:
    """Pull the JSON object out of a reply.

    `format=<schema>` asks Ollama for grammar-constrained decoding, but not every
    server build honours it -- Ollama 0.18.3 ignores it (and plain JSON mode) for
    qwen3.5, returning prose. The schema is therefore also stated in the system
    prompt, and this recovers the object from a fenced or prose-wrapped reply.
    Shape is still enforced by pydantic afterwards, so a miss fails closed.
    """
    if not content:
        return None
    text = content.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return fence.group(1)
    # Fall back to the outermost balanced object.
    start = text.find("{")
    if start == -1:
        return None
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def check_evidence(
    insight: Insight,
    allowed: set[str],
    *,
    property_codes: frozenset[str] = frozenset(),
    asset_keys: frozenset[str] = frozenset(),
) -> tuple[bool, str | None]:
    if not scope_target_ok(insight):
        return False, "scope/target mismatch"
    if not target_is_known(insight, property_codes, asset_keys):
        target = insight.property_code or insight.asset_key
        return False, f"{insight.scope} target {target!r} does not exist"
    for ev in insight.evidence:
        if _normalise(ev.value) not in allowed:
            return False, f"evidence value {ev.value!r} is not in the payload"
    return True, None


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


@dataclass
class InferenceResult:
    """The output of the three inference passes, with no database handle involved.

    `chunks[i]` is the source-chunk label for `insights[i]`; the two lists stay
    index-aligned so a caller that wants to build an artifact can zip them.
    """

    insights: list[Insight] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)
    calls: int = 0
    map_calls: int = 0
    positioning_calls: int = 0
    reduce_calls: int = 0
    dropped: int = 0
    unreachable_reason: str | None = None  # user-facing message; set iff Ollama could not be used
    fail_run_error: str | None = None      # the same failure, phrased for core.insight_run.error
    error_detail: str | None = None        # bare exception text; set only when import_failed
    import_failed: bool = False            # True iff the `ollama` package itself is missing


def run_inference(
    settings: Settings,
    payload: dict,
    *,
    property_codes: frozenset[str],
    asset_keys: frozenset[str],
    chunks: list[dict] | None = None,
) -> InferenceResult:
    """Map + positioning + reduce passes. No database handle anywhere in here.

    Extracted from generate() so the same three passes serve both the direct-to-
    database route and the write-an-artifact route. Ollama being unreachable is
    reported through `unreachable_reason` rather than raised: the caller decides
    whether that is a warning (route A, the dashboard renders fine without
    insights) or an error (route B, the user asked for a file and got none).

    `chunks` is the caller's `map_chunks(payload)` if it already has one -- both
    routes need the count for their own reporting, and map_chunks is pure, so
    recomputing it here would be the same list built twice.
    """
    out = InferenceResult()

    try:
        import ollama
    except ImportError as exc:
        out.fail_run_error = f"ollama package not installed: {exc}"
        out.unreachable_reason = f"ollama package not installed ({exc})"
        out.error_detail = str(exc)
        out.import_failed = True
        return out

    client = ollama.Client(host=settings.ollama_host)
    try:
        client.list()
    except Exception as exc:  # noqa: BLE001 - unreachable Ollama is a warning, not a crash
        out.fail_run_error = f"ollama unreachable at {settings.ollama_host}: {exc}"
        out.unreachable_reason = (
            f"Ollama unreachable at {settings.ollama_host} -- the dashboard renders "
            f"fine without insights ({type(exc).__name__})"
        )
        return out

    if chunks is None:
        chunks = map_chunks(payload)
    headlines: list[dict] = []

    for chunk in chunks:
        name = f"map:{chunk['asset']['asset_key']}"
        batch, n_dropped, err = _call(
            client, settings, chunk, name,
            num_ctx=settings.aker_insight_num_ctx_map, think=False,
            property_codes=property_codes, asset_keys=asset_keys,
        )
        out.calls += 1
        out.map_calls += 1
        out.dropped += n_dropped
        if err:
            log.warning("%s: %s", name, err)
            continue
        for insight in batch:
            out.insights.append(insight)
            out.chunks.append(name)
            headlines.append({
                "asset_key": chunk["asset"]["asset_key"],
                "headline": insight.headline,
                "priority": insight.priority,
                "category": insight.category,
            })

    if settings.aker_insight_positioning:
        pos_kept, pos_calls, pos_dropped = _positioning_pass(
            client, settings, payload, property_codes, asset_keys
        )
        for insight, name in pos_kept:
            out.insights.append(insight)
            out.chunks.append(name)
        out.calls += pos_calls
        out.positioning_calls += pos_calls
        out.dropped += pos_dropped

    red = reduce_chunk(payload, headlines)
    # think=False here too. With thinking on, qwen3.5:4b spends the entire
    # generation budget reasoning and returns an empty `content` -- measured
    # at 2,405 chars of `thinking` and 0 of content at num_predict=1024, and
    # 8,424/0 at 3,072. The reduce pass is a ranking job over figures that
    # are already finished, so it loses nothing by answering directly.
    batch, n_dropped, err = _call(
        client, settings, red, "reduce",
        num_ctx=settings.aker_insight_num_ctx_reduce, think=False,
        property_codes=property_codes, asset_keys=asset_keys,
    )
    out.calls += 1
    out.reduce_calls += 1
    out.dropped += n_dropped
    if err:
        log.warning("reduce: %s", err)
    else:
        for insight in batch:
            out.insights.append(insight)
            out.chunks.append("reduce")

    return out


def _dry_run_report(
    settings: Settings, payload: dict, chunks: list[dict] | None = None
) -> list[tuple[str, int, int]]:
    """Per-chunk token counts, shared by generate()'s and generate_to_file()'s --dry-run."""
    report: list[tuple[str, int, int]] = []
    if chunks is None:
        chunks = map_chunks(payload)
    for chunk in chunks:
        text = canonical_json(chunk)
        report.append(
            (f"map:{chunk['asset']['asset_key']}", estimate_tokens(text),
             settings.aker_insight_num_ctx_map)
        )
    if settings.aker_insight_positioning:
        for chunk in positioning_chunks(payload):
            report.append(
                (f"positioning:{chunk['property']['property_code']}",
                 estimate_tokens(canonical_json(chunk)),
                 settings.aker_insight_num_ctx_map)
            )
    red = reduce_chunk(payload, [{"asset_key": c["asset"]["asset_key"],
                                  "headline": "<map pass output>"} for c in chunks])
    report.append(
        ("reduce", estimate_tokens(canonical_json(red)), settings.aker_insight_num_ctx_reduce)
    )
    return report


def generate(
    settings: Settings,
    *,
    as_of: dt.date | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> GenerateOutcome:
    started = time.monotonic()
    out = GenerateOutcome(model=settings.aker_insight_model)
    try:
        if not settings.aker_insight_enabled and not dry_run:
            out.skipped_reason = "AKER_INSIGHT_ENABLED=false"
            return out

        with connect(settings, autocommit=True) as conn:
            snapshot_id, as_of_date = latest_snapshot(conn, as_of)
            payload, sha = build_payload(conn, as_of_date)
            out.prompt_sha256 = sha

            chunks = map_chunks(payload)
            out.chunks = len(chunks)

            if dry_run:
                out.dry_run_report = _dry_run_report(settings, payload, chunks)
                return out

            # Idempotency: identical payload + identical model tag = nothing to do.
            if not force:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT 1 FROM core.insight_run
                           WHERE snapshot_id = %s AND prompt_sha256 = %s AND model = %s
                             AND status = 'succeeded' LIMIT 1""",
                        (snapshot_id, sha, settings.aker_insight_model),
                    )
                    if cur.fetchone():
                        out.skipped_reason = (
                            "payload and model unchanged since the last successful run "
                            "(pass --force to regenerate)"
                        )
                        return out

            with conn.cursor() as cur:
                cur.execute(
                    # Column order is (model, prompt_sha256). These were passed the
                    # other way round, which never raised -- `prompt_sha256` is
                    # char(64), so a model tag just got space-padded into it -- but it
                    # meant the idempotency guard below could never match its own
                    # rows, and every run regenerated as if --force had been passed.
                    """INSERT INTO core.insight_run (snapshot_id, model, prompt_sha256, status)
                       VALUES (%s,%s,%s,'failed') RETURNING insight_run_id""",
                    (snapshot_id, settings.aker_insight_model, sha),
                )
                insight_run_id = scalar(cur)

            property_codes, asset_keys = _known_targets(conn)
            res = run_inference(
                settings, payload, property_codes=property_codes, asset_keys=asset_keys, chunks=chunks
            )

            if res.unreachable_reason:
                _fail_run(conn, insight_run_id, res.fail_run_error or res.unreachable_reason)
                out.skipped_reason = res.unreachable_reason
                if res.import_failed:
                    out.status, out.error = "failed", res.error_detail
                return out

            kept = [(insight, sha) for insight in res.insights]

            out.insights_kept = _persist(
                conn, snapshot_id, insight_run_id, settings.aker_insight_model, kept, force=True)
            out.insights_dropped = res.dropped
            out.calls = res.calls
            out.map_calls = res.map_calls
            out.positioning_calls = res.positioning_calls
            out.reduce_calls = res.reduce_calls
            out.status = "succeeded" if out.insights_kept else "refused"

            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE core.insight_run SET status = %s, finished_at = now(), error = %s
                       WHERE insight_run_id = %s""",
                    (out.status,
                     None if out.insights_kept else
                     "no insight survived the evidence check, or none named a known property",
                     insight_run_id),
                )
    finally:
        # In a finally: five of the six exits from this function are early
        # returns inside the `with connect(...)` block, and all five used to
        # report 0.0s.
        out.elapsed_s = time.monotonic() - started
    return out


def generate_to_file(
    settings: Settings,
    out_path: Path,
    *,
    as_of: dt.date | None = None,
    payload_file: Path | None = None,
    dry_run: bool = False,
) -> GenerateOutcome:
    """Route B: inference to a self-describing JSON artifact, no persistence. PLAN3 5.3.3."""
    started = time.monotonic()
    out = GenerateOutcome(model=settings.aker_insight_model)

    if not settings.aker_insight_enabled and not dry_run:
        out.skipped_reason = "AKER_INSIGHT_ENABLED=false"
        return out

    if payload_file is not None:
        try:
            payload = json.loads(payload_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("root is not a JSON object")
            as_of_date = dt.date.fromisoformat(payload["as_of"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{payload_file} is not an aker-etl payload: {exc}") from exc
        sha = payload_sha256(payload)
        property_codes, asset_keys = targets_from_payload(payload)
        source: Literal["database", "file"] = "file"
    else:
        with connect(settings, autocommit=True) as conn:
            _snapshot_id, as_of_date = latest_snapshot(conn, as_of)
            payload, sha = build_payload(conn, as_of_date)
            property_codes, asset_keys = _known_targets(conn)
        source = "database"

    out.prompt_sha256 = sha
    chunks = map_chunks(payload)
    out.chunks = len(chunks)

    if dry_run:
        out.dry_run_report = _dry_run_report(settings, payload, chunks)
        return out

    res = run_inference(
        settings, payload, property_codes=property_codes, asset_keys=asset_keys, chunks=chunks
    )

    if res.unreachable_reason:
        out.status = "failed"
        out.error = res.unreachable_reason
        return out

    artifact = InsightArtifact(
        as_of=as_of_date,
        prompt_sha256=sha,
        model=settings.aker_insight_model,
        generated_at=dt.datetime.now(dt.timezone.utc),
        payload_source=source,
        generator=ArtifactGenerator(
            ollama_host=settings.ollama_host,
            num_ctx_map=settings.aker_insight_num_ctx_map,
            num_ctx_reduce=settings.aker_insight_num_ctx_reduce,
            num_predict=NUM_PREDICT,
            positioning=settings.aker_insight_positioning,
            seed=7,
        ),
        stats=ArtifactStats(
            chunks=out.chunks,
            calls=res.calls,
            map_calls=res.map_calls,
            positioning_calls=res.positioning_calls,
            reduce_calls=res.reduce_calls,
            insights_kept=len(res.insights),
            insights_dropped=res.dropped,
            elapsed_s=time.monotonic() - started,
        ),
        insights=[
            ArtifactInsight(**i.model_dump(), source_chunk=c)
            for i, c in zip(res.insights, res.chunks, strict=True)
        ],
    )
    n_bytes = write_artifact(artifact, out_path)

    out.insights_kept = len(res.insights)
    out.insights_dropped = res.dropped
    out.calls = res.calls
    out.map_calls = res.map_calls
    out.positioning_calls = res.positioning_calls
    out.reduce_calls = res.reduce_calls
    out.status = "succeeded" if res.insights else "refused"
    out.elapsed_s = time.monotonic() - started
    out.artifact_path = str(out_path)
    out.artifact_bytes = n_bytes
    return out


def _known_targets(conn) -> tuple[frozenset[str], frozenset[str]]:
    """Every property code and asset key that exists, for the target check."""
    with conn.cursor() as cur:
        cur.execute("SELECT property_code::text, asset_key FROM core.property")
        rows = cur.fetchall()
    return frozenset(c for c, _ in rows), frozenset(a for _, a in rows)


def _positioning_pass(
    client, settings: Settings, payload: dict,
    property_codes: frozenset[str], asset_keys: frozenset[str],
) -> tuple[list[tuple[Insight, str]], int, int]:
    """One model call per plottable property: quadrant-movement advice.

    Reuses `_call()`, the same evidence gate as the map/reduce passes. The
    fields scope/property_code/asset_key/category/priority are forced by a
    `rewrite` hook run *before* the gate -- the chunk is one property, so
    these are facts, not judgements, and forcing them afterwards would be too
    late: check_evidence would already have dropped a mis-scoped reply before
    the caller ever saw it.
    Returns (kept insights with their source-chunk label, calls made, dropped count).
    """
    kept: list[tuple[Insight, str]] = []
    calls = 0
    dropped = 0
    for chunk in positioning_chunks(payload):
        code = chunk["property"]["property_code"]
        quadrant = chunk["property"].get("quadrant")
        name = f"positioning:{code}"

        def force(insight: Insight, code: str = code, quadrant: str | None = quadrant) -> Insight:
            # The chunk is exactly one property, so scope, target and category are
            # facts about the input, not judgements the model gets to make.
            # Priority follows the quadrant, in code -- ranking is never the
            # model's job here, same rule as the ORDER BY in the mart views.
            return insight.model_copy(update={
                "scope": "property", "property_code": code, "asset_key": None,
                "category": "positioning",
                "priority": (QUADRANT_PRIORITY.get(quadrant, insight.priority)
                             if quadrant is not None else insight.priority),
            })

        batch, n_dropped, err = _call(
            client, settings, chunk, name,
            num_ctx=settings.aker_insight_num_ctx_map, think=False,
            property_codes=property_codes, asset_keys=asset_keys,
            system=POSITIONING_PROMPT, rewrite=force,
        )
        calls += 1
        dropped += n_dropped
        if err:
            log.warning("%s: %s", name, err)
            continue
        # Keep at most one insight per property: the first that survived the
        # gate; extras are dropped and counted, not silently discarded.
        if len(batch) > 1:
            dropped += len(batch) - 1
            batch = batch[:1]
        for insight in batch:
            kept.append((insight, name))
    return kept, calls, dropped


def _call(client, settings: Settings, chunk: dict, name: str, *, num_ctx: int, think: bool,
          property_codes: frozenset[str] = frozenset(),
          asset_keys: frozenset[str] = frozenset(),
          system: str = SYSTEM_PROMPT,
          rewrite: Callable[[Insight], Insight] | None = None):
    """One model call. Returns (kept insights, dropped count, error).

    `rewrite` runs on every parsed insight before the evidence gate. The
    positioning pass uses it to force the fields the chunk already determines --
    forcing them afterwards would be too late, because check_evidence drops a
    mis-scoped reply before the caller ever sees it.
    """
    text = canonical_json(chunk)
    tokens = estimate_tokens(text)
    # Refuse rather than truncate. Ollama silently drops overflow, and a model
    # commenting confidently on data it never saw is the worst failure here.
    if num_ctx < tokens * CTX_HEADROOM:
        return [], 0, (f"chunk needs num_ctx >= {int(tokens * CTX_HEADROOM)} "
                       f"(estimated {tokens} tokens), configured {num_ctx}")

    allowed = collect_values(chunk, set())

    for attempt, temperature in enumerate((0.2, 0.0)):
        try:
            resp = client.chat(
                model=settings.aker_insight_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": text}],
                format=InsightBatch.model_json_schema(),
                think=think,
                options={
                    "num_ctx": num_ctx,
                    "temperature": temperature,
                    "top_p": 0.9,
                    "presence_penalty": 0.0,   # the shipped default of 1.5 fights JSON
                    "num_predict": NUM_PREDICT,
                    "seed": 7,
                },
                keep_alive="10m",
            )
        except Exception as exc:  # noqa: BLE001
            return [], 0, f"{type(exc).__name__}: {exc}"

        raw = extract_json(resp["message"]["content"])
        if raw is None:
            if attempt == 0:
                continue
            return [], 0, "response contained no JSON object"
        try:
            batch = InsightBatch.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                continue
            return [], 0, f"unparseable response: {exc}"

        kept, dropped = [], 0
        for raw_insight in batch.insights:
            insight = rewrite(raw_insight) if rewrite else raw_insight
            insight = normalise_target(
                insight, property_codes=property_codes, asset_keys=asset_keys
            )
            ok, why = check_evidence(
                insight, allowed, property_codes=property_codes, asset_keys=asset_keys
            )
            if ok:
                kept.append(insight)
            else:
                dropped += 1
                log.debug("%s dropped an insight: %s", name, why)

        # More than half rejected: retry once at temperature 0, then give up.
        if batch.insights and dropped > len(batch.insights) / 2 and attempt == 0:
            continue
        return kept, dropped, None

    return [], 0, "failed both attempts"


def _persist(conn, snapshot_id: int, insight_run_id: int, model: str,
             kept: list[tuple[Insight, str]], *, force: bool) -> int:
    from psycopg.types.json import Jsonb

    written = 0
    with conn.cursor() as cur:
        cur.execute("SELECT property_code::text, property_id FROM core.property")
        pids = {code: pid for code, pid in cur.fetchall()}
        if force:
            # A regeneration replaces the snapshot's insights wholesale; a partial
            # overwrite would leave a mix of two runs on the dashboard.
            cur.execute("DELETE FROM core.insight WHERE snapshot_id = %s", (snapshot_id,))
        for insight, sha in kept:
            property_id = pids.get(insight.property_code) if insight.property_code else None
            if insight.scope == "property" and property_id is None:
                continue
            cur.execute(
                """INSERT INTO core.insight
                     (snapshot_id, scope, property_id, asset_key, category, priority,
                      headline, detail, evidence, model, prompt_sha256)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (snapshot_id, insight.scope, property_id,
                 insight.asset_key if insight.scope == "asset" else None,
                 insight.category, insight.priority, insight.headline, insight.detail,
                 Jsonb([e.model_dump(exclude_none=True) for e in insight.evidence]),
                 model, sha),
            )
            written += 1
    return written


def _fail_run(conn, insight_run_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE core.insight_run SET status='failed', error=%s, finished_at=now()
               WHERE insight_run_id = %s""",
            (error, insight_run_id),
        )
    log.warning(error)
