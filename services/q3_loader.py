"""Load the Q3 Excel files and check that they agree with each other.

Rows are found by their labels rather than fixed cell addresses. Total rows are computed
here, not read, because those cells are formulas that may have no cached value.

Cost always comes from the NAV summary (USD). The CVP report states Ember's cost in EUR,
and the brief says to keep cost in USD as given, not to re-convert it.
"""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from models import to_decimal


class Q3DataError(Exception):
    """A Q3 file is missing a section, or the two files disagree."""


@dataclass(frozen=True)
class NamedAmount:
    name: str
    amount: Decimal


@dataclass(frozen=True)
class Holding:
    company: str
    cost_usd: Decimal
    fair_value_usd: Decimal


@dataclass(frozen=True)
class Investor:
    name: str
    code: str
    commitment: Decimal


@dataclass(frozen=True)
class NavSummary:
    cash: Decimal
    investments: Decimal
    liabilities: list[NamedAmount]
    nav: Decimal
    holdings: list[Holding]
    investors: list[Investor]
    q4_accruals: list[NamedAmount]


@dataclass(frozen=True)
class Q3Valuation:
    company: str
    currency: str
    fair_value_local: Decimal
    fx_rate: Decimal
    fair_value_usd: Decimal
    method: str


def _rows(sheet) -> list[tuple]:
    return [tuple(row) for row in sheet.iter_rows(values_only=True)]


def _section(rows: list[tuple], start_label: str, end_label: str) -> list[tuple]:
    """Rows strictly between the row labelled start_label and the row labelled end_label."""
    labels = [str(row[0] or "") for row in rows]
    try:
        start = next(i for i, label in enumerate(labels) if label.startswith(start_label))
        end = next(i for i, label in enumerate(labels) if i > start and label.startswith(end_label))
    except StopIteration as exc:
        raise Q3DataError(f"Section {start_label!r} to {end_label!r} not found") from exc
    return rows[start + 1 : end]


def _value(rows: list[tuple], label: str) -> Decimal:
    for row in rows:
        if str(row[0] or "").startswith(label):
            return to_decimal(row[1])
    raise Q3DataError(f"Row {label!r} not found")


def load_nav_summary(path: Path) -> NavSummary:
    workbook = load_workbook(path, data_only=False)
    net_assets, accruals = _rows(workbook.worksheets[0]), _rows(workbook.worksheets[1])
    return NavSummary(
        cash=_value(net_assets, "Cash at bank"),
        investments=_value(net_assets, "Investments at fair value"),
        liabilities=[
            NamedAmount(r[0], to_decimal(r[1])) for r in _section(net_assets, "Liabilities", "Total liabilities")
        ],
        nav=_value(net_assets, "Net asset value"),
        holdings=[
            Holding(r[0], to_decimal(r[1]), to_decimal(r[2]))
            for r in _section(net_assets, "Portfolio company", "Total")
        ],
        investors=[
            Investor(r[0], r[1], to_decimal(r[2])) for r in _section(net_assets, "Investor (LP)", "Total commitments")
        ],
        q4_accruals=[
            NamedAmount(r[0], to_decimal(r[1]))
            for r in _section(accruals, "These are liabilities", "Total")
            if r[0] is not None
        ],
    )


def load_q3_valuations(path: Path) -> list[Q3Valuation]:
    rows = _rows(load_workbook(path, data_only=False).worksheets[0])
    return [
        Q3Valuation(r[0], r[1], to_decimal(r[3]), to_decimal(r[4]), to_decimal(r[5]), r[6])
        for r in _section(rows, "Portfolio company", "Total")
    ]


def check_q3_consistency(summary: NavSummary, valuations: list[Q3Valuation]) -> list[str]:
    """Return every disagreement between or within the two Q3 files."""
    problems = []
    total_fv = sum((h.fair_value_usd for h in summary.holdings), Decimal("0"))
    total_liabilities = sum((l.amount for l in summary.liabilities), Decimal("0"))
    if total_fv != summary.investments:
        problems.append(f"Holdings total {total_fv} differs from investments {summary.investments}")
    if summary.cash + summary.investments - total_liabilities != summary.nav:
        problems.append("Q3 cash + investments - liabilities does not equal Q3 NAV")

    by_company = {h.company: h for h in summary.holdings}
    if set(by_company) != {v.company for v in valuations}:
        problems.append("The two Q3 files list different companies")
    for valuation in valuations:
        if valuation.fair_value_local * valuation.fx_rate != valuation.fair_value_usd:
            problems.append(f"{valuation.company}: local value x FX rate does not equal the USD value")
        holding = by_company.get(valuation.company)
        if holding and holding.fair_value_usd != valuation.fair_value_usd:
            problems.append(f"{valuation.company}: USD fair value differs between the Q3 files")
    return problems
