"""The A2A protocol over real HTTP handling (FastAPI's in-process client), including both agents together."""

import json

import pytest
from fastapi.testclient import TestClient

from agents.a2a import CARD_PATH, RPC_PATH, A2AClient, data_part, new_message, task_text
from agents.bank_agent import BankAgent, derive_purchases_and_sales
from agents.valuation_agent import ValuationAgent, create_app
from tests.conftest import FakeLLM

REQUEST = {"fund": "GPF", "valuation_date": "2022-12-31"}
CORRECT = {
    "purchases": [{"company": "IONMERE MATERIALS LTD", "date": "2022-12-15", "amount_usd": 14000000.0}],
    "sales": [{"company": "Fjord Payments AS", "date": "2022-11-18", "proceeds_usd": 29000000.0}],
}


@pytest.fixture
def agent_b(pack_dir, tmp_path) -> ValuationAgent:
    return ValuationAgent(pack_dir, tmp_path)


@pytest.fixture
def http(agent_b) -> TestClient:
    return TestClient(create_app(agent_b, 8002))


def rpc(http, message):
    body = {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {"message": message}}
    return http.post(RPC_PATH, json=body).json()


def ask(http) -> dict:
    return rpc(http, new_message([data_part(REQUEST)]))["result"]


def answer(http, task, lists) -> dict:
    return rpc(http, new_message([data_part(lists)], task_id=task["id"], context_id=task["contextId"]))


def test_agent_card_advertises_the_endpoint(http):
    card = http.get(CARD_PATH).json()
    assert card["protocolVersion"].startswith("0.3")
    assert card["url"].endswith(":8002/a2a")
    assert card["skills"]


def test_first_turn_asks_for_purchases_and_sales(http):
    task = ask(http)
    assert task["kind"] == "task"
    assert task["status"]["state"] == "input-required"
    assert "purchases and sales" in task_text(task)
    assert task["id"] and task["contextId"] and not task["artifacts"]


def test_matching_lists_release_the_marks_on_the_same_task(http, agent_b_data):
    task = ask(http)
    done = answer(http, task, CORRECT)["result"]
    assert done["status"]["state"] == "completed"
    assert (done["id"], done["contextId"]) == (task["id"], task["contextId"])
    artifact = done["artifacts"][0]
    assert artifact["artifactId"] == "marks-GPF-2022-12-31"
    data = artifact["parts"][0]["data"]
    assert data["fund"] == "The Greatness PE Fund, L.P."
    assert data["marks"] == agent_b_data["marks"]
    assert data["notes"] == agent_b_data["notes"]


def test_marks_are_not_visible_before_verification(http):
    assert "fair_value" not in json.dumps(ask(http))


@pytest.mark.parametrize("change,matched", [
    ({"sales": []}, "1 of 2"),
    ({"purchases": [{"company": "Ionmere Materials Ltd", "date": "2022-12-14", "amount_usd": 14000000.0}]}, "1 of 2"),
    ({"sales": [{"company": "Nordic Acquisitions AS", "date": "2022-11-18", "proceeds_usd": 29000000.0}]}, "1 of 2"),
    ({"purchases": [], "sales": []}, "0 of 2"),
])
def test_mismatch_stays_open_and_reports_the_match_count(http, change, matched):
    task = ask(http)
    reply = answer(http, task, {**CORRECT, **change})["result"]
    assert reply["status"]["state"] == "input-required"
    assert reply["id"] == task["id"] and reply["contextId"] == task["contextId"]
    assert matched in task_text(reply)
    assert not reply["artifacts"]


def test_extra_item_is_a_mismatch(http):
    task = ask(http)
    lists = {**CORRECT, "purchases": CORRECT["purchases"] * 2}
    assert answer(http, task, lists)["result"]["status"]["state"] == "input-required"


def test_mismatch_can_be_corrected_on_the_same_task(http):
    task = ask(http)
    answer(http, task, {"purchases": [], "sales": []})
    assert answer(http, task, CORRECT)["result"]["status"]["state"] == "completed"


def test_mismatch_does_not_reveal_agent_b_records(http):
    reply = answer(http, ask(http), {"purchases": [], "sales": []})["result"]
    assert "ionmere" not in task_text(reply).lower()


def test_malformed_lists_are_asked_for_again(http):
    task = ask(http)
    assert answer(http, task, {"purchases": [{"company": "x"}], "sales": []})["result"]["status"]["state"] == "input-required"
    assert answer(http, task, {"purchases": []})["result"]["status"]["state"] == "input-required"


def test_completed_task_cannot_be_reused(http):
    task = ask(http)
    answer(http, task, CORRECT)
    assert answer(http, task, CORRECT)["error"]["code"] == -32001


