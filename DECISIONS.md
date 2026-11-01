# Decisions

## Decision 1: How Agent B gets the cash data
**Decision:** Agent A's second-turn message has the brief's exact `purchases` and `sales` data part, then a second data part with the categorized ledger. B uses the ledger for its NAV and writes `review.json` and `nav.json` itself. `artifacts[0]` holds only the marks.
**Alternatives:** A third A2A task for the NAV, which is the cleanest separation but no longer the two-turn conversation the brief describes. A shared file, which would tie the agents to one filesystem and add a channel outside A2A. Having `main.py` compute the NAV, which would take the work away from B, the agent that owns it.
**Why:** The log stays at four lines and shows the conversation the brief specifies, and A2A remains the only link between the agents. If a client sends only the brief's shape, B still releases the marks and just skips the NAV. The cost is that the second message carries more than the question asked for, and I accepted that.

## Decision 2: LLM decides categories, code checks the answers
**Decision:** The LLM picks category, LP code and company. Code rejects answers that cannot be right, sends the reason back to the LLM, and stops the run after four failed attempts. Amounts, dates and references come only from the PDF. There is no keyword fallback.
**Alternatives:** Keyword rules, which the brief rules out and which break on lines like the Mehta wire, where no LP code is printed. Trusting the LLM with no checks, which would let one bad answer reach the NAV. Falling back to rules when the LLM fails, which would hide the failure and give figures nobody can explain.
**Why:** The evaluator's model may differ from mine, so the result can't depend on one model's habits. The checks use facts that hold whatever the model is: a distribution can't be a credit, an LP code must be in the register, a named company must appear in the description, a settlement must match a Q3 liability. A failed run is better than a wrong NAV that looks fine.

## Decision 3: Several independent checks, none adjustable
**Decision:** Check at each stage against a different source. Statement arithmetic and its printed totals. Q3 files against each other. Purchases and sales against B's records. Opening cash against Q3 cash. Balance-sheet NAV against the bridge. Each LP's calls and distributions against their commitment. Differences are reported and never corrected in code.
**Alternatives:** Relying on the bridge alone, which is the brief's stated check. But my tests show it misses some errors: a wrong LP code leaves it at 0.00. Testing only against expected totals, which would pass for a wrong reason if two errors cancelled out.
**Why:** The pro-rata check came from looking at the data. Every LP paid exactly 8% of commitment and received exactly 9%, so a wrong LP code stands out straight away, including on the Mehta wire where the LLM has to work out the investor from its name. Tests also break categories on purpose to confirm the bridge fails, so a 0.00 difference means something.
