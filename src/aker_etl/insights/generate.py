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
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from ..config import Settings
from ..db import connect, scalar
from .context import (
    build_payload,
    canonical_json,
    estimate_tokens,
    latest_snapshot,
    map_chunks,
    reduce_chunk,
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


@dataclass
class GenerateOutcome:
    status: str = "succeeded"
    model: str = ""
    prompt_sha256: str = ""
    chunks: int = 0
    calls: int = 0
    insights_kept: int = 0
    insights_dropped: int = 0
    skipped_reason: str | None = None
    elapsed_s: float = 0.0
    dry_run_report: list[tuple[str, int, int]] = field(default_factory=list)
    error: str | None = None

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
        if self.status != "succeeded":
            return f"[red]insights {self.status}:[/] {self.error}"
        return (f"[green]{self.insights_kept} insight(s) stored[/] from {self.calls} call(s) "
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


def generate(
    settings: Settings,
    *,
    as_of: dt.date | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> GenerateOutcome:
    started = time.monotonic()
    out = GenerateOutcome(model=settings.aker_insight_model)

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
            for chunk in chunks:
                text = canonical_json(chunk)
                out.dry_run_report.append(
                    (f"map:{chunk['asset']['asset_key']}", estimate_tokens(text),
                     settings.aker_insight_num_ctx_map)
                )
            red = reduce_chunk(payload, [{"asset_key": c["asset"]["asset_key"],
                                          "headline": "<map pass output>"} for c in chunks])
            out.dry_run_report.append(
                ("reduce", estimate_tokens(canonical_json(red)), settings.aker_insight_num_ctx_reduce)
            )
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

        try:
            import ollama
        except ImportError as exc:
            _fail_run(conn, insight_run_id, f"ollama package not installed: {exc}")
            out.status, out.error = "failed", str(exc)
            out.skipped_reason = f"ollama package not installed ({exc})"
            return out

        client = ollama.Client(host=settings.ollama_host)
        try:
            client.list()
        except Exception as exc:  # noqa: BLE001 - unreachable Ollama is a warning, not a crash
            _fail_run(conn, insight_run_id, f"ollama unreachable at {settings.ollama_host}: {exc}")
            out.skipped_reason = (
                f"Ollama unreachable at {settings.ollama_host} -- the dashboard renders "
                f"fine without insights ({type(exc).__name__})"
            )
            return out

        property_codes, asset_keys = _known_targets(conn)

        kept: list[tuple[Insight, str]] = []
        dropped = 0
        headlines: list[dict] = []

        for chunk in chunks:
            name = f"map:{chunk['asset']['asset_key']}"
            batch, n_dropped, err = _call(
                client, settings, chunk, name,
                num_ctx=settings.aker_insight_num_ctx_map, think=False,
                property_codes=property_codes, asset_keys=asset_keys,
            )
            out.calls += 1
            dropped += n_dropped
            if err:
                log.warning("%s: %s", name, err)
                continue
            for insight in batch:
                kept.append((insight, sha))
                headlines.append({
                    "asset_key": chunk["asset"]["asset_key"],
                    "headline": insight.headline,
                    "priority": insight.priority,
                    "category": insight.category,
                })

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
        dropped += n_dropped
        if err:
            log.warning("reduce: %s", err)
        else:
            kept.extend((insight, sha) for insight in batch)

        _persist(conn, snapshot_id, insight_run_id, settings.aker_insight_model, kept, force=True)
        out.insights_kept = len(kept)
        out.insights_dropped = dropped
        out.status = "succeeded" if kept else "refused"

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE core.insight_run SET status = %s, finished_at = now(), error = %s
                   WHERE insight_run_id = %s""",
                (out.status, None if kept else "no insight survived the evidence check",
                 insight_run_id),
            )

    out.elapsed_s = time.monotonic() - started
    return out


def _known_targets(conn) -> tuple[frozenset[str], frozenset[str]]:
    """Every property code and asset key that exists, for the target check."""
    with conn.cursor() as cur:
        cur.execute("SELECT property_code::text, asset_key FROM core.property")
        rows = cur.fetchall()
    return frozenset(c for c, _ in rows), frozenset(a for _, a in rows)


def _call(client, settings: Settings, chunk: dict, name: str, *, num_ctx: int, think: bool,
          property_codes: frozenset[str] = frozenset(),
          asset_keys: frozenset[str] = frozenset()):
    """One model call. Returns (kept insights, dropped count, error)."""
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
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
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
            insight = normalise_target(
                raw_insight, property_codes=property_codes, asset_keys=asset_keys
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
             kept: list[tuple[Insight, str]], *, force: bool) -> None:
    from psycopg.types.json import Jsonb

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


def _fail_run(conn, insight_run_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE core.insight_run SET status='failed', error=%s, finished_at=now()
               WHERE insight_run_id = %s""",
            (error, insight_run_id),
        )
    log.warning(error)
