"""The MCP server, driven by a real MCP client over stdio. Starts a subprocess, so a little slower."""

import pytest

from filing_desk import tools
from filing_desk.mcp_client import MCPTools


@pytest.fixture(scope="module")
def server():
    with MCPTools() as running:
        yield running


def test_server_lists_the_four_tools(server):
    assert server.list_tool_names() == ["get_fact", "list_filings", "read_section", "search_filing"]


def test_results_over_mcp_match_the_plain_functions(server):
    over_mcp = server.run_tool("get_fact", {"concept": "LongTermDebtNoncurrent", "period": "FY2024"})
    assert over_mcp == tools.get_fact("LongTermDebtNoncurrent", "FY2024")


def test_search_over_mcp_matches_the_plain_function(server):
    arguments = {"filing_id": "aapl-10k-fy2024", "query": "commercial paper", "section": "Note 9"}
    assert server.run_tool("search_filing", arguments) == tools.search_filing(**arguments)


def test_errors_over_mcp_are_error_dicts(server):
    result = server.run_tool("read_section", {"filing_id": "aapl-10k-fy2099"})
    assert "Unknown filing_id" in result["error"]
