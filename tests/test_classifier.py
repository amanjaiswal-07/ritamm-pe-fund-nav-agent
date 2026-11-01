"""Categorization: the checks around the LLM, and the HTTP client's failure handling."""

import json
from collections import Counter

import httpx
import pytest

from config import LLMSettings
from models import CATEGORIES
from services.classifier import (MAX_ATTEMPTS, ClassificationError, LLMAnswer, LLMError, OpenAICompatibleLLM,
                                 classify_transactions, extract_json, normalize_company, validate_answer)
from tests.conftest import FakeLLM


def row(ledger, text):
    return next(t for t in ledger if text in t.description)


def test_every_row_gets_an_allowed_category(ledger):
    assert all(t.category in CATEGORIES for t in ledger)
    assert Counter(t.category for t in ledger) == Counter({
        "capital_call": 11, "distribution": 10, "bank_charges": 6, "interest_income": 3, "accrual_settlement": 2,
        "expense": 2, "management_fee": 1, "dividend_income": 1, "sale_proceeds": 1, "investment_purchase": 1,
        "expense_refund": 1,
    })


def test_llm_cannot_change_source_values(statement, ledger):
    for source, classified in zip(statement.transactions, ledger):
        assert (source.date, source.debit, source.credit, source.balance, source.reference, source.description) == (
            classified.date, classified.debit, classified.credit, classified.balance, classified.reference,
            classified.description)


def test_investor_without_a_printed_code_maps_to_lp10(ledger):
    assert row(ledger, "MEHTA FAM TRST").lp_code == "LP10"


def test_investor_paying_in_two_wires_keeps_one_code(ledger):
    assert {t.lp_code for t in ledger if "BLUEWATER" in t.description and t.category == "capital_call"} == {"LP07"}


def test_company_names_are_extracted_and_normalized(ledger):
    assert row(ledger, "NORDIC").company == "Fjord Payments AS"
    assert row(ledger, "IONMERE").company == "Ionmere Materials Ltd"
    assert normalize_company("AURORA LOGISTICS PVT LTD") == "Aurora Logistics Pvt Ltd"


def test_lp_and_company_are_blank_where_they_do_not_apply(ledger):
    fee = row(ledger, "MGMT FEE")
    assert fee.lp_code is None and fee.company is None


@pytest.mark.parametrize("field,value,text,expected", [
    ("category", "capital_call", "MGMT FEE", "cannot be a debit"),
    ("category", "expense", "HARBOURSIDE", "settles the Q3 liability"),
    ("category", "accrual_settlement", "LEGAL", "matches none"),
])
def test_impossible_categories_are_rejected(ledger, context, field, value, text, expected):
    txn = row(ledger, text)
    errors = validate_answer(txn, LLMAnswer(id=1, **{field: value}), context)
    assert any(expected in e for e in errors)


def test_invented_lp_codes_are_rejected(ledger, context):
    txn = row(ledger, "MEHTA FAM TRST")
    assert validate_answer(txn, LLMAnswer(id=1, category="capital_call", lp_code="LP99"), context)
    assert validate_answer(txn, LLMAnswer(id=1, category="capital_call", lp_code="LP01"), context)
    printed = row(ledger, "GPF CC07 LP02")
    assert validate_answer(printed, LLMAnswer(id=1, category="capital_call", lp_code="LP01"), context)


def test_company_not_in_the_description_is_rejected(ledger, context):
    txn = row(ledger, "NORDIC")
    assert validate_answer(txn, LLMAnswer(id=1, category="sale_proceeds", company="Acme Corp"), context)
    assert validate_answer(txn, LLMAnswer(id=1, category="sale_proceeds"), context)


def test_the_buyer_on_a_sale_is_rejected_because_the_fund_never_held_it(ledger, context):
    """Seen with a real model: it named the buyer, which does appear in the description."""
    txn = row(ledger, "NORDIC")
    errors = validate_answer(txn, LLMAnswer(id=1, category="sale_proceeds", company="NORDIC ACQUISITIONS AS SPA"), context)
    assert any("not a holding" in e for e in errors)
    assert not validate_answer(txn, LLMAnswer(id=1, category="sale_proceeds", company="FJORD PAYMENTS AS"), context)


