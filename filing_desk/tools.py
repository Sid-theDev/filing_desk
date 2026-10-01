"""The four Filing Desk tools, as plain functions.

    list_filings   which filings exist
    get_fact       a reported number, straight from XBRL
    search_filing  where words appear in one filing (locations + snippets, never whole sections)
    read_section   bounded text with line numbers, with a cursor to continue

Every result is a small dict carrying a `cite` string the model can copy into its answer.
Tools raise ToolError on bad input; run_tool() turns any failure into an error dict.
"""

import math
import re

from . import facts
from .config import MAX_FACT_ROWS, MAX_FILINGS, MAX_READ_CHARS, MAX_SEARCH_HITS, SNIPPET_CHARS, TICKER
from .errors import ToolError
from .filings import filing_id_for_accession, find_section, get_filing, load_filings, section_label

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "does", "for", "from", "how", "in", "is",
    "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were", "what", "with",
    "apple", "apple's", "apple’s", "company", "company's", "company’s",
}


def check_ticker(ticker: str | None) -> None:
    if (ticker or TICKER).strip().upper() != TICKER:
        raise ToolError(
            f"'{ticker}' is not in this universe. Only {TICKER} (Apple Inc.) filings are available.",
            hint="If the question is about another company, answer NOT FOUND.",
        )


def text_cite(filing_id: str, start: int, end: int | None = None) -> str:
    if end is None or end == start:
        return f"[{filing_id} L{start}]"
    return f"[{filing_id} L{start}-{end}]"


# ----------------------------------------------------------------------------- list_filings
def list_filings(ticker: str = TICKER, form: str | None = None,
                 fiscal_year_from: int | None = None, fiscal_year_to: int | None = None) -> dict:
    check_ticker(ticker)
    rows = []
    for filing in sorted(load_filings().values(), key=lambda f: f.period_end, reverse=True):
        if form and filing.form != form:
            continue
        if fiscal_year_from and filing.fiscal_year < fiscal_year_from:
            continue
        if fiscal_year_to and filing.fiscal_year > fiscal_year_to:
            continue
        rows.append(filing.summary())

    result = {
        "filings": rows[:MAX_FILINGS],
        "count": min(len(rows), MAX_FILINGS),
        "truncated": len(rows) > MAX_FILINGS,
        "note": "Only filings whose text is available are listed. get_fact covers every period Apple reported in XBRL.",
    }
    if not rows:
        result["hint"] = "No filing matches. Available: " + ", ".join(sorted(load_filings()))
    return result


# ----------------------------------------------------------------------------- get_fact
def get_fact(concept: str, period: str | None = None, ticker: str = TICKER) -> dict:
    check_ticker(ticker)
    name, entry = facts.find_concept(concept)
    rows = facts.fact_rows(entry)

    if period:
        label, period_end = facts.resolve_period(period)
        chosen = [row for row in rows if row["end"] == period_end]
        if not chosen:
            recent = ", ".join(sorted({row["end"] for row in rows})[-6:])
            raise ToolError(
                f"Apple reported no {name} value for the period ending {period_end} ({label}).",
                hint=f"Most recent period ends with a value: {recent}. Call get_fact without a period to see them.",
            )
    else:
        chosen = rows[-MAX_FACT_ROWS:]

    return {
        "concept": f"us-gaap:{name}",
        "label": entry.get("label"),
        "facts": [format_fact(name, row) for row in chosen],
        "truncated": not period and len(rows) > MAX_FACT_ROWS,
        "note": (
            "Values are exactly as reported, in the unit shown (not rescaled). When several filings "
            "reported the same period, the value is from the earliest one; other_values lists any "
            "different value a later filing gave."
        ),
    }


