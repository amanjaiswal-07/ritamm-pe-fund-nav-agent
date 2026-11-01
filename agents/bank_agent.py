"""Agent A: Bank & Ingestion Agent. Owns the statement PDF and its categorization.

When triggered it parses and reconciles the statement, categorizes each row with the LLM,
then holds the two-turn conversation with Agent B to obtain the Q4 marks. If B rejects the
purchases and sales, A re-checks its categorization with B's feedback and answers again.

Run as a process with: python -m agents.bank_agent --pack-dir pack --output-dir output
"""

import argparse
import json
import logging
from pathlib import Path

import uvicorn

import config
from agents.a2a import (A2AClient, A2AError, INVALID_PARAMS, RPC_PATH, build_agent_card, create_a2a_app, data_part,
                        find_data, new_message, new_task, task_text)
from models import Transaction, money
from services.classifier import LLM, FundContext, OpenAICompatibleLLM, classify_transactions
from services.pdf_parser import Statement, parse_statement
from services.q3_loader import load_nav_summary
from services.reconciliation import reconcile

logger = logging.getLogger("agent_a")

MAX_ANSWERS = 3  # How many times A will answer B's question before giving up.


class CloseFailed(Exception):
    """The quarter close could not be completed; the message says why."""


def derive_purchases_and_sales(transactions: list[Transaction]) -> dict:
    """Agent A's answer to B's question, built only from its own categorized rows."""
    return {
        "purchases": [
            {"company": t.company, "date": t.date, "amount_usd": money(t.debit)}
            for t in transactions if t.category == "investment_purchase"
        ],
        "sales": [
            {"company": t.company, "date": t.date, "proceeds_usd": money(t.credit)}
            for t in transactions if t.category == "sale_proceeds"
        ],
    }


class BankAgent:
    def __init__(self, pack_dir: Path, output_dir: Path, llm: LLM, peer: A2AClient):
        self._pack_dir = pack_dir
        self._output_dir = output_dir
        self._llm = llm
        self._peer = peer

    def on_message(self, message: dict) -> dict:
        """A2A entry point: a data part naming the fund and date triggers a close."""
        request = find_data(message, "fund")
        if request is None:
            raise A2AError(INVALID_PARAMS, "Send a data part with fund and valuation_date")
        if request.get("fund") != config.FUND_CODE or request.get("valuation_date") != config.VALUATION_DATE:
            return new_task("rejected", f"This agent only closes {config.FUND_CODE} at {config.VALUATION_DATE}.")
        try:
            return new_task("completed", self.close_quarter())
        except Exception as exc:  # Any failure must reach the launcher and fail the run.
            logger.exception("Close failed")
            return new_task("failed", f"{type(exc).__name__}: {exc}")

    def close_quarter(self) -> str:
        statement = parse_statement(self._pack_dir / config.BANK_STATEMENT_FILE)
        logger.info("Parsed %d transactions", len(statement.transactions))

        check = reconcile(statement)
        if not check.ok:
            raise CloseFailed("Bank statement does not reconcile: " + "; ".join(check.problems))
        logger.info("Statement reconciles: opening %s + credits %s - debits %s = closing %s",
                    check.opening_balance, check.total_credits, check.total_debits, check.closing_balance)

        summary = load_nav_summary(self._pack_dir / config.Q3_NAV_FILE)
        context = FundContext("The Greatness PE Fund, L.P.", summary.investors, summary.liabilities,
                              [h.company for h in summary.holdings])
        transactions = classify_transactions(statement.transactions, context, self._llm)
        self._write_transactions(transactions)
        return self._converse_with_b(statement, transactions, context)

    def _converse_with_b(self, statement: Statement, transactions: list[Transaction], context: FundContext) -> str:
        endpoint = self._peer.discover()["url"]
        logger.info("Turn 1: asking Agent B for marks at %s", endpoint)
        task = self._peer.send(endpoint, new_message(
            [data_part({"fund": config.FUND_CODE, "valuation_date": config.VALUATION_DATE})]))

        answers = 0
        previous_lists = None
        while task["status"]["state"] == "input-required":
            if answers:
                # B says the lists are wrong, so the categorization is what needs another look.
                logger.warning("Agent B rejected the lists: %s", task_text(task))
                transactions = classify_transactions(statement.transactions, context, self._llm, task_text(task))
                self._write_transactions(transactions)
            lists = derive_purchases_and_sales(transactions)
            if answers >= MAX_ANSWERS or lists == previous_lists:
                raise CloseFailed(f"Could not satisfy Agent B: {task_text(task)}")
            previous_lists = lists
            answers += 1

            logger.info("Turn 2: sending %d purchases and %d sales", len(lists["purchases"]), len(lists["sales"]))
            # The first part is the brief's exact shape. B needs the cash ledger for the NAV, and
            # sending it here keeps A2A the only channel between the agents and the talk at two turns.
            ledger = {"opening_balance": money(statement.opening_balance),
                      "closing_balance": money(statement.closing_balance),
                      "transactions": [t.to_output() for t in transactions]}
            task = self._peer.send(endpoint, new_message(
                [data_part(lists), data_part({"ledger": ledger})], task_id=task["id"], context_id=task["contextId"]))

        if task["status"]["state"] != "completed" or not task.get("artifacts"):
            raise CloseFailed(f"Agent B ended in state {task['status']['state']}: {task_text(task)}")

        marks = task["artifacts"][0]["parts"][0]["data"]
        (self._output_dir / "marks.json").write_text(json.dumps(marks, indent=2), encoding="utf-8")
        logger.info("Saved marks.json; Agent B says: %s", task_text(task))

        if task.get("metadata", {}).get("nav_status") != "ok":
            raise CloseFailed(f"Agent B could not finish the NAV: {task_text(task)}")
        return task_text(task)

    def _write_transactions(self, transactions: list[Transaction]) -> None:
        rows = [t.to_output() for t in transactions]
        (self._output_dir / "transactions.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    port_a, port_b = config.agent_ports()
    peer = A2AClient("agent-a", "agent-b", f"http://{config.HOST}:{port_b}", args.output_dir / "a2a_log.jsonl")
    agent = BankAgent(args.pack_dir, args.output_dir, OpenAICompatibleLLM(config.load_llm_settings()), peer)
    uvicorn.run(create_app(agent, port_a), host=config.HOST, port=port_a, log_level="warning")


def create_app(agent: BankAgent, port: int):
    card = build_agent_card(
        name="Bank & Ingestion Agent",
        description="Parses a fund's bank statement, categorizes each transaction with an LLM and reconciles it.",
        url=f"http://{config.HOST}:{port}{RPC_PATH}",
        skills=[{"id": "close-quarter-bank", "name": "Ingest bank statement",
                 "description": "Given a fund and valuation date, ingest the statement and fetch verified Q4 marks.",
                 "tags": ["bank", "reconciliation", "categorization"]}],
    )
    return create_a2a_app(card, agent.on_message)


if __name__ == "__main__":
    main()