def test_unknown_task_and_wrong_context_are_errors(http):
    assert answer(http, {"id": "nope", "contextId": "x"}, CORRECT)["error"]["code"] == -32001
    task = ask(http)
    assert answer(http, {"id": task["id"], "contextId": "other"}, CORRECT)["error"]["code"] == -32602


def test_wrong_fund_is_rejected(http):
    reply = rpc(http, new_message([data_part({"fund": "XYZ", "valuation_date": "2022-12-31"})]))
    assert reply["result"]["status"]["state"] == "rejected"


def test_jsonrpc_errors(http):
    assert http.post(RPC_PATH, content="{bad").json()["error"]["code"] == -32700
    assert http.post(RPC_PATH, json={"id": 1}).json()["error"]["code"] == -32600
    body = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {}}
    assert http.post(RPC_PATH, json=body).json()["error"]["code"] == -32601
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}
    assert http.post(RPC_PATH, json=body).json()["error"]["code"] == -32602


def test_purchases_and_sales_come_from_categorized_rows(ledger):
    lists = derive_purchases_and_sales(ledger)
    assert lists["purchases"] == [{"company": "Ionmere Materials Ltd", "date": "2022-12-15", "amount_usd": 14000000.0}]
    assert lists["sales"] == [{"company": "Fjord Payments AS", "date": "2022-11-18", "proceeds_usd": 29000000.0}]


def close_with(llm, pack_dir, tmp_path):
    """Run both agents together: A's client posts to B's HTTP app, and files go to tmp_path."""
    b_app = create_app(ValuationAgent(pack_dir, tmp_path), 8002)
    peer = A2AClient("agent-a", "agent-b", "http://testserver", tmp_path / "a2a_log.jsonl", client=TestClient(b_app))
    return BankAgent(pack_dir, tmp_path, llm, peer).on_message(new_message([data_part(REQUEST)]))


def log_lines(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "a2a_log.jsonl").read_text().splitlines()]


def test_full_close_writes_every_file_and_logs_two_turns(pack_dir, tmp_path, agent_b_data):
    task = close_with(FakeLLM(), pack_dir, tmp_path)
    assert task["status"]["state"] == "completed", task_text(task)
    for name in ("transactions.json", "a2a_log.jsonl", "marks.json", "review.json", "nav.json"):
        assert (tmp_path / name).exists()

    lines = log_lines(tmp_path)
    assert [line["direction"] for line in lines] == ["request", "response", "request", "response"]
    assert all(line["timestamp"] for line in lines)
    first_reply = lines[1]["payload"]["result"]
    last_reply = lines[3]["payload"]["result"]
    second_message = lines[2]["payload"]["params"]["message"]
    assert first_reply["status"]["state"] == "input-required"
    assert last_reply["status"]["state"] == "completed"
    assert second_message["taskId"] == first_reply["id"] == last_reply["id"]
    assert second_message["contextId"] == first_reply["contextId"] == last_reply["contextId"]

    marks = json.loads((tmp_path / "marks.json").read_text())
    assert marks == last_reply["artifacts"][0]["parts"][0]["data"]
    assert marks["marks"] == agent_b_data["marks"]


def shorten_purchase_name(answers, lines):
    """A name that appears in the description, so only B's records can show it is wrong."""
    for item, line in zip(answers, lines):
        if "IONMERE" in line["description"]:
            item["company"] = "IONMERE MATERIALS"
    return answers


def test_agent_a_recovers_when_b_rejects_a_wrong_company(pack_dir, tmp_path):
    def wrong_until_told(answers, lines, prompt):
        return answers if "rejected" in prompt else shorten_purchase_name(answers, lines)

    task = close_with(FakeLLM(tamper=wrong_until_told), pack_dir, tmp_path)
    assert task["status"]["state"] == "completed", task_text(task)
    states = [line["payload"]["result"]["status"]["state"] for line in log_lines(tmp_path) if line["direction"] == "response"]
    assert states == ["input-required", "input-required", "completed"]


def test_agent_a_fails_clearly_when_it_cannot_satisfy_b(pack_dir, tmp_path):
    def always_wrong(answers, lines, prompt):
        return shorten_purchase_name(answers, lines)

    task = close_with(FakeLLM(tamper=always_wrong), pack_dir, tmp_path)
    assert task["status"]["state"] == "failed"
    assert "Could not satisfy Agent B" in task_text(task)
    assert not (tmp_path / "marks.json").exists()


def test_unreachable_peer_fails_the_close(pack_dir, tmp_path):
    peer = A2AClient("agent-a", "agent-b", "http://127.0.0.1:9", tmp_path / "a2a_log.jsonl", timeout=1)
    task = BankAgent(pack_dir, tmp_path, FakeLLM(), peer).on_message(new_message([data_part(REQUEST)]))
    assert task["status"]["state"] == "failed"