def format_fact(name: str, row: dict) -> dict:
    period = f"{row['start']}..{row['end']}" if row["start"] else row["end"]
    fact = {
        "value": row["value"],
        "unit": row["unit"],
        "period_type": row["period_type"],
        "period": period,
        "fiscal_period": facts.label_for_date(row["end"]),
        "reported_in": {
            "form": row["form"],
            "filed": row["filed"],
            "accession": row["accession"],
            "filing_id": filing_id_for_accession(row["accession"]),
        },
        "times_reported": row["times_reported"],
        "cite": f"[xbrl {name} {period}]",
    }
    if row["other_values"]:
        fact["other_values"] = row["other_values"]
    return fact


# ----------------------------------------------------------------------------- search_filing
def search_filing(filing_id: str, query: str, section: str | None = None) -> dict:
    filing = get_filing(filing_id)
    words = [w for w in re.findall(r"[a-z0-9]+(?:['’-][a-z0-9]+)*", (query or "").lower())
             if w not in STOPWORDS and len(w) > 1]
    if not words:
        raise ToolError("The query has no searchable words.",
                        hint="Use distinctive words from the filing, e.g. 'term debt' or 'commercial paper'.")

    if section:
        target = find_section(filing, section)
        first, last = target.start_line, target.end_line
    else:
        first, last = 1, len(filing.lines)

    lowered = [line.lower() for line in filing.lines]
    phrase = " ".join(words)
    needed = math.ceil(len(words) / 2)
    # A word matches at the start of a word: "repay" finds "repayments" but not "prepayments".
    patterns = [re.compile(r"(?<![a-z0-9])" + re.escape(word)) for word in words]

    # Score every 3-line window that starts on a matching line.
    scored = []
    for number in range(first, last + 1):
        if not any(pattern.search(lowered[number - 1]) for pattern in patterns):
            continue
        window = re.sub(r"\s+", " ", " ".join(lowered[number - 1:min(number + 2, last)]))
        found = sum(1 for pattern in patterns if pattern.search(window))
        if found < needed:
            continue
        bonus = 3 if len(words) > 1 and phrase in window else 0
        scored.append((found + bonus, number))

    scored.sort(key=lambda item: (-item[0], item[1]))
    hit_lines = []
    for _, number in scored:
        if all(abs(number - other) >= 3 for other in hit_lines):
            hit_lines.append(number)

    hits = []
    for number in hit_lines[:MAX_SEARCH_HITS]:
        end = min(number + 2, last)
        snippet = " / ".join(re.sub(r"\s+", " ", line).strip() for line in filing.lines[number - 1:end] if line.strip())
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS - 1] + "…"
        hits.append({
            "line": number,
            "section": section_label(filing, number),
            "snippet": snippet,
            "cite": text_cite(filing.filing_id, number, end),
        })

    result = {
        "filing_id": filing.filing_id,
        "query_words": words,
        "searched": section or "whole filing",
        "hits": hits,
        "total_hits": len(hit_lines),
        "truncated": len(hit_lines) > MAX_SEARCH_HITS,
        "limits": {"max_hits": MAX_SEARCH_HITS, "snippet_chars": SNIPPET_CHARS},
    }
    if hits:
        result["next_step"] = "Call read_section with start_line near a hit to read the surrounding text."
    else:
        if filing.form == "10-K":
            where = "figures and notes are in Item 8; management's explanations are in Item 7 (MD&A)"
        else:
            where = "figures and notes are in Part I Item 1; management's explanations are in Part I Item 2 (MD&A)"
        result["hint"] = (
            "No line matched. Filings often word things differently from the question "
            f"(e.g. 'term debt' rather than 'long-term debt'). Try other words; {where}."
        )
    return result


