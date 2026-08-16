"""Row models and the shared value coercers.

Every value read out of a workbook goes through the coercers in this module.
Two paths reach them: detail rows, where openpyxl hands back int/float/datetime,
and the summary blocks, where money arrives as comma-formatted strings
('260,778.00'). One coercer per type keeps both honest.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Severity = Literal["info", "warning", "error"]
Section = Literal["current", "future"]
Status = Literal["occupied", "notice", "vacant", "model", "down", "future"]

SENTINEL_RESIDENTS = {"VACANT", "MODEL", "DOWN"}
_STRIP = str.maketrans("", "", ",$%")


class CoercionError(ValueError):
    """A cell could not be coerced. Callers turn this into a ParsedIssue."""


def as_text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def as_decimal(v: Any) -> Decimal:
    """Money/percent -> Decimal. Never Decimal(float): that carries binary error in."""
    if v is None or v == "":
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):
        raise CoercionError(f"bool is not a number: {v!r}")
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip().translate(_STRIP)
        negative = s.startswith("(") and s.endswith(")")
        if negative:
            s = s[1:-1]
        s = s.strip()
        if not s:
            return Decimal(0)
        try:
            d = Decimal(s)
        except InvalidOperation as exc:
            raise CoercionError(f"not a number: {v!r}") from exc
        return -d if negative else d
    raise CoercionError(f"unsupported money type {type(v).__name__}: {v!r}")


def as_date(v: Any) -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return dt.datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        raise CoercionError(f"not a date: {v!r}")
    raise CoercionError(f"unsupported date type {type(v).__name__}: {v!r}")


def as_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    d = as_decimal(v)
    if d != d.to_integral_value():
        raise CoercionError(f"not an integer: {v!r}")
    return int(d)


# --------------------------------------------------------------------------- #
# Parsed rows. Plain pydantic models so a ProcessPoolExecutor worker can return
# them without a DB handle crossing the process boundary.
# --------------------------------------------------------------------------- #


class ParsedIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    severity: Severity
    rule: str
    sheet_row: int | None = None
    detail: dict[str, Any] = {}


class ParsedCharge(BaseModel):
    line_no: int
    charge_code: str
    amount: Decimal
    sheet_row: int


class ParsedLease(BaseModel):
    unit_code: str
    unit_type_code: str | None
    unit_sqft: int | None
    resident_id: str | None          # None for the VACANT/MODEL/DOWN sentinels
    resident_name: str | None
    sentinel: str | None             # the sentinel text, when there was one
    section: Section
    occupancy_status: Status
    market_rent: Decimal
    resident_deposit: Decimal
    other_deposit: Decimal
    balance: Decimal
    charges_total: Decimal
    move_in: dt.date | None
    lease_expiration: dt.date | None
    move_out: dt.date | None
    sheet_row: int
    charges: list[ParsedCharge] = []


class ParsedSummaryGroup(BaseModel):
    group_label: str
    square_footage: Decimal | None
    market_rent: Decimal | None
    lease_charges: Decimal | None
    security_deposit: Decimal | None
    other_deposits: Decimal | None
    unit_count: int | None
    pct_unit_occupancy: Decimal | None
    pct_sqft_occupied: Decimal | None
    balance: Decimal | None
    sheet_row: int


class ParsedChargeSummary(BaseModel):
    charge_code: str
    amount: Decimal
    sheet_row: int


class RentRollFile(BaseModel):
    property_code: str
    property_name: str
    as_of_date: dt.date
    report_month: dt.date
    leases: list[ParsedLease] = []
    summary_groups: list[ParsedSummaryGroup] = []
    charge_summary: list[ParsedChargeSummary] = []
    sheet_rows: int = 0
    issues: list[ParsedIssue] = []

    @property
    def parsed_rows(self) -> int:
        return (
            len(self.leases)
            + sum(len(x.charges) for x in self.leases)
            + len(self.summary_groups)
            + len(self.charge_summary)
        )


class AvailabilityFile(BaseModel):
    property_code: str
    property_name: str
    as_of_date: dt.date
    avg_sqft: int
    avg_rent: Decimal
    units: int
    occupied_no_notice: int
    vacant_rented: int
    vacant_unrented: int
    notice_rented: int
    notice_unrented: int
    available: int
    model: int
    down: int
    admin: int
    pct_occ: Decimal
    pct_occ_w_nonrev: Decimal
    pct_leased: Decimal
    pct_trend: Decimal
    sheet_rows: int = 0
    issues: list[ParsedIssue] = []

    @property
    def parsed_rows(self) -> int:
        return 1


def derive_status(section: Section, sentinel: str | None, move_out: dt.date | None) -> Status:
    """PLAN.md 0.3. Order matters: the section wins over everything else."""
    if section == "future":
        return "future"
    if sentinel == "VACANT":
        return "vacant"
    if sentinel == "MODEL":
        return "model"
    if sentinel == "DOWN":
        return "down"
    if move_out is not None:
        return "notice"
    return "occupied"


def derive_asset_key(property_code: str) -> str:
    """'134r' -> '134'; 'altapm' -> 'altapm'."""
    import re

    m = re.match(r"^\d+", property_code)
    return m.group(0) if m else property_code


def derive_book_type(property_code: str) -> str:
    code = property_code.lower()
    if code.endswith("land"):
        return "land"
    if code.endswith("c"):
        return "commercial"
    if code.endswith("a"):
        return "affordable"
    if code.endswith("r"):
        return "residential"
    return "other"
