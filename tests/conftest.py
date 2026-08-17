from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "Aker Case Study Data"
RENT_ROLL_DIR = DATA_DIR / "Rent_Roll_with_Lease_Charges"
AVAILABILITY_DIR = DATA_DIR / "Unit_Availability"


def rent_roll_path(code: str) -> Path:
    return RENT_ROLL_DIR / f"ResAnalytics_Rent_Roll_with_Lease_Charges_{code}.xlsx"


def availability_path(code: str) -> Path:
    return AVAILABILITY_DIR / f"ResAnalytics_Unit_Availability_{code}.xlsx"


@pytest.fixture(scope="session")
def rent_roll_files() -> list[Path]:
    return sorted(RENT_ROLL_DIR.glob("*.xlsx"))


@pytest.fixture(scope="session")
def availability_files() -> list[Path]:
    return sorted(AVAILABILITY_DIR.glob("*.xlsx"))


@pytest.fixture(scope="session")
def all_rent_rolls(rent_roll_files):
    from aker_etl.parsers import parse_rent_roll

    return [parse_rent_roll(p) for p in rent_roll_files]


@pytest.fixture(scope="session")
def all_availability(availability_files):
    from aker_etl.parsers import parse_availability

    return [parse_availability(p) for p in availability_files]


@pytest.fixture(scope="session")
def rr_115r():
    from aker_etl.parsers import parse_rent_roll

    return parse_rent_roll(rent_roll_path("115r"))


def write_rent_roll(path: Path, detail_rows: list[list[object]]) -> Path:
    """A minimal but structurally valid rent roll, plus the given detail rows.

    parse_rent_roll validates rows 1-6 strictly (title, '<name> (<code>)',
    'As Of = ...', 'Month Year = ...', two header rows), so a synthetic workbook
    has to reproduce them exactly or every test fails as a structural error
    instead of exercising the row it was written for.
    """
    import openpyxl

    from aker_etl.parsers.rent_roll import HEADER_ROW_1, HEADER_ROW_2, TITLE

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report1"
    ws.append([TITLE])
    ws.append(["Synthetic Property (test1r)"])
    ws.append(["As Of = 02/25/2026"])
    ws.append(["Month Year = 02/2026"])
    ws.append(list(HEADER_ROW_1))
    ws.append(list(HEADER_ROW_2))
    for row in detail_rows:
        ws.append(row)
    wb.save(path)
    return path
