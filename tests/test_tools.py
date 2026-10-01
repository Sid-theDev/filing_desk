"""Offline tests for the four tools. They read only the cached files under data/."""

import re

import pytest

from filing_desk import tools
from filing_desk.config import MAX_READ_CHARS, READING_DIR
from filing_desk.filings import load_filings

FILING_IDS = ["aapl-10k-fy2022", "aapl-10k-fy2023", "aapl-10k-fy2024", "aapl-10q-q3fy2026"]
BALANCE_SHEET_TERM_DEBT = re.compile(r"^Term debt\s+([\d,]+)\s+([\d,]+)$")


# ----------------------------------------------------------------------------- documents
def test_all_filings_load():
    assert sorted(load_filings()) == FILING_IDS


@pytest.mark.parametrize("filing_id", FILING_IDS)
def test_body_line_count_matches_header(filing_id):
    header = (READING_DIR / f"{filing_id}.txt").read_text(encoding="utf-8")
    expected = int(re.search(r"Body lines:\s+([\d,]+)", header).group(1).replace(",", ""))
    assert len(load_filings()[filing_id].lines) == expected


@pytest.mark.parametrize("filing_id", FILING_IDS)
def test_line_numbers_agree_with_markers(filing_id):
    """Line N in tool results is the line printed just above `----- line N -----` in the reading file."""
    raw = (READING_DIR / f"{filing_id}.txt").read_text(encoding="utf-8").split("\n")
    lines = load_filings()[filing_id].lines
    markers = 0
    for index, text in enumerate(raw):
        if match := re.match(r"^----- line (\d+) -----$", text):
            assert raw[index - 2] == lines[int(match.group(1)) - 1]
            markers += 1
    assert markers > 5


@pytest.mark.parametrize("filing_id, expected", [
    ("aapl-10k-fy2022", ["Item 7", "Item 8"]),
    ("aapl-10k-fy2023", ["Item 7", "Item 8"]),
    ("aapl-10k-fy2024", ["Item 1A", "Item 7", "Item 8", "Note 9"]),
    ("aapl-10q-q3fy2026", ["Part I Item 1", "Part I Item 2", "Part II Item 1A", "Note 6"]),
])
def test_section_map_finds_key_sections(filing_id, expected):
    names = [section.name for section in load_filings()[filing_id].sections]
    for name in expected:
        assert name in names


def test_sections_skip_table_of_contents():
    item_7 = next(s for s in load_filings()["aapl-10k-fy2024"].sections if s.name == "Item 7")
    assert item_7.title.startswith("Management")
    assert item_7.end_line - item_7.start_line > 100  # the real section, not a one-line TOC row


# ----------------------------------------------------------------------------- Phase 1 reconciliation
@pytest.mark.parametrize("filing_id, period", [
    ("aapl-10k-fy2022", "FY2022"),
    ("aapl-10k-fy2023", "FY2023"),
    ("aapl-10k-fy2024", "FY2024"),
    ("aapl-10q-q3fy2026", "Q3FY2026"),
])
def test_balance_sheet_term_debt_matches_xbrl(filing_id, period):
    """The number read off the balance sheet (in millions) equals the XBRL fact for the same date."""
    rows = [m for line in load_filings()[filing_id].lines if (m := BALANCE_SHEET_TERM_DEBT.match(line.strip()))]
    current_millions, noncurrent_millions = (int(row.group(1).replace(",", "")) for row in rows[:2])

    current = tools.get_fact("LongTermDebtCurrent", period)["facts"][0]
    noncurrent = tools.get_fact("LongTermDebtNoncurrent", period)["facts"][0]

    assert current["unit"] == noncurrent["unit"] == "USD"
    assert current_millions * 1_000_000 == current["value"]
    assert noncurrent_millions * 1_000_000 == noncurrent["value"]


# ----------------------------------------------------------------------------- list_filings
def test_list_filings_filters_by_form_and_year():
    result = tools.list_filings(form="10-K", fiscal_year_from=2023, fiscal_year_to=2024)
    assert [f["filing_id"] for f in result["filings"]] == ["aapl-10k-fy2024", "aapl-10k-fy2023"]
    assert result["truncated"] is False


def test_other_company_is_refused():
    result = tools.run_tool("list_filings", {"ticker": "MSFT"})
    assert "not in this universe" in result["error"]