def test_malformed_json_is_retried(statement, context):
    one = statement.transactions[:1]
    good = json.dumps({"transactions": [{"id": 1, "category": "management_fee", "rationale": "fee"}]})
    llm = FakeLLM(replies=["this is not json", good])
    assert classify_transactions(one, context, llm)[0].category == "management_fee"
    assert len(llm.calls) == 2
    assert "problems" in llm.calls[1][-1]["content"]


def test_unknown_category_is_retried_with_the_reason(statement, context):
    one = statement.transactions[:1]
    bad = json.dumps({"transactions": [{"id": 1, "category": "fees"}]})
    good = json.dumps({"transactions": [{"id": 1, "category": "management_fee"}]})
    llm = FakeLLM(replies=[bad, good])
    assert classify_transactions(one, context, llm)[0].category == "management_fee"
    assert "Categories allowed" in llm.calls[1][-1]["content"]


def test_only_unresolved_rows_are_asked_again(statement, context):
    two = statement.transactions[:2]
    first = json.dumps({"transactions": [{"id": 1, "category": "management_fee"}]})
    second = json.dumps({"transactions": [{"id": 2, "category": "accrual_settlement"}]})
    llm = FakeLLM(replies=[first, second])
    classify_transactions(two, context, llm)
    assert '"id": 1' not in llm.calls[1][-1]["content"]


def test_run_stops_when_the_llm_never_gives_a_valid_answer(statement, context):
    llm = FakeLLM(replies=["nope"] * MAX_ATTEMPTS)
    with pytest.raises(ClassificationError):
        classify_transactions(statement.transactions[:1], context, llm)


@pytest.mark.parametrize("text", [
    '{"transactions": []}',
    '```json\n{"transactions": []}\n```',
    '<think>let me see {x}</think>{"transactions": []}',
    'Here you go: {"transactions": []} Done.',
])
def test_extract_json_handles_model_styles(text):
    assert extract_json(text) == {"transactions": []}


SETTINGS = LLMSettings("secret-key", "https://llm.test/v1", "test-model")
OK_BODY = {"choices": [{"message": {"content": "hello"}}]}


def client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rate_limit_waits_for_retry_after_then_succeeds():
    replies = [httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, json=OK_BODY)]
    waits = []
    llm = OpenAICompatibleLLM(SETTINGS, client_with(lambda request: replies.pop(0)), sleep=waits.append)
    assert llm([{"role": "user", "content": "hi"}]) == "hello"
    assert waits == [7.0]


def test_request_uses_configured_endpoint_model_and_key():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    OpenAICompatibleLLM(SETTINGS, client_with(handler))([])
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"]["model"] == "test-model"


def test_falls_back_when_json_mode_is_rejected():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(400 if len(bodies) == 1 else 200, json=OK_BODY)

    assert OpenAICompatibleLLM(SETTINGS, client_with(handler))([]) == "hello"
    assert "response_format" in bodies[0] and "response_format" not in bodies[1]


def test_bad_credentials_fail_at_once_without_exposing_the_key():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    with pytest.raises(LLMError) as error:
        OpenAICompatibleLLM(SETTINGS, client_with(handler))([])
    assert len(calls) == 1
    assert "secret-key" not in str(error.value)


def test_gives_up_after_repeated_server_errors():
    llm = OpenAICompatibleLLM(SETTINGS, client_with(lambda request: httpx.Response(503)), sleep=lambda s: None)
    with pytest.raises(LLMError):
        llm([])


def test_network_errors_are_retried():
    replies = [httpx.ConnectError("down")]

    def handler(request):
        if replies:
            raise replies.pop()
        return httpx.Response(200, json=OK_BODY)

    assert OpenAICompatibleLLM(SETTINGS, client_with(handler), sleep=lambda s: None)([]) == "hello"
