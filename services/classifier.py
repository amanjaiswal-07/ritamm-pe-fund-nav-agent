"""LLM categorization of bank transactions, with code checks around every answer.

The LLM decides category, LP code and company, and gives a one-line rationale. It sees
amounts for context but cannot change them: its answer only fills those four fields.

Each answer is checked against facts in code (right side of the statement, LP in the
register, company named in the description, settlement matches a Q3 liability). A failed
answer goes back to the LLM with the reason. If it still fails, the run stops. There is
deliberately no keyword fallback.

`llm` is any callable taking chat messages and returning text, so tests can inject a fake.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ValidationError

from config import LLMSettings
from models import CATEGORIES, CREDIT_CATEGORIES, Category, Transaction

logger = logging.getLogger(__name__)

LLM = Callable[[list[dict]], str]

BATCH_SIZE = 10
MAX_ATTEMPTS = 4  # LLM tries per batch before giving up on bad answers.
MAX_HTTP_RETRIES = 6  # Retries per call on rate limits, server errors and timeouts.
MAX_BACKOFF_SECONDS = 65.0  # Free-tier rate limits generally reset within a minute.
REQUEST_TIMEOUT_SECONDS = 120.0

# Words too common to identify an investor or creditor by name.
GENERIC_NAME_WORDS = {
    "fund", "funds", "trust", "family", "board", "office", "state", "pension", "services", "accrual",
    "payable", "administration", "audit", "insurance", "foundation", "retirement", "university",
    "teachers", "sovereign", "reserve", "endowment", "limited",
}

# Display casing for company names, since the bank prints everything in capitals.
LEGAL_SUFFIXES = {"LTD": "Ltd", "INC": "Inc", "LLC": "LLC", "LLP": "LLP", "PVT": "Pvt", "AS": "AS",
                  "GMBH": "GmbH", "CORP": "Corp", "PLC": "PLC", "SA": "SA", "AG": "AG"}


class LLMError(Exception):
    """The LLM endpoint could not be reached or refused the request."""


class ClassificationError(Exception):
    """The LLM could not produce a valid answer for some transactions."""


@dataclass(frozen=True)
class FundContext:
    """Q3 facts the LLM needs. Agent A reads these from the NAV summary, never agent_b_data.json."""

    fund_name: str
    investors: list  # q3_loader.Investor
    q3_liabilities: list  # q3_loader.NamedAmount
    q3_holdings: list[str]  # Company names; a sale must be of one of these.


class LLMAnswer(BaseModel):
    id: int
    category: Category
    lp_code: str | None = None
    company: str | None = None
    rationale: str = ""


class OpenAICompatibleLLM:
    """Minimal chat-completions client for any OpenAI-compatible provider."""

    def __init__(self, settings: LLMSettings, client: httpx.Client | None = None, sleep=time.sleep):
        self._settings = settings
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._sleep = sleep
        self._json_mode = True  # Switched off if the provider or model rejects it.

    def __call__(self, messages: list[dict]) -> str:
        url = f"{self._settings.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._settings.api_key}"}
        last_problem = ""

        for attempt in range(MAX_HTTP_RETRIES + 1):
            body = {"model": self._settings.model, "messages": messages, "temperature": 0}
            if self._json_mode:
                body["response_format"] = {"type": "json_object"}

            try:
                response = self._client.post(url, headers=headers, json=body)
            except httpx.TransportError as exc:
                last_problem = f"network error: {type(exc).__name__}"
                self._sleep(min(2.0**attempt, MAX_BACKOFF_SECONDS))
                continue

            if response.status_code == 200:
                try:
                    return response.json()["choices"][0]["message"].get("content") or ""
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise LLMError(f"Unexpected response shape from {url}") from exc
            if response.status_code == 400 and self._json_mode:
                # Some models reject JSON mode, or fail its strict output check. Parse text instead.
                logger.info("Provider returned 400 in JSON mode; retrying without it")
                self._json_mode = False
                continue
            if response.status_code in (401, 403):
                raise LLMError(f"LLM rejected the credentials (HTTP {response.status_code}). Check LLM_API_KEY.")
            if response.status_code == 429 or response.status_code >= 500:
                last_problem = f"HTTP {response.status_code}"
                wait = _retry_after(response) or 2.0**attempt
                logger.info("LLM returned %s; waiting %.1fs", last_problem, min(wait, MAX_BACKOFF_SECONDS))
                self._sleep(min(wait, MAX_BACKOFF_SECONDS))
                continue
            raise LLMError(f"LLM request failed (HTTP {response.status_code}): {response.text[:300]}")

        raise LLMError(f"LLM unavailable after {MAX_HTTP_RETRIES} retries ({last_problem})")


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None


def extract_json(text: str):
    """Parse JSON from model output that may include reasoning tags, code fences or prose."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if not starts or end < 0:
            raise ValueError("No JSON found in the LLM response") from None
        try:
            return json.loads(cleaned[min(starts) : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in the LLM response: {exc}") from None


def normalize_company(name: str) -> str:
    """Give a company name stable casing, whatever casing the LLM returned."""
    words = name.split()
    return " ".join(LEGAL_SUFFIXES.get(w.upper().strip(".,"), w.capitalize() if w.isupper() else w) for w in words)


def _name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z]+", name.lower())
    return {w for w in words if len(w) >= 4 and w not in GENERIC_NAME_WORDS}


def _mentions(description: str, name: str) -> bool:
    """True if a distinctive word of `name` appears in the description."""
    return bool(_name_tokens(name) & set(re.findall(r"[a-z]+", description.lower())))


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def validate_answer(txn: Transaction, answer: LLMAnswer, context: FundContext) -> list[str]:
    """Return the reasons this answer cannot be right. An empty list means it passes."""
    errors = []
    is_credit = txn.credit > 0
    if (answer.category in CREDIT_CATEGORIES) != is_credit:
        errors.append(f"category {answer.category} cannot be a {'credit' if is_credit else 'debit'}")

    if answer.category in ("capital_call", "distribution"):
        investor = next((i for i in context.investors if i.code == (answer.lp_code or "").upper()), None)
        printed = re.search(r"\bLP\d{2}\b", txn.description)
        if investor is None:
            errors.append(f"lp_code {answer.lp_code!r} is not in the LP register")
        elif printed and printed.group() != investor.code:
            errors.append(f"the description says {printed.group()}, not {investor.code}")
        elif not printed and not _mentions(txn.description, investor.name):
            errors.append(f"{investor.name} ({investor.code}) is not named in the description")

    if answer.category in ("investment_purchase", "sale_proceeds"):
        if not answer.company:
            errors.append('company is required here: set "company" to the portfolio company name printed in the description')
        elif _squash(answer.company) not in _squash(txn.description):
            errors.append(f"company {answer.company!r} does not appear in the description")
        elif answer.category == "sale_proceeds" and _squash(answer.company) not in map(_squash, context.q3_holdings):
            errors.append(f"{answer.company!r} is not a holding, so the fund cannot have sold it. "
                          "Name the holding that was sold, not the buyer")

    q3_amounts = {liability.amount for liability in context.q3_liabilities}
    if answer.category == "accrual_settlement" and txn.debit not in q3_amounts:
        errors.append("accrual_settlement must settle a liability on the Q3 statement, and this amount matches none")
    for liability in context.q3_liabilities:
        if txn.debit == liability.amount and _mentions(txn.description, liability.name):
            if answer.category != "accrual_settlement":
                errors.append(f"this payment settles the Q3 liability {liability.name!r}")
    return errors


def _apply(txn: Transaction, answer: LLMAnswer) -> Transaction:
    """Copy the LLM's fields onto the row, keeping LP and company only where the brief uses them."""
    wants_lp = answer.category in ("capital_call", "distribution")
    wants_company = answer.category in ("investment_purchase", "sale_proceeds")
    return txn.model_copy(
        update={
            "category": answer.category,
            "lp_code": answer.lp_code.upper() if wants_lp and answer.lp_code else None,
            "company": normalize_company(answer.company) if wants_company and answer.company else None,
            "rationale": answer.rationale.strip(),
        }
    )


def build_system_prompt(context: FundContext) -> str:
    investors = "\n".join(f"- {i.code}: {i.name}" for i in context.investors)
    liabilities = "\n".join(f"- {l.name}: {l.amount:,.2f}" for l in context.q3_liabilities)
    holdings = "\n".join(f"- {name}" for name in context.q3_holdings)
    return f"""You are a careful fund accountant categorizing bank statement lines for {context.fund_name}, \
a private equity fund, for the quarter ending 31 December 2022.

Choose exactly one category for each line:
- capital_call: an investor (LP) paying money into the fund
- distribution: the fund paying cash back to an investor
- management_fee: the fee paid to the fund's manager or adviser
- investment_purchase: the fund buying a stake in a portfolio company
- sale_proceeds: cash received for selling a stake in a portfolio company
- dividend_income: a dividend received from a portfolio company
- interest_income: interest paid by the bank on the account
- bank_charges: fees charged by the bank itself, such as maintenance or wire charges
- expense: any other fee or cost paid in the quarter, even if it covers a later period
- expense_refund: money returned by a supplier for an earlier expense
- accrual_settlement: a payment that settles a liability already on the 30 September statement

Liabilities on the 30 September statement of net assets:
{liabilities}

Limited partners of the fund:
{investors}

Portfolio companies held at 30 September:
{holdings}

Rules:
- For capital_call and distribution, set lp_code to the investor's code. The bank may abbreviate \
names and may omit the code, so match the wording against the list above. Otherwise lp_code is null.
- For investment_purchase and sale_proceeds, set company to the portfolio company whose stake was \
bought or sold, copied from the description. A sale must be of a company held at 30 September, \
and is never the buyer who sent the money. Otherwise company is null.
- Do not invent or change any date, amount or reference.

Reply with JSON only, in this form:
{{"transactions": [{{"id": 1, "category": "...", "lp_code": "LP code or null", "company": "company name or null", "rationale": "one short reason"}}]}}
Include every id you are given."""


def _user_prompt(batch: dict[int, Transaction], notes: list[str], feedback: str | None) -> str:
    lines = [
        {"id": i, "date": t.date, "description": t.description, "reference": t.reference,
         "debit": str(t.debit), "credit": str(t.credit)}
        for i, t in batch.items()
    ]
    parts = []
    if feedback:
        parts.append(f"The valuation agent rejected the last purchases and sales lists: {feedback}\nRe-check the lines.")
    if notes:
        parts.append("Your previous answer had these problems. Fix them:\n" + "\n".join(f"- {n}" for n in notes))
    parts.append("TRANSACTIONS:\n" + json.dumps(lines, indent=1))
    return "\n\n".join(parts)


def _classify_batch(batch: dict[int, Transaction], context: FundContext, llm: LLM, feedback: str | None) -> dict[int, Transaction]:
    pending = dict(batch)
    done: dict[int, Transaction] = {}
    notes: list[str] = []
    system = build_system_prompt(context)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = llm([{"role": "system", "content": system},
                     {"role": "user", "content": _user_prompt(pending, notes, feedback)}])
        notes = []
        try:
            parsed = extract_json(reply)
        except ValueError as exc:
            logger.warning("Attempt %d: %s", attempt, exc)
            notes.append(f"{exc}. Reply with JSON only.")
            continue

        items = parsed.get("transactions") if isinstance(parsed, dict) else parsed
        for item in items if isinstance(items, list) else []:
            try:
                answer = LLMAnswer.model_validate(item)
            except ValidationError as exc:
                problem = exc.errors()[0]
                notes.append(f"id {item.get('id') if isinstance(item, dict) else '?'}: "
                             f"{problem['msg']}. Categories allowed: {', '.join(CATEGORIES)}")
                continue
            txn = pending.get(answer.id)
            if txn is None:
                continue  # An id we did not ask about, or one already settled: ignore it.
            errors = validate_answer(txn, answer, context)
            if errors:
                notes.append(f"id {answer.id}: " + "; ".join(errors))
            else:
                done[answer.id] = _apply(txn, answer)
                del pending[answer.id]

        if not pending:
            return done
        notes.extend(f"id {i}: no valid answer yet" for i in pending if not any(n.startswith(f"id {i}:") for n in notes))
        logger.warning("Attempt %d: %d lines still unresolved: %s", attempt, len(pending), notes)

    raise ClassificationError(f"No valid category for {len(pending)} line(s) after {MAX_ATTEMPTS} attempts: {notes}")


def classify_transactions(
    transactions: list[Transaction], context: FundContext, llm: LLM, feedback: str | None = None
) -> list[Transaction]:
    """Categorize every row, returning new rows in statement order. Batched for rate limits."""
    indexed = dict(enumerate(transactions, start=1))
    ids = list(indexed)
    result: dict[int, Transaction] = {}
    for start in range(0, len(ids), BATCH_SIZE):
        batch = {i: indexed[i] for i in ids[start : start + BATCH_SIZE]}
        result.update(_classify_batch(batch, context, llm, feedback))
        logger.info("Classified %d of %d transactions", len(result), len(ids))
    return [result[i] for i in ids]