# ----------------------------------------------------------------------------- get_fact
def test_get_fact_returns_value_unit_period_and_cite():
    fact = tools.get_fact("us-gaap:LongTermDebtNoncurrent", "FY2024")["facts"][0]
    assert fact["unit"] == "USD"
    assert fact["period"] == "2024-09-28"
    assert fact["fiscal_period"] == "FY2024"
    assert fact["reported_in"]["filing_id"] == "aapl-10k-fy2024"
    assert fact["cite"] == "[xbrl LongTermDebtNoncurrent 2024-09-28]"


def test_get_fact_prefers_original_filing_and_discloses_other_values():
    fact = tools.get_fact("LongTermDebt", "FY2024")["facts"][0]
    assert fact["reported_in"]["form"] == "10-K"
    assert fact["other_values"], "later 10-Qs reported this period with a different (rounded) value"


def test_get_fact_duration_concept_has_start_and_end():
    facts = tools.get_fact("RepaymentsOfLongTermDebt", "FY2024")["facts"]
    assert all(f["period_type"] == "duration" and ".." in f["period"] for f in facts)


def test_unknown_concept_returns_candidates():
    result = tools.run_tool("get_fact", {"concept": "long-term debt"})
    assert "error" in result
    assert "LongTermDebt" in [c["concept"] for c in result["candidates"]]


def test_unknown_period_explains_known_range():
    result = tools.run_tool("get_fact", {"concept": "LongTermDebt", "period": "FY2031"})
    assert "FY2031" in result["error"]
    assert "Known periods run from" in result["hint"]


def test_get_fact_without_period_is_capped():
    result = tools.get_fact("CommercialPaper")
    assert 0 < len(result["facts"]) <= 12


# ----------------------------------------------------------------------------- search_filing
def test_search_finds_debt_note():
    result = tools.search_filing("aapl-10k-fy2024", "commercial paper general corporate purposes")
    assert result["hits"]
    assert "Note 9" in result["hits"][0]["section"]
    assert all(len(hit["snippet"]) <= 300 for hit in result["hits"])


def test_search_within_section_stays_inside_it():
    item_7 = next(s for s in load_filings()["aapl-10k-fy2024"].sections if s.name == "Item 7")
    result = tools.search_filing("aapl-10k-fy2024", "commercial paper", section="Item 7")
    assert all(item_7.start_line <= hit["line"] <= item_7.end_line for hit in result["hits"])


def test_search_with_no_hits_gives_hint():
    result = tools.search_filing("aapl-10k-fy2024", "zebra unicorn")
    assert result["hits"] == []
    assert "Item 7" in result["hint"]


# ----------------------------------------------------------------------------- read_section
def test_read_section_without_arguments_lists_sections():
    result = tools.read_section("aapl-10k-fy2024")
    assert any(s["section"] == "Item 8" for s in result["sections"])


def test_read_section_truncates_and_continues():
    first = tools.read_section("aapl-10k-fy2024", section="Item 8")
    assert first["truncated"] is True
    assert len(first["text"]) <= MAX_READ_CHARS

    second = tools.read_section("aapl-10k-fy2024", section="Item 8", start_line=first["next_start_line"])
    assert second["start_line"] == first["end_line"] + 1


def test_read_section_text_carries_line_numbers():
    result = tools.read_section("aapl-10k-fy2024", start_line=100)
    assert result["text"].startswith("100|")
    assert result["cite"] == f"[aapl-10k-fy2024 L100-{result['end_line']}]"


def test_unknown_section_lists_available():
    result = tools.run_tool("read_section", {"filing_id": "aapl-10k-fy2024", "section": "Item 99"})
    assert "Item 7 – " in " ".join(result["available"])


def test_ambiguous_10q_item_asks_for_part():
    result = tools.run_tool("read_section", {"filing_id": "aapl-10q-q3fy2026", "section": "Item 2"})
    assert "ambiguous" in result["error"]
    ok = tools.run_tool("read_section", {"filing_id": "aapl-10q-q3fy2026", "section": "Part I Item 2"})
    assert "error" not in ok


# ----------------------------------------------------------------------------- run_tool
def test_run_tool_rejects_unknown_arguments():
    result = tools.run_tool("get_fact", {"concept": "LongTermDebt", "year": 2024})
    assert "Unknown argument" in result["error"]


def test_run_tool_reports_missing_arguments():
    result = tools.run_tool("search_filing", {"filing_id": "aapl-10k-fy2024"})
    assert "Missing required" in result["error"]


def test_run_tool_accepts_digit_strings_for_integers():
    result = tools.run_tool("read_section", {"filing_id": "aapl-10k-fy2024", "start_line": "50"})
    assert result["start_line"] == 50
