"""Apply the four review rules to CVP's Q4 marks and decide each stake's Q4 value.

R1 mark moved more than the threshold, measured in the company's own currency.
R2 valuation method changed from Q3.
R3 held at 31 December with no mark: carry a Q4 purchase at cost (brief, hint 2).
R4 marked but not held at 31 December: flag it and leave it out of the NAV.

A company sold in the quarter with no mark trips none of these, so Fjord gets no flag.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from models import money, round_money, to_decimal
from services.q3_loader import Q3Valuation


class ReviewError(Exception):
    """A mark is in a currency this review has no rate for."""


@dataclass
class Position:
    company: str
    currency: str
    q3_fair_value: Decimal | None
    q4_fair_value: Decimal | None
    q3_fair_value_usd: Decimal | None
    q4_fair_value_usd: Decimal | None
    change_pct: Decimal | None
    q3_method: str | None
    q4_method: str | None
    held_at_q4: bool
    value_basis: str
    flags: list[str] = field(default_factory=list)
    reason: str = ""

    def to_output(self) -> dict:
        def amount(value):
            return None if value is None else money(value)

        return {
            "company": self.company,
            "currency": self.currency,
            "q3_fair_value": amount(self.q3_fair_value),
            "q4_fair_value": amount(self.q4_fair_value),
            "change_pct": None if self.change_pct is None else money(self.change_pct),
            "q3_method": self.q3_method,
            "q4_method": self.q4_method,
            "flags": self.flags,
            "reason": self.reason,
            "q3_fair_value_usd": amount(self.q3_fair_value_usd),
            "q4_fair_value_usd": amount(self.q4_fair_value_usd),
            "held_at_q4": self.held_at_q4,
            "value_basis": self.value_basis,
        }


def _to_usd(value: Decimal, currency: str, eurusd: Decimal) -> Decimal:
    if currency == "USD":
        return value
    if currency == "EUR":
        return round_money(value * eurusd)
    raise ReviewError(f"No FX rate for {currency}")


def review_marks(
    q3: list[Q3Valuation], marks: list[dict], notes: list[dict], purchases: list[dict], sales: list[dict],
    threshold_pct: Decimal, eurusd: Decimal,
) -> list[Position]:
    q3_by_company = {v.company: v for v in q3}
    marks_by_company = {m["company"]: m for m in marks}
    notes_by_company = {n["company"]: n["note"] for n in notes}
    bought = {p["company"]: to_decimal(p["amount_usd"]) for p in purchases}
    sold = {s["company"] for s in sales}
    held = (set(q3_by_company) | set(bought)) - sold

    positions = []
    for company in sorted(set(q3_by_company) | set(marks_by_company) | set(bought)):
        before = q3_by_company.get(company)
        mark = marks_by_company.get(company)
        note = notes_by_company.get(company)
        is_held = company in held
        flags: list[str] = []
        reasons: list[str] = []

        currency = mark["currency"] if mark else before.currency if before else "USD"
        q4_local = to_decimal(mark["fair_value"]) if mark else None
        q4_usd = None
        change_pct = None
        basis = "CVP Q4 mark"

        if mark and before and mark["currency"] == before.currency:
            change_pct = (q4_local - before.fair_value_local) / before.fair_value_local * 100
            if abs(change_pct) > threshold_pct:
                flags.append("R1")
                reasons.append(f"R1: {currency} fair value moved {change_pct:+.2f}% from Q3, beyond the {threshold_pct}% threshold")
            if mark["valuation_method"] != before.method:
                flags.append("R2")
                reasons.append(f"R2: method changed from {before.method} to {mark['valuation_method']}")

        if mark and is_held:
            q4_usd = _to_usd(q4_local, currency, eurusd)
        elif mark:
            flags.append("R4")
            reasons.append("R4: CVP gave a mark but the fund does not hold this company at 31 December; left out of the NAV")
            basis = "Excluded: not held"
        elif is_held:
            flags.append("R3")
            if company in bought:
                q4_local = q4_usd = bought[company]
                basis = "Cost: purchased in Q4, no mark yet"
                reasons.append(f"R3: no Q4 mark ({note or 'no CVP note'}); carried at purchase cost")
            else:
                # Not in this quarter's data, but a stake must never fall out of the NAV silently.
                q4_local = before.fair_value_local
                q4_usd = _to_usd(q4_local, currency, eurusd)
                basis = "Q3 fair value carried forward: no mark"
                reasons.append(f"R3: no Q4 mark ({note or 'no CVP note'}); Q3 fair value carried forward")
        else:
            basis = "Sold in Q4"

        positions.append(Position(
            company=company,
            currency=currency,
            q3_fair_value=before.fair_value_local if before else None,
            q4_fair_value=q4_local if is_held or mark else None,
            q3_fair_value_usd=before.fair_value_usd if before else None,
            q4_fair_value_usd=q4_usd,
            change_pct=change_pct,
            q3_method=before.method if before else None,
            q4_method=mark["valuation_method"] if mark else None,
            held_at_q4=is_held,
            value_basis=basis,
            flags=flags,
            reason="; ".join(reasons) or note or "No exceptions.",
        ))
    return positions