# ----------------------------------------------------------------------------- read_section
def read_section(filing_id: str, section: str | None = None, start_line: int | None = None) -> dict:
    filing = get_filing(filing_id)

    if not section and start_line is None:
        return {
            "filing_id": filing.filing_id,
            "sections": [s.to_dict() for s in filing.sections],
            "hint": "Call read_section again with section (e.g. 'Item 7', 'Note 9', 'Part I Item 2') or start_line.",
        }

    total = len(filing.lines)
    if section:
        target = find_section(filing, section)
        first, last = target.start_line, target.end_line
        label = f"{target.name} – {target.title}"
        if start_line is not None:
            if not first <= start_line <= last:
                raise ToolError(f"start_line {start_line} is outside {target.name} (lines {first}-{last}).",
                                hint="Leave section out to read from any line.")
            first = start_line
    else:
        if not 1 <= start_line <= total:
            raise ToolError(f"start_line {start_line} is out of range.", hint=f"Valid lines are 1-{total}.")
        first, last = start_line, total
        label = section_label(filing, start_line)

    text_lines = []
    used = 0
    number = first
    while number <= last:
        line = re.sub(r" {3,}", "  ", filing.lines[number - 1].rstrip())
        entry = f"{number}| {line}" if line else f"{number}|"
        if text_lines and used + len(entry) + 1 > MAX_READ_CHARS:
            break
        text_lines.append(entry)
        used += len(entry) + 1
        number += 1
    end = number - 1

    result = {
        "filing_id": filing.filing_id,
        "section": label,
        "start_line": first,
        "end_line": end,
        "text": "\n".join(text_lines),
        "truncated": end < last,
        "cite": text_cite(filing.filing_id, first, end),
    }
    if end < last:
        result["next_start_line"] = end + 1
        result["hint"] = (
            f"Stopped at the {MAX_READ_CHARS}-character limit. Call read_section again with "
            f"start_line={end + 1}{' and the same section' if section else ''} to continue."
        )
    return result


# ----------------------------------------------------------------------------- descriptions + schemas
LIST_FILINGS_DESCRIPTION = """List the SEC filings whose full text is available, newest first. Cheap: ids and dates only (about 1 KB).
Call it first to get the filing_id that search_filing and read_section need. Only Apple Inc. (AAPL) is in this universe.
Example: list_filings(form="10-K", fiscal_year_from=2022, fiscal_year_to=2024)"""

GET_FACT_DESCRIPTION = """Get a figure exactly as Apple reported it in XBRL (structured financial data). Prefer this for any reported number: it cannot misread a table.
Use search_filing / read_section instead when you need wording, management's explanations, or a figure that is not tagged.
concept: an exact us-gaap concept name, e.g. "LongTermDebtNoncurrent". A wrong name returns candidate names with labels; pick one and call again.
period: "FY2024", "Q3FY2026" or a period end date "2024-09-28". Leave it out for a short time series of the most recent values.
Returns value, unit, period and the filing that reported it, plus a cite string. Small (1-4 KB).
Example: get_fact(concept="LongTermDebtNoncurrent", period="FY2024")"""

SEARCH_FILING_DESCRIPTION = f"""Find where words appear in one filing. Returns up to {MAX_SEARCH_HITS} line locations, each with a short snippet ({SNIPPET_CHARS} chars) and a cite string. It never returns whole sections.
Use it to locate a passage, then read_section(start_line=...) to read around it. Matching is by words, so use the filing's own vocabulary ("term debt", "commercial paper", "repayments"), not the question's.
Optional section narrows the search, e.g. "Item 7" or "Note 9".
Example: search_filing(filing_id="aapl-10k-fy2024", query="term debt repayments", section="Item 7")"""

READ_SECTION_DESCRIPTION = f"""Read filing text with line numbers, at most {MAX_READ_CHARS} characters per call.
Pass section ("Item 7", "Item 8", "Note 9"; for a 10-Q "Part I Item 2") or start_line (e.g. a line from search_filing). Pass neither to get the filing's list of sections with line ranges.
If truncated is true, call again with next_start_line to continue. Cite what you use with the cite string or a narrower [filing_id Lx-y] range.
Example: read_section(filing_id="aapl-10k-fy2024", start_line=2000)"""

