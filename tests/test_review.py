"""The four review rules, on the real Q4 marks and on constructed cases that the real data lacks."""

from decimal import Decimal

import pytest

import config
from services.q3_loader import Q3Valuation
from services.review import ReviewError, review_marks

RATE = config.EURUSD_RATE
THRESHOLD = config.MATERIALITY_THRESHOLD_PCT


@pytest.fixture(scope="module")
def positions(q3_valuations, agent_b_data):
    data = agent_b_data
    found = review_marks(q3_valuations, data["marks"], data["notes"], data["purchases"], data["sales"], THRESHOLD, RATE)
    return {p.company: p for p in found}


def test_flags_on_the_real_data(positions):
    assert {name: p.flags for name, p in positions.items() if p.flags} == {
        "Bluetarn Software Inc": ["R1"],
        "Cedar Health Holdings": ["R1"],
        "Delta Agritech Ltd": ["R2"],
        "Harbor Analytics Inc": ["R1"],
        "Ionmere Materials Ltd": ["R3"],
    }


def test_every_company_has_one_entry(positions):
    assert len(positions) == 9


def test_every_flag_has_a_one_line_reason(positions):
    for position in positions.values():
        for flag in position.flags:
            assert flag in position.reason and "\n" not in position.reason


def test_euro_mark_is_measured_in_euros(positions):
    ember = positions["Ember Retail GmbH"]
    assert ember.currency == "EUR"
    assert ember.q3_fair_value == Decimal("13500000")
    assert ember.q4_fair_value == Decimal("13100000")
    assert round(ember.change_pct, 2) == Decimal("-2.96")
    assert ember.flags == []


def test_euro_mark_converts_at_the_ecb_rate(positions):
    assert positions["Ember Retail GmbH"].q4_fair_value_usd == Decimal("13972460.00")


def test_fx_move_alone_cannot_trigger_r1(q3_valuations):
    """Flat in EUR with a very different rate: the USD value moves about 54%, but the company did not."""
    ember = [v for v in q3_valuations if v.company == "Ember Retail GmbH"]
    mark = {"company": "Ember Retail GmbH", "currency": "EUR", "fair_value": "13500000", "valuation_method": "Market multiples"}
    position = review_marks(ember, [mark], [], [], [], THRESHOLD, Decimal("1.5"))[0]
    assert position.flags == []


def test_sold_company_without_a_mark_gets_no_flag_and_no_value(positions):
    fjord = positions["Fjord Payments AS"]
    assert fjord.flags == [] and not fjord.held_at_q4
    assert fjord.q4_fair_value is None and fjord.q4_fair_value_usd is None
    assert "Sold" in fjord.reason


def test_r3_carries_the_recent_purchase_at_cost(positions):
    ionmere = positions["Ionmere Materials Ltd"]
    assert ionmere.q4_fair_value_usd == Decimal("14000000")
    assert ionmere.held_at_q4 and ionmere.q4_method is None and ionmere.change_pct is None
    assert "Not yet valued" in ionmere.reason


def valuation(company="Co", fair_value="100", method="DCF", currency="USD"):
    return Q3Valuation(company, currency, Decimal(fair_value), Decimal("1"), Decimal(fair_value), method)


def mark(company="Co", fair_value="100", method="DCF", currency="USD"):
    return {"company": company, "currency": currency, "fair_value": fair_value, "valuation_method": method}


@pytest.mark.parametrize("q4,flagged", [("115", False), ("115.01", True), ("85", False), ("84.99", True)])
def test_r1_fires_only_beyond_the_threshold(q4, flagged):
    position = review_marks([valuation()], [mark(fair_value=q4)], [], [], [], THRESHOLD, RATE)[0]
    assert ("R1" in position.flags) == flagged


def test_r2_fires_on_a_method_change_alone():
    assert review_marks([valuation()], [mark(method="Multiples")], [], [], [], THRESHOLD, RATE)[0].flags == ["R2"]


def test_r1_and_r2_can_both_fire():
    position = review_marks([valuation()], [mark(fair_value="200", method="Multiples")], [], [], [], THRESHOLD, RATE)[0]
    assert position.flags == ["R1", "R2"]


def test_r4_mark_for_a_sold_company_is_flagged_and_excluded():
    sale = [{"company": "Co", "date": "2022-11-18", "proceeds_usd": "120"}]
    position = review_marks([valuation()], [mark()], [], [], sale, THRESHOLD, RATE)[0]
    assert position.flags == ["R4"]
    assert not position.held_at_q4 and position.q4_fair_value_usd is None


def test_r4_mark_for_a_company_never_held_is_flagged():
    position = review_marks([], [mark()], [], [], [], THRESHOLD, RATE)[0]
    assert position.flags == ["R4"]


def test_r3_older_holding_without_a_mark_stays_in_the_nav():
    position = review_marks([valuation()], [], [], [], [], THRESHOLD, RATE)[0]
    assert position.flags == ["R3"] and position.q4_fair_value_usd == Decimal("100")


def test_unsupported_currency_stops_the_review():
    with pytest.raises(ReviewError):
        review_marks([valuation(currency="GBP")], [mark(currency="GBP")], [], [], [], THRESHOLD, RATE)
