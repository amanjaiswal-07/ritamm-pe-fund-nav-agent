"""Read the bank statement PDF into Transaction objects.

pdfplumber's table extraction only finds the header row of this statement, so rows are
rebuilt from word positions: each word goes to a column by its x-coordinate. This is what
separates a debit from a credit, since the text alone cannot tell them apart.

A line with a date starts a new transaction. A line without one continues the previous
description, which keeps stray fragments like "8.1" out of the amount columns.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber

from models import Transaction, to_decimal

AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")
DATE_RE = re.compile(r"^\d{2} [A-Z][a-z]{2} \d{4}$")
PAGE_FOOTER_RE = re.compile(r"Page \d+ of \d+$")
TOTALS_PREFIX = "TOTAL DEBITS / CREDITS"
COLUMNS = ("Date", "Description", "Reference", "Debit", "Credit", "Balance")
LINE_TOLERANCE = 2  # Words within this many points vertically share a line.
COLUMN_SLACK = 2  # Allowance for words starting just left of a column's header.


class StatementParseError(Exception):
    """The statement does not have the expected layout or a row is malformed."""


@dataclass
class Statement:
    opening_balance: Decimal
    transactions: list[Transaction]
    reported_total_debits: Decimal | None
    reported_total_credits: Decimal | None

    @property
    def closing_balance(self) -> Decimal:
        return self.transactions[-1].balance if self.transactions else self.opening_balance


def _group_into_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - word["top"]) <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _split_columns(line: list[dict], header: dict[str, dict]) -> dict[str, list[str]]:
    """Assign words to columns. Amounts are right-aligned, so they are placed by right edge."""
    cols: dict[str, list[str]] = {name: [] for name in COLUMNS}
    for word in line:
        text = word["text"]
        if word["x0"] < header["Description"]["x0"] - COLUMN_SLACK:
            cols["Date"].append(text)
        elif word["x0"] < header["Reference"]["x0"] - COLUMN_SLACK:
            cols["Description"].append(text)
        elif not AMOUNT_RE.match(text):
            cols["Reference"].append(text)
        elif word["x1"] <= header["Credit"]["x0"] - COLUMN_SLACK:
            cols["Debit"].append(text)
        elif word["x1"] <= header["Balance"]["x0"] - COLUMN_SLACK:
            cols["Credit"].append(text)
        else:
            cols["Balance"].append(text)
    return cols


def _amount(values: list[str]) -> Decimal:
    return to_decimal(values[0].replace(",", "")) if values else Decimal("0")


def parse_statement(path: Path) -> Statement:
    """Parse the PDF. Raises StatementParseError rather than skipping a row it cannot read."""
    rows: list[dict] = []
    totals: tuple[Decimal, Decimal] | None = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=1.5)
            header_words = [w for w in words if w["text"] in COLUMNS]
            header = {w["text"]: w for w in header_words}
            if set(header) != set(COLUMNS):
                raise StatementParseError(f"Page {page.page_number}: table header not found")
            body = [w for w in words if w["top"] > header["Date"]["bottom"]]

            for line in _group_into_lines(body):
                text = " ".join(w["text"] for w in line)
                if text.startswith(TOTALS_PREFIX):
                    amounts = [w["text"] for w in line if AMOUNT_RE.match(w["text"])]
                    if len(amounts) != 2:
                        raise StatementParseError(f"Unreadable totals line: {text}")
                    totals = (_amount(amounts[:1]), _amount(amounts[1:]))
                    break  # Only the disclaimer and footer follow the totals line.
                if PAGE_FOOTER_RE.search(text):
                    continue

                cols = _split_columns(line, header)
                date_text = " ".join(cols["Date"])
                if DATE_RE.match(date_text):
                    if len(cols["Balance"]) != 1:
                        raise StatementParseError(f"Row without a single balance: {text}")
                    rows.append(
                        {
                            "date": datetime.strptime(date_text, "%d %b %Y").strftime("%Y-%m-%d"),
                            "description": cols["Description"],
                            "reference": " ".join(cols["Reference"]) or None,
                            "debit": _amount(cols["Debit"]),
                            "credit": _amount(cols["Credit"]),
                            "balance": _amount(cols["Balance"]),
                        }
                    )
                elif rows and cols["Description"] and not (cols["Debit"] or cols["Credit"] or cols["Balance"]):
                    rows[-1]["description"] += cols["Description"]
                else:
                    raise StatementParseError(f"Unrecognised line: {text}")

    if not rows or " ".join(rows[0]["description"]) != "BALANCE BROUGHT FORWARD":
        raise StatementParseError("The statement must start with the opening balance line")

    transactions = [Transaction(**{**row, "description": " ".join(row["description"])}) for row in rows[1:]]
    return Statement(
        opening_balance=rows[0]["balance"],
        transactions=transactions,
        reported_total_debits=totals[0] if totals else None,
        reported_total_credits=totals[1] if totals else None,
    )
