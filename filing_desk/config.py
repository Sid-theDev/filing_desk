"""Paths, limits and settings used across Filing Desk. Change numbers here, nowhere else."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
READING_DIR = ROOT / "data" / "reading"      # plain-text filings (derived from data/raw)
COMPANY_DIR = ROOT / "data" / "raw" / "aapl"  # cached EDGAR JSON for Apple
RUNS_DIR = ROOT / "runs"                      # one trace file per agent run

TICKER = "AAPL"

# --- tool return budgets --------------------------------------------------
MAX_FILINGS = 20          # rows from list_filings
MAX_FACT_ROWS = 12        # rows from get_fact when no period is given
MAX_CANDIDATES = 8        # concept suggestions when a name is not found
MAX_SEARCH_HITS = 8       # hits from search_filing
SNIPPET_CHARS = 300       # characters per search hit
MAX_READ_CHARS = 6000     # characters of text per read_section call

# --- agent limits -----------------------------------------------------------
MAX_STEPS = 15                 # model calls before a forced stop
MAX_TOTAL_TOKENS = 400_000     # prompt + completion tokens across the run
MAX_SECONDS = 300              # wall-clock time for the run
COMPACT_AFTER_CHARS = 60_000   # context size that triggers compaction (only read text the model has seen)

# --- model ------------------------------------------------------------------
DEFAULT_MODEL = "openai/gpt-5.6-luna"  # chosen for answer quality and tone (see DECISIONS.md #14)
BAKE_OFF_MODELS = [                    # compared with: python evaluate.py --models ...
    "qwen/qwen3.7-flash",
    "openai/gpt-5-nano",
    "inclusionai/ling-3.0-flash",
    "deepseek/deepseek-v4-flash-0731",
    "openai/gpt-5.6-luna",
]
TEMPERATURE = 0.0
MAX_OUTPUT_TOKENS = 4000


def load_env() -> None:
    """Copy KEY=value lines from .env into os.environ. Variables already set in the shell win."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip())
