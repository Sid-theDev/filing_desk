"""XBRL facts from companyfacts.json: concept lookup, fiscal period labels, and fact rows."""

import json
import re
from functools import lru_cache

from .config import COMPANY_DIR, MAX_CANDIDATES
from .errors import ToolError

IGNORED_WORDS = {"us", "gaap", "of", "the", "and", "apple", "total", "s"}


@lru_cache(maxsize=1)
def load_concepts() -> dict:
    """All us-gaap concepts Apple has reported, as EDGAR sent them."""
    data = json.loads((COMPANY_DIR / "companyfacts.json").read_text(encoding="utf-8"))
    return data["facts"]["us-gaap"]


@lru_cache(maxsize=1)
def load_fiscal_calendar() -> dict[str, str]:
    """Fiscal period label -> period end date, e.g. {"FY2024": "2024-09-28", "Q3FY2026": "2026-06-27"}."""
    data = json.loads((COMPANY_DIR / "submissions.json").read_text(encoding="utf-8"))
    recent = data["filings"]["recent"]
    calendar = {}
    for form, period_end in zip(recent["form"], recent["reportDate"]):
        if form in ("10-K", "10-Q") and period_end:
            calendar[fiscal_label(form, period_end)] = period_end
    return calendar


def fiscal_label(form: str, period_end: str) -> str:
    """Apple's fiscal year ends on the last Saturday of September; there is no Q4 10-Q."""
    year, month = int(period_end[:4]), int(period_end[5:7])
    if form == "10-K":
        return f"FY{year + 1 if month >= 11 else year}"
    if month in (12, 1):
        return f"Q1FY{year + 1 if month == 12 else year}"
    if month in (3, 4):
        return f"Q2FY{year}"
    if month in (6, 7):
        return f"Q3FY{year}"
    return f"Q4FY{year}"


def label_for_date(period_end: str) -> str | None:
    return next((label for label, end in load_fiscal_calendar().items() if end == period_end), None)


def resolve_period(period: str) -> tuple[str, str]:
    """Turn 'FY2024', 'Q3 FY2026', '2024' or '2024-09-28' into (label, period_end)."""
    text = (period or "").strip().upper().replace(" ", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return label_for_date(text) or text, text
    if re.fullmatch(r"\d{4}", text):
        text = f"FY{text}"
    calendar = load_fiscal_calendar()
    if text in calendar:
        return text, calendar[text]
    labels = sorted(calendar, key=calendar.get)
    raise ToolError(
        f"No Apple 10-K or 10-Q covers the period '{period}'.",
        hint=(
            f"Use a label like FY2024 or Q3FY2026, or a YYYY-MM-DD date. Known periods run from "
            f"{labels[0]} ({calendar[labels[0]]}) to {labels[-1]} ({calendar[labels[-1]]})."
        ),
    )


def split_words(text: str) -> set[str]:
    """'LongTermDebtNoncurrent' or 'Long-term debt' -> {'long', 'term', 'debt', ...}."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return {word for word in re.split(r"[^a-z0-9]+", spaced.lower()) if word}


def find_concept(name: str) -> tuple[str, dict]:
    """Exact concept lookup (case-insensitive, 'us-gaap:' prefix optional). Never guesses."""
    concepts = load_concepts()
    wanted = (name or "").strip().split(":")[-1].lower()
    for key, entry in concepts.items():
        if key.lower() == wanted:
            return key, entry
    raise ToolError(
        f"'{name}' is not an XBRL concept Apple reports.",
        candidates=suggest_concepts(name),
        hint="Pick the candidate whose label means what you need, then call get_fact again with its exact name.",
    )


def suggest_concepts(query: str) -> list[dict]:
    """Concepts sharing the most words with the query, by name and label."""
    words = split_words(query or "") - IGNORED_WORDS
    scored = []
    for key, entry in load_concepts().items():
        label = entry.get("label") or ""
        overlap = len(words & (split_words(key) | split_words(label)))
        if overlap:
            scored.append((-overlap, len(key), key, label))
    scored.sort()
    return [{"concept": key, "label": label} for _, _, key, label in scored[:MAX_CANDIDATES]]


def fact_rows(entry: dict) -> list[dict]:
    """One row per (unit, start, end), valued from the earliest filing that reported it.

    Later filings repeat a period as a comparative, sometimes with a different value
    (rounding, restatement). Those values are kept in `other_values`, never hidden.
    """
    groups = {}
    for unit, items in entry.get("units", {}).items():
        for item in items:
            groups.setdefault((unit, item.get("start"), item["end"]), []).append(item)

    rows = []
    for (unit, start, end), items in groups.items():
        items.sort(key=lambda item: item.get("filed", ""))
        first = items[0]
        rows.append({
            "unit": unit,
            "start": start,
            "end": end,
            "value": first["val"],
            "period_type": "duration" if start else "instant",
            "form": first.get("form"),
            "accession": first.get("accn"),
            "filed": first.get("filed"),
            "times_reported": len(items),
            "other_values": sorted({item["val"] for item in items if item["val"] != first["val"]}),
        })
    rows.sort(key=lambda row: (row["end"], row["start"] or ""))
    return rows
