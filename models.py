"""Shared data shapes and money helpers used by both agents.

Amounts are Decimal in memory so no float rounding can creep into the accounting.
They become 2-decimal floats only at the boundary (JSON files and A2A messages).
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, get_args

from pydantic import BaseModel, field_validator

Category = Literal[
    "capital_call",
    "distribution",
    "management_fee",
    "investment_purchase",
    "sale_proceeds",
    "dividend_income",
    "interest_income",
    "bank_charges",
    "expense",
    "expense_refund",
    "accrual_settlement",
]
CATEGORIES = get_args(Category)

# Which side of the statement each category must sit on. Used to reject impossible LLM answers.
CREDIT_CATEGORIES = {"capital_call", "sale_proceeds", "dividend_income", "interest_income", "expense_refund"}
DEBIT_CATEGORIES = set(CATEGORIES) - CREDIT_CATEGORIES

CENT = Decimal("0.01")


def to_decimal(value) -> Decimal:
    """Convert a JSON or Excel number to Decimal via str, which avoids binary float noise."""
    return Decimal(str(value))


def round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> float:
    """Format a Decimal for output: a float rounded to the cent."""
    return float(round_money(value))


class Transaction(BaseModel):
    """One statement line. Amounts and dates come only from the PDF, never from the LLM."""

    date: str
    description: str
    reference: str | None = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    balance: Decimal
    category: Category | None = None
    lp_code: str | None = None
    company: str | None = None
    rationale: str | None = None

    @field_validator("debit", "credit", "balance", mode="before")
    @classmethod
    def _via_str(cls, value):
        return to_decimal(value)

    def to_output(self) -> dict:
        """The transactions.json shape, also sent to Agent B as the ledger."""
        return {
            "date": self.date,
            "description": self.description,
            "reference": self.reference,
            "debit": money(self.debit),
            "credit": money(self.credit),
            "balance": money(self.balance),
            "category": self.category,
            "lp_code": self.lp_code,
            "company": self.company,
            "rationale": self.rationale,
        }
