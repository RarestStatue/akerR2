from __future__ import annotations

import logging

from rich.logging import RichHandler


def configure(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=False)],
        force=True,
    )
    logging.getLogger("openpyxl").setLevel(logging.WARNING)
