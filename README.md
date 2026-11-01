# Quarter close for The Greatness PE Fund

Two agents close Q4 2022 for a private equity fund. One reads and categorizes the bank statement, the other holds the valuer's marks. They talk over A2A v0.3, and the run ends with a NAV that is computed two separate ways and must agree to the cent.

## Run it

Needs Python 3.11 or newer.

```bash
pip install -r requirements.txt
python main.py ./pack
```

The pack directory is optional and defaults to `./pack` (or `PACK_DIR` if set). There are no other arguments. Results go to `./output`.

Settings come from the environment, or from a `.env` file (copy `.env.example`). Real environment variables win over `.env`.

| Variable | Purpose |
|---|---|
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | Required. Any OpenAI-compatible endpoint |
| `AGENT_A_PORT`, `AGENT_B_PORT` | Optional. Default to 8001 and 8002 |

The exit code is 0 only if every step worked and the bridge balanced. Both agents stop when the run ends.

Tests run offline, with no LLM or network. They read the real files in `./pack`:

```bash
python -m pytest
```

The submitted zip includes `pack/` and `output/`. The Git repository leaves them out, because the pack is marked confidential, so a fresh clone needs the pack added before it can run.

## How it works

```
main.py: starts both processes, waits for their agent cards, triggers A, stops both

Agent A (Bank & Ingestion)                  Agent B (Valuation & NAV)
  parse PDF, reconcile balances               owns agent_b_data.json and the Q3 numbers
  LLM categorizes every row
    1. {"fund", "valuation_date"}  ------->
    <-------  2. input-required: send your purchases and sales
    3. same taskId and contextId:
       purchases, sales, ledger  ---------->  compares with its records
    <-------  4. completed, artifacts[0] = marks and notes
  writes transactions, marks, a2a_log         reviews marks, computes NAV and bridge,
                                              writes review.json and nav.json
```

If the lists don't match, B stays in `input-required` and says how many items matched. A then re-categorizes with that feedback and answers again, up to three answers in total.

The agents are separate processes, and HTTP is their only link. B's marks never reach A before verification, and A never opens `agent_b_data.json`. A does read the investor list, Q3 liabilities and Q3 holdings from the NAV summary, because the LLM needs them to identify investors, settlements and what was sold.

| Path | What it does |
|---|---|
| `main.py` | Launcher, with no business logic |
| `agents/a2a.py` | JSON-RPC server wrapper, agent cards, client, conversation log |
| `agents/bank_agent.py` | Agent A |
| `agents/valuation_agent.py` | Agent B |
| `services/pdf_parser.py` | Statement to rows, using word positions |
| `services/reconciliation.py` | Balance checks |
| `services/classifier.py` | LLM client, prompt, and the checks on each answer |
| `services/verification.py` | Compares purchases and sales with B's records |
| `services/q3_loader.py` | Reads the Excel files and checks they agree |
| `services/review.py` | Rules R1 to R4 and each stake's Q4 value |
| `services/nav.py` | NAV, bridge and cross-checks |

## Output files

| File | Contents |
|---|---|
| `transactions.json` | All rows, with the required fields plus the LLM's `rationale` |
| `a2a_log.jsonl` | One line per request and per response between the agents, timestamped. The launcher's trigger to A and agent card fetches are not logged |
| `marks.json` | The artifact data A received from B, unchanged. Amounts stay as strings, as in B's source file |
| `review.json` | `threshold_pct`, `eurusd_rate` and its source, then one entry per company under `companies`. `q3_fair_value` and `q4_fair_value` are in the company's own currency; `_usd` fields are added |
| `nav.json` | The required fields, plus `management_fee`, expense and income breakdowns, holdings, per-LP activity and `checks` |

## The numbers

Amounts are `Decimal` in memory and become 2-decimal floats only in output.

- **NAV** = closing cash + investments at fair value in USD - accruals from sheet 2 of the NAV summary.
- **Bridge** = Q3 NAV + capital called - distributions + income - expenses + realized gain + change in unrealized gain.
- **Expenses** = management fee + other expenses paid - refunds + bank charges + Q4 accruals. Settlements of Q3 liabilities are left out because they reduce cash and a liability equally.
- **Realized gain** = proceeds - USD cost from the NAV summary.
- **Change in unrealized gain** = Q4 total - Q3 total, so a sold stake's Q3 gain reverses.

The bridge is reported as it comes out. Nothing adjusts it, and a non-zero difference fails the run.

## What the bridge can't catch

The bridge finds rows moved between cash-only and P&L categories, such as a settlement booked as an expense. It can't find a mistake that stays within the same side of the bridge: a distribution booked as an expense lowers NAV in the bridge just the same, and a wrong LP code changes nothing in it.

Three other checks help here. Purchases and sales are matched against B's records. Each answer is checked on arrival (see `validate_answer`). Calls and distributions must be the same percentage of commitment for every LP, which `nav.json` reports under `checks`. A failed pro-rata check is logged and recorded in `nav.json` and shown in the final summary, but it doesn't fail the run on its own.

## What was tested with real models

I ran the whole pipeline on Groq's free tier with `openai/gpt-oss-20b`, `openai/gpt-oss-120b` and `qwen/qwen3.8-27b`, and compared each row with my own categorization by hand. All three matched on every row and gave the same NAV. A run takes one to two minutes, mostly waiting on rate limits. I have not tried other providers.

Models do get things wrong on the first try, so expect retry warnings in the log. In testing, one named the buyer instead of the company sold, and others left the company blank on the purchase. The checks caught both. The first one made B reject the lists and stopped the run, which led to the rule that a sale must be of a Q3 holding.

## Known gaps

- I only ran this on Windows with Python 3.13. For 3.11 I checked the syntax and that the pinned packages install, but did not run it. Linux and macOS are untested.
- The parser is built for this bank's layout. A different statement would fail with a parse error, not produce wrong rows, but it would need its own layout handling.
- The ECB rate is pinned in `config.py`. A new quarter needs a new rate.
- Only USD and EUR are supported. Any other currency stops the review.
- Fund code and valuation date are constants in `config.py`, so this closes one fund and one date.
- Agent B keeps open tasks in memory, so a restart loses them.
- The A2A endpoints have no authentication. They bind to localhost only.
- A2A coverage is `message/send` and agent cards, as the brief specifies. No streaming, `tasks/get` or push notifications.
- Categories can differ between runs or models, since an LLM picks them. The checks above should stop a wrong answer from changing a number, but they don't prove every category is right. For example, a dividend booked as interest passes everything because both count as income.

## Taking it to production

1. Keep every LLM answer with its prompt and model version, and send low-confidence rows to a person for approval.
2. Put each fund's rules (categories, LP register, FX source) in data, so a new fund needs no code change.
3. Fetch and archive ECB rates with the source response, and record who approved each one.
4. Move task state to a database and add authentication between agents.
5. Add a statement layout per bank, with a test pack for each.
6. Add a review screen where an accountant clears flags before anything reaches investors.
