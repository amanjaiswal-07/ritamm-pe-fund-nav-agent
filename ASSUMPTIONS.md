# Assumptions

## Assumption 1: FX rate and date
- **What I assumed:** EUR/USD of 1.0666, the ECB euro reference rate published on Friday 30 December 2022. Ember's EUR 13,100,000 mark becomes USD 13,972,460.00. Ember's cost stays at USD 14,184,000 as given in the NAV summary.
- **Why:** 31 December 2022 was a Saturday and the ECB publishes no rate at weekends, so the last published rate on or before the valuation date is the one that existed at that date. A later rate would use information from after the valuation date. The same ECB series shows 0.9748 on 30 September 2022, which matches CVP's Q3 report, so this is the source CVP uses. Source: https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?startPeriod=2022-12-28&endPeriod=2023-01-03&format=csvdata
- **What would change if wrong:** Every 0.0001 on the rate moves NAV by USD 1,310. Using the next published rate (1.0683 on 2 January 2023) would raise investments, NAV and the change in unrealized gain by USD 22,270. Flags would not change, because R1 is measured in euros.

## Assumption 2: Materiality threshold
- **What I assumed:** A mark is material if it moved by more than 15% from Q3, in either direction, measured in the company's own currency.
- **Why:** 15% sits in the 10-25% band the brief mentions and leans cautious, because the cost of a false flag is a few minutes of a reviewer's time while a missed one could reach investors. Using local currency keeps exchange-rate moves out of it: Ember fell 2.96% in EUR but rose 6.18% in USD, and only the first says anything about the company. The moves in this data fall into two groups, under 5% and over 17%, so the result does not hinge on the exact figure.
- **What would change if wrong:** Any threshold from 4.47% up to 17.83% flags the same three companies for R1 (Bluetarn +37.50%, Cedar -31.91%, Harbor +17.83%). At 20% or 25%, Harbor would no longer be flagged. NAV does not change either way, because flags don't alter values.

## Assumption 3: Company with no mark yet
- **What I assumed:** Ionmere Materials Ltd is carried at its purchase cost of USD 14,000,000 and flagged R3. Its unrealized gain is therefore zero.
- **Why:** The fund bought it on 15 December 2022, and CVP's note says the first valuation comes at Q1 2023. The price paid about two weeks before quarter-end is the best evidence available, and hint 2 in the brief says this is common practice. The cost comes from the statement and B's records, which agree.
- **What would change if wrong:** Every 1% difference between cost and true value moves NAV by USD 140,000, through investments and the change in unrealized gain.

## Assumption 4: The sold company is not an R4
- **What I assumed:** Fjord Payments AS gets no flag. It was sold on 18 November 2022 and CVP gave a note but no mark.
- **Why:** R4 applies when CVP gives a mark for a company the fund does not hold, and CVP gave none here. R3 applies only to companies the fund still holds. R4 is implemented and tested, but it does not fire on this data.
- **What would change if wrong:** One more name on the flagged list if a reviewer wanted sold companies flagged. NAV would not change, because Fjord is out of the NAV in either case.

## Assumption 5: Which expenses and settlements
- **What I assumed:** The USD 120,000 Coral & Finch and USD 65,000 Harbourside payments are `accrual_settlement`, because each matches a liability on the 30 September statement by creditor and amount. The USD 42,000 D&O premium for 2023 is a Q4 expense, as the brief instructs, not a prepayment. The Haldane & Ross refund of USD 3,500 reduces expenses.
- **Why:** The brief defines each of these. The settlement test is a check in code as well as a prompt instruction.
- **What would change if wrong:** If the settlements were counted as expenses, the bridge would be out by USD 185,000. Treating the D&O premium as a prepayment would add a USD 42,000 asset to NAV.
