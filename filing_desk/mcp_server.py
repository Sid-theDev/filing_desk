"""Filing Desk as an MCP server: the same four tools over the Model Context Protocol.

Start it with:  python serve_mcp.py   (stdio transport, for Claude Desktop or any MCP client)

This layer validates, dispatches and logs. Nothing else. All behaviour lives in tools.py, so the
tests of the plain functions describe exactly what the server does. Every call is appended to
runs/mcp_calls.jsonl with its arguments, result size and duration.
"""

import json
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import config, tools

LOG_PATH = config.RUNS_DIR / "mcp_calls.jsonl"

server = MCPServer(
    name="filing-desk",
    instructions=(
        "Answer questions about Apple Inc. SEC filings (10-K, 10-Q) with citations. Start with "
        "list_filings; use get_fact for reported numbers and search_filing + read_section for text."
    ),
)


def call_and_log(name: str, arguments: dict) -> dict:
    arguments = {key: value for key, value in arguments.items() if value is not None}
    started = time.monotonic()
    result = tools.run_tool(name, arguments)
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": name,
        "arguments": arguments,
        "result_chars": len(json.dumps(result, ensure_ascii=False)),
        "ms": round((time.monotonic() - started) * 1000, 1),
        "error": result.get("error"),
    }
    config.RUNS_DIR.mkdir(exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return result


@server.tool(name="list_filings", description=tools.LIST_FILINGS_DESCRIPTION)
def list_filings(
    ticker: Annotated[str, Field(description="Company ticker. Only AAPL is available.")] = "AAPL",
    form: Annotated[Literal["10-K", "10-Q"] | None, Field(description="Annual (10-K) or quarterly (10-Q).")] = None,
    fiscal_year_from: Annotated[int | None, Field(ge=2000, le=2100)] = None,
    fiscal_year_to: Annotated[int | None, Field(ge=2000, le=2100)] = None,
) -> dict:
    return call_and_log("list_filings", {
        "ticker": ticker, "form": form, "fiscal_year_from": fiscal_year_from, "fiscal_year_to": fiscal_year_to,
    })


@server.tool(name="get_fact", description=tools.GET_FACT_DESCRIPTION)
def get_fact(
    concept: Annotated[str, Field(description="Exact us-gaap concept name, e.g. LongTermDebtNoncurrent.")],
    period: Annotated[str | None, Field(description="FY2024, Q3FY2026 or YYYY-MM-DD. Omit for recent values.")] = None,
    ticker: Annotated[str, Field(description="Company ticker. Only AAPL is available.")] = "AAPL",
) -> dict:
    return call_and_log("get_fact", {"concept": concept, "period": period, "ticker": ticker})


@server.tool(name="search_filing", description=tools.SEARCH_FILING_DESCRIPTION)
def search_filing(
    filing_id: Annotated[str, Field(description="A filing_id from list_filings, e.g. aapl-10k-fy2024.")],
    query: Annotated[str, Field(description="Words to look for, in the filing's own vocabulary.")],
    section: Annotated[str | None, Field(description="Optional: limit to one section, e.g. Item 7 or Note 9.")] = None,
) -> dict:
    return call_and_log("search_filing", {"filing_id": filing_id, "query": query, "section": section})


@server.tool(name="read_section", description=tools.READ_SECTION_DESCRIPTION)
def read_section(
    filing_id: Annotated[str, Field(description="A filing_id from list_filings, e.g. aapl-10k-fy2024.")],
    section: Annotated[str | None, Field(description="Item 7, Item 8, Note 9, Part I Item 2, ...")] = None,
    start_line: Annotated[int | None, Field(ge=1, description="Line to start reading from.")] = None,
) -> dict:
    return call_and_log("read_section", {"filing_id": filing_id, "section": section, "start_line": start_line})


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
