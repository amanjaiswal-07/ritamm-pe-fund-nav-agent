"""Compute the Q4 NAV two independent ways and compare them.

The balance-sheet NAV uses closing cash, valuations and accruals. The bridge walks from Q3
using categorized cash movements and gains. They share few inputs, so a miscategorized row
(say a settlement booked as a capital item) shows up as a non-zero difference. That
difference is reported, never adjusted away.
"""

from collections import defaultdict
from decimal import Decimal

from models import Transaction, money, to_decimal
from services.q3_loader import NavSummary
from services.review import Position

ZERO = Decimal("0")


def _total(ledger: list[Transaction], category: str, side: str) -> Decimal:
    return sum((getattr(t, side) for t in ledger if t.category == category), ZERO)


def _lp_activity(ledger: list[Transaction], summary: NavSummary) -> tuple[list[dict], dict]:
    """Per-LP totals. Calls and distributions are pro rata, so each LP's % of commitment should agree."""
    called: dict[str, Decimal] = defaultdict(lambda: ZERO)
    distributed: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in ledger:
        if t.category == "capital_call":
            called[t.lp_code] += t.credit
        elif t.category == "distribution":
            distributed[t.lp_code] += t.debit

    rows = []
    for lp in summary.investors:
        rows.append({
            "lp_code": lp.code,
            "commitment": money(lp.commitment),
            "capital_called": money(called[lp.code]),
            "distributions": money(distributed[lp.code]),
            "called_pct_of_commitment": money(called[lp.code] / lp.commitment * 100),
            "distributed_pct_of_commitment": money(distributed[lp.code] / lp.commitment * 100),
        })
    checks = {
        "capital_calls_pro_rata": len({r["called_pct_of_commitment"] for r in rows}) == 1,
        "distributions_pro_rata": len({r["distributed_pct_of_commitment"] for r in rows}) == 1,
    }
    return rows, checks


def compute_nav(
    ledger: list[Transaction], opening_balance: Decimal, closing_balance: Decimal, summary: NavSummary,
    positions: list[Position], purchases: list[dict], sales: list[dict],
) -> dict:
    # Balance-sheet NAV.
    held = [p for p in positions if p.held_at_q4]
    investments = sum((p.q4_fair_value_usd for p in held), ZERO)
    liabilities = sum((a.amount for a in summary.q4_accruals), ZERO)
    nav_q4 = closing_balance + investments - liabilities

    # Cash movements by category.
    capital_called = _total(ledger, "capital_call", "credit")
    distributions = _total(ledger, "distribution", "debit")
    management_fee = _total(ledger, "management_fee", "debit")
    other_expenses = _total(ledger, "expense", "debit")
    refunds = _total(ledger, "expense_refund", "credit")
    bank_charges = _total(ledger, "bank_charges", "debit")
    dividends = _total(ledger, "dividend_income", "credit")
    interest = _total(ledger, "interest_income", "credit")
    settlements = _total(ledger, "accrual_settlement", "debit")

    income = dividends + interest
    # Settlements are left out: they reduce cash and a Q3 liability equally, so NAV is unchanged.
    expenses = management_fee + other_expenses - refunds + bank_charges + liabilities

    # Realized and unrealized gains use USD cost from the NAV summary plus Q4 purchases at cost.
    cost = {h.company: h.cost_usd for h in summary.holdings}
    cost.update({p["company"]: to_decimal(p["amount_usd"]) for p in purchases})
    realized_gain = sum((to_decimal(s["proceeds_usd"]) - cost[s["company"]] for s in sales), ZERO)

    unrealized_q3 = sum((h.fair_value_usd - h.cost_usd for h in summary.holdings), ZERO)
    unrealized_q4 = sum((p.q4_fair_value_usd - cost[p.company] for p in held), ZERO)
    change_in_unrealized = unrealized_q4 - unrealized_q3  # A sold stake's Q3 gain reverses here.

    bridge_nav = summary.nav + capital_called - distributions + income - expenses + realized_gain + change_in_unrealized

    lp_rows, lp_checks = _lp_activity(ledger, summary)
    q3_liabilities = sum((l.amount for l in summary.liabilities), ZERO)
    checks = {
        **lp_checks,
        "opening_cash_matches_q3": opening_balance == summary.cash,
        "settlements_equal_q3_liabilities": settlements == q3_liabilities,
        "ledger_cash_matches_closing": opening_balance + sum((t.credit - t.debit for t in ledger), ZERO) == closing_balance,
        "bridge_balances": nav_q4 == bridge_nav,
    }

    return {
        "closing_cash": money(closing_balance),
        "investments_usd": money(investments),
        "liabilities": money(liabilities),
        "nav_q4": money(nav_q4),
        "bridge": {
            "nav_q3": money(summary.nav),
            "capital_called": money(capital_called),
            "distributions": money(distributions),
            "income": money(income),
            "expenses": money(expenses),
            "realized_gain": money(realized_gain),
            "change_in_unrealized": money(change_in_unrealized),
            "nav_q4": money(bridge_nav),
            "difference": money(nav_q4 - bridge_nav),
        },
        "management_fee": money(management_fee),
        "expense_breakdown": {
            "management_fee": money(management_fee),
            "other_expenses_paid": money(other_expenses),
            "refunds": money(refunds),
            "bank_charges": money(bank_charges),
            "q4_accrued_expenses": money(liabilities),
        },
        "income_breakdown": {"dividends": money(dividends), "interest": money(interest)},
        "accrual_settlements_excluded_from_expenses": money(settlements),
        "unrealized_gain": {"q3": money(unrealized_q3), "q4": money(unrealized_q4)},
        "holdings": [
            {"company": p.company, "cost_usd": money(cost[p.company]), "fair_value_usd": money(p.q4_fair_value_usd),
             "unrealized_gain_usd": money(p.q4_fair_value_usd - cost[p.company]), "value_basis": p.value_basis}
            for p in held
        ],
        "lp_activity": lp_rows,
        "checks": checks,
    }
