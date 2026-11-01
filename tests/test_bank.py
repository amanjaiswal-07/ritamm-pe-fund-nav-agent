"""PDF parsing and bank reconciliation, against the real statement and corrupted copies of it."""

import copy
from decimal import Decimal

from services.reconciliation import reconcile


def test_counts_transactions_without_the_opening_or_totals_lines(statement):
    assert len(statement.transactions) == 39
    descriptions = [t.description for t in statement.transactions]
    assert not any("BROUGHT FORWARD" in d or "TOTAL DEBITS" in d for d in descriptions)


def test_multiline_description_is_complete(statement):
    first = statement.transactions[0]
    assert first.description == "OUTWARD WIRE TO GREATNESS CAPITAL ADVISORS LLC REF MGMT FEE Q4-22 PER LPA 8.1"
    assert first.reference == "OW-221003-01"
    assert first.date == "2022-10-03"


def test_footer_does_not_leak_into_the_last_row_of_a_page(statement):
    assert not any("Meridian" in t.description or "Page" in t.description for t in statement.transactions)


def test_amounts_land_in_the_right_column(statement):
    by_text = {t.description: t for t in statement.transactions}
    sale = next(t for d, t in by_text.items() if "FJORD" in d)
    assert (sale.credit, sale.debit) == (Decimal("29000000.00"), Decimal("0"))
    purchase = next(t for d, t in by_text.items() if "IONMERE" in d)
    assert (purchase.debit, purchase.credit) == (Decimal("14000000.00"), Decimal("0"))


def test_wires_without_a_reference_are_null(statement):
    wire = next(t for t in statement.transactions if "LP02" in t.description and t.credit > 0)
    assert wire.reference is None


def test_statement_reconciles(statement):
    result = reconcile(statement)
    assert result.ok, result.problems
    assert result.opening_balance == Decimal("12450000.00")
    assert result.closing_balance == Decimal("23896579.25")
    assert result.total_debits == Decimal("38016350.00")
    assert result.total_credits == Decimal("49462929.25")


def test_a_wrong_running_balance_is_reported_on_that_row_only(statement):
    broken = copy.deepcopy(statement)
    broken.transactions[5].balance += Decimal("0.01")
    problems = reconcile(broken).problems
    assert any(p.startswith("Row 6 ") for p in problems)
    assert not any(p.startswith("Row 9 ") for p in problems)


def test_a_totals_line_disagreement_is_reported(statement):
    broken = copy.deepcopy(statement)
    broken.reported_total_credits += Decimal("1")
    assert not reconcile(broken).ok


def test_a_row_with_both_debit_and_credit_is_reported(statement):
    broken = copy.deepcopy(statement)
    broken.transactions[0].credit = broken.transactions[0].debit
    assert any("exactly one" in p for p in reconcile(broken).problems)
