"""Central configuration: environment variables, file names and the judgment-call constants.

Everything an evaluator may change comes from the environment. A local `.env` is
loaded for convenience, but real environment variables always win over it.
"""

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

FUND_CODE = "GPF"
VALUATION_DATE = "2022-12-31"

BANK_STATEMENT_FILE = "GPF_Bank_Statement_Q4_2022.pdf"
Q3_VALUATION_FILE = "CVP_Valuation_Report_Q3_2022.xlsx"
Q3_NAV_FILE = "GPF_NAV_Summary_Q3_2022.xlsx"
AGENT_B_DATA_FILE = "agent_b_data.json"
PACK_FILES = (BANK_STATEMENT_FILE, Q3_VALUATION_FILE, Q3_NAV_FILE, AGENT_B_DATA_FILE)

# Judgment call: see ASSUMPTIONS.md, Assumption 2.
MATERIALITY_THRESHOLD_PCT = Decimal("15")

# Judgment call: see ASSUMPTIONS.md, Assumption 1. Pinned rather than fetched so a run
# is reproducible and needs no network access beyond the LLM.
EURUSD_RATE = Decimal("1.0666")
EURUSD_RATE_DATE = "2022-12-30"
EURUSD_SOURCE = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
    "?startPeriod=2022-12-28&endPeriod=2023-01-03&format=csvdata"
)

HOST = "127.0.0.1"
DEFAULT_AGENT_A_PORT = 8001
DEFAULT_AGENT_B_PORT = 8002


class ConfigError(Exception):
    """A required setting or input file is missing."""


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    model: str


def load_llm_settings() -> LLMSettings:
    """Read the three LLM variables, naming any that are missing."""
    values = {name: os.environ.get(name, "").strip() for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")}
    # The placeholder from .env.example counts as missing.
    missing = [k for k, v in values.items() if not v or v == "your_llm_api_key_here"]
    if missing:
        raise ConfigError(
            f"Missing LLM settings: {', '.join(missing)}. "
            "Set them in the environment or copy .env.example to .env and fill them in."
        )
    return LLMSettings(values["LLM_API_KEY"], values["LLM_BASE_URL"].rstrip("/"), values["LLM_MODEL"])


def agent_ports() -> tuple[int, int]:
    """Return (Agent A port, Agent B port) from the environment, or the defaults."""
    return (
        int(os.environ.get("AGENT_A_PORT") or DEFAULT_AGENT_A_PORT),
        int(os.environ.get("AGENT_B_PORT") or DEFAULT_AGENT_B_PORT),
    )


def default_pack_dir() -> str:
    return os.environ.get("PACK_DIR") or "./pack"


def check_pack(pack_dir: Path) -> None:
    """Fail early with a clear message if an input file is absent."""
    missing = [name for name in PACK_FILES if not (pack_dir / name).is_file()]
    if missing:
        raise ConfigError(f"Missing from pack directory {pack_dir}: {', '.join(missing)}")
