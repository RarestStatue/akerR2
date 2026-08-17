"""Where in the payload did this figure come from?

collect_values() flattens the payload to a set of normalised scalars, which is all
the evidence gate needs -- it only asks "is this value present?". Provenance asks
the harder question "present *where*?", so this walks the same structure and keeps
the path. It reuses generate._normalise, so the two can never disagree about what
counts as the same number.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .generate import _normalise


def walk_paths(obj: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """Yield (json_path, normalised_value) for every scalar in the payload."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk_paths(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            yield from walk_paths(value, f"{path}[{i}]")
    elif obj is not None and not isinstance(obj, bool):
        yield path, _normalise(obj)


def find_paths(payload: dict, value: str, *, limit: int = 12) -> list[str]:
    """Every path whose normalised scalar equals the normalised `value`."""
    target = _normalise(value)
    out: list[str] = []
    for path, scalar in walk_paths(payload):
        if scalar == target:
            out.append(path)
            if len(out) >= limit:
                break
    return out
