"""Agent B: Valuation & NAV Agent. Owns CVP's Q4 marks and the Q3 numbers.

It releases marks only after Agent A's purchases and sales match its records. On release it
also runs its internal steps, using the ledger A sent with its answer: review the marks,
compute the NAV and bridge, and write review.json and nav.json.

Run as a process with: python -m agents.valuation_agent --pack-dir pack --output-dir output
"""

import argparse
import json
import logging
import threading
from pathlib import Path

import uvicorn

import config
from agents.a2a import (A2AError, INVALID_PARAMS, RPC_PATH, TASK_NOT_FOUND, build_agent_card, create_a2a_app, data_part,
                        find_data, new_task)
from models import Transaction, money, to_decimal
from services.nav import compute_nav
from services.q3_loader import check_q3_consistency, load_nav_summary, load_q3_valuations
from services.review import review_marks
from services.verification import verify_records

logger = logging.getLogger("agent_b")

QUESTION = (
    "Before I release the marks, send the fund's Q4 purchases and sales of companies: a data part with "
    '"purchases" [{company, date, amount_usd}] and "sales" [{company, date, proceeds_usd}]. '
    "Send an empty list if there were none."
)


class ValuationAgent:
    def __init__(self, pack_dir: Path, output_dir: Path):
        self._output_dir = output_dir
        self._data = json.loads((pack_dir / config.AGENT_B_DATA_FILE).read_text(encoding="utf-8"))
        self._summary = load_nav_summary(pack_dir / config.Q3_NAV_FILE)
        self._q3 = load_q3_valuations(pack_dir / config.Q3_VALUATION_FILE)
        self._q3_problems = check_q3_consistency(self._summary, self._q3)
        self._open_tasks: dict[str, str] = {}  # taskId -> contextId, for tasks awaiting an answer.
        self._lock = threading.Lock()

    def on_message(self, message: dict) -> dict:
        task_id = message.get("taskId")
        return self._answer_turn(message, task_id) if task_id else self._first_turn(message)

    def _first_turn(self, message: dict) -> dict:
        request = find_data(message, "fund")
        fund = self._data["fund"]
        if request is None or "valuation_date" not in request:
            raise A2AError(INVALID_PARAMS, "Send a data part with fund and valuation_date")
        if request["fund"] != fund["code"] or request["valuation_date"] != self._data["valuation_date"]:
            return new_task("rejected", f"I hold marks only for {fund['code']} at {self._data['valuation_date']}.")

        task = new_task("input-required", QUESTION, context_id=message.get("contextId"))
        with self._lock:
            self._open_tasks[task["id"]] = task["contextId"]
        return task

    def _answer_turn(self, message: dict, task_id: str) -> dict:
        with self._lock:
            context_id = self._open_tasks.get(task_id)
        if context_id is None:
            raise A2AError(TASK_NOT_FOUND, f"No open task {task_id}")
        if message.get("contextId") != context_id:
            raise A2AError(INVALID_PARAMS, "contextId does not match this task")

        def ask_again(text: str) -> dict:
            return new_task("input-required", text, task_id, context_id)

        answer = find_data(message, "purchases")
        if answer is None or "sales" not in answer:
            return ask_again("I need a data part with both purchases and sales. " + QUESTION)
        try:
            result = verify_records(answer, self._data)
        except ValueError as exc:
            return ask_again(f"I could not read your lists: {exc}")
        if not result.ok:
            detail = f" Not on my records: {', '.join(result.unmatched_submitted)}." if result.unmatched_submitted else ""
            return ask_again(
                f"{result.matched} of {result.expected} items on my records matched; you sent "
                f"{result.submitted}.{detail} Fix your lists and send them again."
            )

        with self._lock:
            del self._open_tasks[task_id]  # Completed tasks are terminal.
        artifact = {
            "artifactId": f"marks-{self._data['fund']['code']}-{self._data['valuation_date']}",
            "name": "Q4 valuation marks",
            "parts": [data_part({
                "fund": self._data["fund"]["name"],
                "valuation_date": self._data["valuation_date"],
                "marks": self._data["marks"],
                "notes": self._data["notes"],
            })],
        }
        nav_status, text = self._run_internal_steps(find_data(message, "ledger"))
        return new_task("completed", text, task_id, context_id, [artifact], {"nav_status": nav_status})

    def _run_internal_steps(self, ledger_part: dict | None) -> tuple[str, str]:
        """Review and NAV. Marks are released either way; failures are reported in the status."""
        if ledger_part is None:
            return "skipped", "Marks released. No ledger was sent, so the NAV was not computed."
        try:
            bridge_difference, flagged = self.close(ledger_part["ledger"])
        except Exception as exc:
            logger.exception("NAV failed")
            return "failed", f"Marks released, but the NAV failed: {type(exc).__name__}: {exc}"
        if bridge_difference != 0:
            return "failed", f"Marks released, but the NAV bridge is out by {bridge_difference}. See nav.json."
        return "ok", f"Marks released. NAV computed; bridge difference 0.00. Flagged: {', '.join(flagged) or 'none'}."

    def close(self, ledger: dict) -> tuple[float, list[str]]:
        """Review the marks, compute the NAV and write both files."""
        if self._q3_problems:
            raise ValueError("Q3 files disagree: " + "; ".join(self._q3_problems))

        positions = review_marks(
            self._q3, self._data["marks"], self._data["notes"], self._data["purchases"], self._data["sales"],
            config.MATERIALITY_THRESHOLD_PCT, config.EURUSD_RATE,
        )
        flagged = [p.company for p in positions if p.flags]
        review = {
            "threshold_pct": float(config.MATERIALITY_THRESHOLD_PCT),
            "threshold_currency": "company's own currency",
            "eurusd_rate": float(config.EURUSD_RATE),
            "eurusd_rate_date": config.EURUSD_RATE_DATE,
            "eurusd_source": config.EURUSD_SOURCE,
            "flagged_companies": flagged,
            "companies": [p.to_output() for p in positions],
        }
        self._write("review.json", review)

        nav = compute_nav(
            [Transaction.model_validate(row) for row in ledger["transactions"]],
            to_decimal(ledger["opening_balance"]), to_decimal(ledger["closing_balance"]),
            self._summary, positions, self._data["purchases"], self._data["sales"],
        )
        self._write("nav.json", nav)
        failed = [name for name, passed in nav["checks"].items() if not passed]
        if failed:
            logger.warning("Checks failed: %s", failed)
        return nav["bridge"]["difference"], flagged

    def _write(self, name: str, content: dict) -> None:
        (self._output_dir / name).write_text(json.dumps(content, indent=2), encoding="utf-8")


def create_app(agent: ValuationAgent, port: int):
    card = build_agent_card(
        name="Valuation & NAV Agent",
        description="Releases a fund's Q4 valuation marks once its purchases and sales are verified, then reviews them and computes the NAV.",
        url=f"http://{config.HOST}:{port}{RPC_PATH}",
        skills=[{"id": "release-marks", "name": "Release Q4 marks",
                 "description": "Two-turn exchange: asks for the fund's purchases and sales, then returns marks and notes.",
                 "tags": ["valuation", "nav", "review"]}],
    )
    return create_a2a_app(card, agent.on_message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    _, port_b = config.agent_ports()
    agent = ValuationAgent(args.pack_dir, args.output_dir)
    uvicorn.run(create_app(agent, port_b), host=config.HOST, port=port_b, log_level="warning")


if __name__ == "__main__":
    main()