TICKER_PARAM = {"type": "string", "description": "Company ticker. Only AAPL is available.", "default": TICKER}
FILING_ID_PARAM = {"type": "string", "description": "A filing_id from list_filings, e.g. aapl-10k-fy2024."}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "list_filings",
        "description": LIST_FILINGS_DESCRIPTION,
        "parameters": {"type": "object", "properties": {
            "ticker": TICKER_PARAM,
            "form": {"type": "string", "enum": ["10-K", "10-Q"], "description": "Annual (10-K) or quarterly (10-Q)."},
            "fiscal_year_from": {"type": "integer", "minimum": 2000, "maximum": 2100},
            "fiscal_year_to": {"type": "integer", "minimum": 2000, "maximum": 2100},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_fact",
        "description": GET_FACT_DESCRIPTION,
        "parameters": {"type": "object", "properties": {
            "concept": {"type": "string", "description": "Exact us-gaap concept name, e.g. LongTermDebtNoncurrent."},
            "period": {"type": "string", "description": "FY2024, Q3FY2026 or YYYY-MM-DD. Omit for recent values."},
            "ticker": TICKER_PARAM,
        }, "required": ["concept"]},
    }},
    {"type": "function", "function": {
        "name": "search_filing",
        "description": SEARCH_FILING_DESCRIPTION,
        "parameters": {"type": "object", "properties": {
            "filing_id": FILING_ID_PARAM,
            "query": {"type": "string", "description": "Words to look for, in the filing's own vocabulary."},
            "section": {"type": "string", "description": "Optional: limit to one section, e.g. Item 7 or Note 9."},
        }, "required": ["filing_id", "query"]},
    }},
    {"type": "function", "function": {
        "name": "read_section",
        "description": READ_SECTION_DESCRIPTION,
        "parameters": {"type": "object", "properties": {
            "filing_id": FILING_ID_PARAM,
            "section": {"type": "string", "description": "Item 7, Item 8, Note 9, Part I Item 2, ..."},
            "start_line": {"type": "integer", "minimum": 1, "description": "Line to start reading from."},
        }, "required": ["filing_id"]},
    }},
]

TOOLS = {
    "list_filings": list_filings,
    "get_fact": get_fact,
    "search_filing": search_filing,
    "read_section": read_section,
}
PARAMETERS = {spec["function"]["name"]: spec["function"]["parameters"] for spec in TOOL_SPECS}


def run_tool(name: str, arguments: dict) -> dict:
    """Validate arguments, call the tool, and always return a dict. Errors come back as {'error': ..., 'hint': ...}."""
    if name not in TOOLS:
        return {"error": f"Unknown tool '{name}'.", "available": sorted(TOOLS)}
    try:
        return TOOLS[name](**validate_arguments(PARAMETERS[name], arguments))
    except ToolError as error:
        return error.to_dict()
    except Exception as error:  # a tool bug must reach the model as a message, not kill the run
        return {"error": f"The tool failed unexpectedly: {type(error).__name__}: {error}",
                "hint": "Try different arguments or a different tool."}


def validate_arguments(schema: dict, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ToolError("Arguments must be a JSON object.")
    properties = schema["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ToolError(f"Unknown argument(s): {', '.join(unknown)}.", hint=f"Valid arguments: {', '.join(properties)}.")
    missing = [key for key in schema["required"] if arguments.get(key) in (None, "")]
    if missing:
        raise ToolError(f"Missing required argument(s): {', '.join(missing)}.")

    clean = {}
    for key, value in arguments.items():
        if value in (None, ""):
            continue
        rules = properties[key]
        if rules["type"] == "integer":
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ToolError(f"'{key}' must be an integer, got {value!r}.")
        elif not isinstance(value, str):
            raise ToolError(f"'{key}' must be a string, got {value!r}.")
        if "enum" in rules:
            match = next((option for option in rules["enum"] if option.lower() == value.lower()), None)
            if match is None:
                raise ToolError(f"'{key}' must be one of: {', '.join(rules['enum'])}.")
            value = match
        clean[key] = value
    return clean
