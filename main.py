"""One command to close the quarter: python main.py [pack_dir]

This launcher holds no business logic. It checks the setup, starts both agents as separate
processes, waits for their agent cards, triggers Agent A, prints the result, and stops both.
Exit code 0 means every step succeeded and the NAV bridge balanced.
"""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

import config
from agents.a2a import CARD_PATH, A2AClient, A2AError, data_part, new_message, task_text

ROOT = Path(__file__).resolve().parent
OUTPUT_FILES = ("transactions.json", "a2a_log.jsonl", "marks.json", "review.json", "nav.json")
STARTUP_TIMEOUT_SECONDS = 30
STOP_TIMEOUT_SECONDS = 10
CLOSE_TIMEOUT_SECONDS = 1800  # Allows for rate-limit waits on a free LLM tier.


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex((config.HOST, port)) == 0


def wait_for_card(base_url: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Agent at {base_url} exited during startup (code {process.returncode})")
        try:
            if httpx.get(f"{base_url}{CARD_PATH}", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Agent at {base_url} did not start within {STARTUP_TIMEOUT_SECONDS}s")


def start_agent(module: str, pack_dir: Path, output_dir: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", module, "--pack-dir", str(pack_dir), "--output-dir", str(output_dir)], cwd=ROOT
    )


def stop_agents(processes: list[subprocess.Popen]) -> None:
    """Ask each agent to stop, and force it if it has not exited after a few seconds."""
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def print_summary(output_dir: Path, status_text: str) -> None:
    nav = json.loads((output_dir / "nav.json").read_text(encoding="utf-8"))
    review = json.loads((output_dir / "review.json").read_text(encoding="utf-8"))
    count = len(json.loads((output_dir / "transactions.json").read_text(encoding="utf-8")))
    bridge = nav["bridge"]
    print(f"\n{status_text}\n")
    for label, value in [
        ("Closing cash", nav["closing_cash"]), ("Capital called", bridge["capital_called"]),
        ("Distributions paid", bridge["distributions"]), ("Management fee", nav["management_fee"]),
        ("Income", bridge["income"]), ("Total expenses", bridge["expenses"]),
        ("Realized gain", bridge["realized_gain"]), ("Change in unrealized gain", bridge["change_in_unrealized"]),
        ("Investments at fair value", nav["investments_usd"]), ("Liabilities", nav["liabilities"]),
        ("NAV at 31 Dec 2022", nav["nav_q4"]), ("Bridge difference", bridge["difference"]),
    ]:
        print(f"{label:<28}{value:>20.2f}")
    print(f"{'EUR/USD rate':<28}{review['eurusd_rate']:>20}")
    print(f"{'Materiality threshold %':<28}{review['threshold_pct']:>20}")
    print(f"{'Transactions':<28}{count:>20}")
    print("Flagged: " + ", ".join(review["flagged_companies"]))
    failed = [name for name, passed in nav["checks"].items() if not passed]
    print("Checks: " + (f"FAILED {failed}" if failed else "all passed"))


def run(pack_dir: Path, output_dir: Path) -> int:
    config.load_llm_settings()
    config.check_pack(pack_dir)
    port_a, port_b = config.agent_ports()
    for port in (port_a, port_b):
        if port_in_use(port):
            raise config.ConfigError(f"Port {port} is already in use. Set AGENT_A_PORT and AGENT_B_PORT, or free it.")

    # Clear earlier results so a failed run can never leave stale files looking current.
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_FILES:
        (output_dir / name).unlink(missing_ok=True)

    base_a = f"http://{config.HOST}:{port_a}"
    base_b = f"http://{config.HOST}:{port_b}"
    processes = []
    try:
        processes.append(start_agent("agents.valuation_agent", pack_dir, output_dir))
        wait_for_card(base_b, processes[-1])
        processes.append(start_agent("agents.bank_agent", pack_dir, output_dir))
        wait_for_card(base_a, processes[-1])

        # This trigger comes from the launcher, so it is not part of the agent-to-agent log.
        client = A2AClient("launcher", "agent-a", base_a, timeout=CLOSE_TIMEOUT_SECONDS)
        task = client.send(client.discover()["url"], new_message(
            [data_part({"fund": config.FUND_CODE, "valuation_date": config.VALUATION_DATE})]))

        if task["status"]["state"] != "completed":
            print(f"\nRun failed: {task_text(task)}", file=sys.stderr)
            return 1
        print_summary(output_dir, task_text(task))
        return 0
    finally:
        stop_agents(processes)


def main() -> int:
    pack_dir = Path(sys.argv[1] if len(sys.argv) > 1 else config.default_pack_dir()).resolve()
    output_dir = Path("output").resolve()
    try:
        return run(pack_dir, output_dir)
    except (config.ConfigError, A2AError, RuntimeError, httpx.HTTPError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
