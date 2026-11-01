"""NAV and bridge. Expected figures were derived by hand from the source files, separately from this code."""

from decimal import Decimal

import pytest

import config
from services.nav import compute_nav
from services.q3_loader import check_q3_consistency
from services.review import review_marks


@pytest.fixture(scope="module")
def positions(q3_valuations, agent_b_data):
    data = agent_b_data
    return review_marks(q3_valuations, data["marks"], data["notes"], data["purchases"], data["sales"],
                        config.MATERIALITY_THRESHOLD_PCT, config.EURUSD_RATE)


def run_nav(ledger, statement, summary, positions, data):
    return compute_nav(ledger, statement.opening_balance, statement.closing_balance, summary, positions,
                       data["purchases"], data["sales"])


@pytest.fixture(scope="module")
def nav(ledger, statement, summary, positions, agent_b_data):
    return run_nav(ledger, statement, summary, positions, agent_b_data)


def test_q3_files_agree(summary, q3_valuations):
    assert check_q3_consistency(summary, q3_valuations) == []


def test_balance_sheet_nav(nav):
    assert nav["closing_cash"] == 23896579.25
    assert nav["investments_usd"] == 146062460.00
    assert nav["liabilities"] == 195000.00
    assert nav["nav_q4"] == 169764039.25


def test_bridge_balances_to_the_cent(nav):
    bridge = nav["bridge"]
    assert bridge["nav_q3"] == 160224800.00
    assert bridge["capital_called"] == 20000000.00
    assert bridge["distributions"] == 22500000.00
    assert bridge["income"] == 459429.25
    assert bridge["expenses"] == 1522850.00
    assert bridge["realized_gain"] == 9000000.00
    assert bridge["change_in_unrealized"] == 4102660.00
    assert bridge["nav_q4"] == nav["nav_q4"]
    assert bridge["difference"] == 0.00


def test_expense_definition(nav):
    assert nav["management_fee"] == 1250000.00
    assert nav["expense_breakdown"] == {
        "management_fee": 1250000.00,
        "other_expenses_paid": 80500.00,
        "refunds": 3500.00,
        "bank_charges": 850.00,
        "q4_accrued_expenses": 195000.00,
    }


def test_accrual_settlements_are_not_expenses(nav):
    assert nav["accrual_settlements_excluded_from_expenses"] == 185000.00
    assert nav["checks"]["settlements_equal_q3_liabilities"]


def test_sold_stake_reverses_its_q3_gain_and_is_not_double_counted(nav):
    assert nav["unrealized_gain"] == {"q3": 23775800.00, "q4": 27878460.00}
    assert "Fjord Payments AS" not in {h["company"] for h in nav["holdings"]}
    # Fjord: 9.0M realized, with its 7.5M Q3 unrealized gain reversing. The net 1.5M is the real Q4 uplift.
    assert nav["bridge"]["realized_gain"] == 29000000.00 - 20000000.00


def test_euro_stake_keeps_its_usd_cost(nav):
    ember = next(h for h in nav["holdings"] if h["company"] == "Ember Retail GmbH")
    assert ember == {"company": "Ember Retail GmbH", "cost_usd": 14184000.00, "fair_value_usd": 13972460.00,
                     "unrealized_gain_usd": -211540.00, "value_basis": "CVP Q4 mark"}


def test_recent_purchase_is_in_the_nav_at_cost(nav):
    ionmere = next(h for h in nav["holdings"] if h["company"] == "Ionmere Materials Ltd")
    assert ionmere["fair_value_usd"] == ionmere["cost_usd"] == 14000000.00
    assert ionmere["unrealized_gain_usd"] == 0.00


def test_lp_activity_is_pro_rata(nav):
    assert nav["checks"]["capital_calls_pro_rata"] and nav["checks"]["distributions_pro_rata"]
    assert {r["called_pct_of_commitment"] for r in nav["lp_activity"]} == {8.00}
    assert {r["distributed_pct_of_commitment"] for r in nav["lp_activity"]} == {9.00}
    bluewater = next(r for r in nav["lp_activity"] if r["lp_code"] == "LP07")
    assert bluewater["capital_called"] == 1600000.00


def test_all_checks_pass_on_the_real_data(nav):
    assert all(nav["checks"].values()), nav["checks"]


def recategorize(ledger, text, category):
    return [t.model_copy(update={"category": category}) if text in t.description else t for t in ledger]


@pytest.mark.parametrize("text,category,gap", [
    # difference = balance-sheet NAV - bridge NAV, so understating expenses makes it negative.
    ("HARBOURSIDE", "expense", 65000.00),  # A settlement booked as a new expense.
    ("LEGAL", "accrual_settlement", -38500.00),  # A real expense hidden as a settlement.
    ("D&O", "investment_purchase", -42000.00),  # An expense treated as a capital item.
])
def test_a_miscategorized_row_breaks_the_bridge_rather_than_being_absorbed(
    ledger, statement, summary, positions, agent_b_data, text, category, gap
):
    nav = run_nav(recategorize(ledger, text, category), statement, summary, positions, agent_b_data)
    assert nav["bridge"]["difference"] == gap
    assert not nav["checks"]["bridge_balances"]


def test_a_wrong_lp_code_fails_the_pro_rata_check(ledger, statement, summary, positions, agent_b_data):
    changed = [t.model_copy(update={"lp_code": "LP02"}) if "MEHTA FAM TRST" in t.description else t for t in ledger]
    nav = run_nav(changed, statement, summary, positions, agent_b_data)
    assert not nav["checks"]["capital_calls_pro_rata"]
    assert nav["bridge"]["difference"] == 0.00  # The bridge alone would not catch this one.


def test_money_is_exact_decimal_internally(statement):
    # Binary floats give 0.30000000000000004 style noise; these are exact.
    assert sum((t.credit for t in statement.transactions if "CREDIT INTEREST" in t.description), Decimal("0")) == Decimal("39429.25")
