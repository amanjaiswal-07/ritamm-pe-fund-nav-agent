"""Shared fixtures. Everything here runs offline: FakeLLM stands in for the real model."""

import json
import re
from pathlib import Path

import pytest

import config
from services.classifier import FundContext, classify_transactions
from services.pdf_parser import parse_statement
from services.q3_loader import load_nav_summary, load_q3_valuations

PACK = Path(__file__).resolve().parents[1] / "pack"

# What a correct model would answer: (description pattern, category, company).
ANSWERS = [
    (r"MGMT FEE", "management_fee", None),
    (r"HARBOURSIDE|CORAL AND FINCH", "accrual_settlement", None),
    (r"DIVIDEND", "dividend_income", None),
    (r"CC07|CALL NO 7", "capital_call", None),
    (r"CREDIT INTEREST", "interest_income", None),
    (r"MAINTENANCE|WIRE CHARGES", "bank_charges", None),
    (r"LEGAL|D&O", "expense", None),
    (r"REFUND", "expense_refund", None),
    (r"DISTRIBUTION", "distribution", None),
    (r"FJORD", "sale_proceeds", "FJORD PAYMENTS AS"),
    (r"IONMERE", "investment_purchase", "IONMERE MATERIALS LTD"),
]


def correct_answer(line: dict) -> dict:
    for pattern, category, company in ANSWERS:
        if re.search(pattern, line["description"]):
            printed = re.search(r"LP\d{2}", line["description"])
            lp_code = printed.group() if printed else ("LP10" if "MEHTA" in line["description"] else None)
            return {"id": line["id"], "category": category, "lp_code": lp_code, "company": company, "rationale": "test"}
    raise AssertionError(f"No test answer for {line['description']}")


class FakeLLM:
    """Answers correctly, unless a test passes `tamper` to change a reply or `replies` to script them."""

    def __init__(self, tamper=None, replies=None):
        self.tamper = tamper
        self.replies = list(replies or [])
        self.calls: list[list[dict]] = []

    def __call__(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        if self.replies:
            return self.replies.pop(0)
        lines = json.loads(messages[-1]["content"].split("TRANSACTIONS:\n", 1)[1])
        answers = [correct_answer(line) for line in lines]
        if self.tamper:
            answers = self.tamper(answers, lines, messages[-1]["content"])
        return json.dumps({"transactions": answers})


@pytest.fixture(scope="session")
def pack_dir() -> Path:
    return PACK


@pytest.fixture(scope="session")
def statement():
    return parse_statement(PACK / config.BANK_STATEMENT_FILE)


@pytest.fixture(scope="session")
def summary():
    return load_nav_summary(PACK / config.Q3_NAV_FILE)


@pytest.fixture(scope="session")
def q3_valuations():
    return load_q3_valuations(PACK / config.Q3_VALUATION_FILE)


@pytest.fixture(scope="session")
def agent_b_data() -> dict:
    return json.loads((PACK / config.AGENT_B_DATA_FILE).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def context(summary) -> FundContext:
    return FundContext("The Greatness PE Fund, L.P.", summary.investors, summary.liabilities,
                       [h.company for h in summary.holdings])


@pytest.fixture(scope="session")
def ledger(statement, context):
    return classify_transactions(statement.transactions, context, FakeLLM())
