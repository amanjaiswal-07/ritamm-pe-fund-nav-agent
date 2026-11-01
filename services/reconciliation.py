"""Arithmetic checks on the statement: opening + movements = closing, row by row.

These use only numbers printed on the statement, so they can run before any LLM call.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from services.pdf_parser import Statement


@dataclass
class ReconciliationResult:
    opening_balance: Decimal
    total_debits: Decimal
    total_credits: Decimal
    closing_balance: Decimal
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def reconcile(statement: Statement) -> ReconciliationResult:
    """Return every break found rather than stopping at the first, to make diagnosis easier."""
    problems: list[str] = []
    running = statement.opening_balance

    for row, txn in enumerate(statement.transactions, start=1):
        if (txn.debit > 0) == (txn.credit > 0):
            problems.append(f"Row {row} ({txn.date}): needs exactly one of debit or credit")
        running = running + txn.credit - txn.debit
        if running != txn.balance:
            problems.append(f"Row {row} ({txn.date}): expected balance {running}, statement shows {txn.balance}")
            running = txn.balance  # Resynchronise so one bad row reports one break, not all later ones.

    debits = sum((t.debit for t in statement.transactions), Decimal("0"))
    credits = sum((t.credit for t in statement.transactions), Decimal("0"))
    closing = statement.closing_balance

    if statement.opening_balance + credits - debits != closing:
        problems.append(f"Opening {statement.opening_balance} + credits {credits} - debits {debits} != closing {closing}")
    if statement.reported_total_debits is None:
        problems.append("Statement totals line not found")
    else:
        if statement.reported_total_debits != debits:
            problems.append(f"Total debits {debits} differ from the statement's {statement.reported_total_debits}")
        if statement.reported_total_credits != credits:
            problems.append(f"Total credits {credits} differ from the statement's {statement.reported_total_credits}")

    return ReconciliationResult(statement.opening_balance, debits, credits, closing, problems)
