"""Compare Agent A's purchases and sales with Agent B's records.

Company names are compared ignoring case and punctuation because banks print capitals.
Dates and amounts must match exactly. Duplicates are counted, so a repeated item fails.
"""

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from models import to_decimal


@dataclass(frozen=True)
class VerificationResult:
    matched: int
    expected: int
    submitted: int
    unmatched_submitted: list[str]

    @property
    def ok(self) -> bool:
        return self.matched == self.expected == self.submitted


def _key(kind: str, item: dict, amount_field: str) -> tuple[str, str, str, Decimal]:
    company = re.sub(r"[^a-z0-9]+", " ", str(item["company"]).lower()).strip()
    return kind, company, str(item["date"]), to_decimal(item[amount_field])


def _keys(purchases: list[dict], sales: list[dict]) -> list[tuple]:
    return [_key("purchase", p, "amount_usd") for p in purchases] + [_key("sale", s, "proceeds_usd") for s in sales]


def verify_records(submitted: dict, records: dict) -> VerificationResult:
    """Raises ValueError if the submission is malformed. Reports only A's own unmatched items,
    so a wrong answer cannot be used to read B's records."""
    try:
        claimed = Counter(_keys(submitted["purchases"], submitted["sales"]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError(f"purchases and sales are malformed: {exc}") from exc
    expected = Counter(_keys(records["purchases"], records["sales"]))
    matched = claimed & expected
    unmatched = [f"{k[0]} {k[1]} {k[2]} {k[3]}" for k in (claimed - expected).elements()]
    return VerificationResult(sum(matched.values()), sum(expected.values()), sum(claimed.values()), unmatched)
