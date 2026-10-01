"""Mechanical answer checks, run after every answer.

1. Figures: every dollar amount or percentage in the answer must appear in a tool result from
   this run (allowing for rounding, and for "85,750" in millions vs "$85.75 billion"), or be derived
   from backed numbers: a difference / percent change between two get_fact values, or a sum,
   difference or percent change of two figures the answer itself already backed.
2. Citations: every [filing_id Lx-y] must point at lines a tool returned in this run, and every
   [xbrl Concept period] at a fact get_fact returned in this run.

This is a cheap hallucination detector, not a proof. A figure can pass by coincidence when the
same digits appear somewhere in the text the run read, and a real number attached to the wrong
label (principal reported as interest) passes, because the number itself is backed.
"""

import bisect
import re

TOLERANCE = 0.006          # relative, for amounts: "$85.8 billion" matches 85,750 million
RATE_TOLERANCE = 0.051     # percentage points, for a quoted rate: "5.0%" matches 5.00%
CHANGE_TOLERANCE = 0.51    # percentage points, for a computed change: "10%" matches 10.3%
SCALES = (1, 1e3, 1e6, 1e9)
SCALE_WORDS = {"thousand": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9}

ANY_BRACKET = re.compile(r"\[[^\]]*\]")
TEXT_CITATION = re.compile(r"\[([a-z0-9-]+) L(\d+)(?:[-–](\d+))?\]", re.I)
XBRL_CITATION = re.compile(r"\[xbrl ([A-Za-z0-9]+) (?:\d{4}-\d{2}-\d{2}\.\.)?(\d{4}-\d{2}-\d{2})\]", re.I)
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
LINE_PREFIX = re.compile(r"^\d+\|", re.M)
NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
FIGURE = re.compile(
    r"(?<![\w.])(\$\s?)?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?"
    r"(?:\s*(%|percent\b|billion\b|million\b|thousand\b|bn\b|mn\b|b\b|m\b))?",
    re.I,
)
PARTIAL_PREFIX = "PARTIAL ANSWER:"


def check_answer(answer: str, results: list[dict]) -> dict:
    """Check an answer against the tool results the run actually received."""
    text_numbers, fact_values = set(), set()
    for result in results:
        collect_numbers(result, "", text_numbers, fact_values)
    numbers = sorted(text_numbers | fact_values)

    figures = extract_figures(answer)
    backed = [is_supported(f, numbers, fact_values) for f in figures]
    anchors = {abs(f["value"]) for f, ok in zip(figures, backed) if ok and f["kind"] == "amount"}
    unsupported = [f["text"] for f, ok in zip(figures, backed) if not ok and not is_derived(f, anchors)]

    ranges, fact_keys = sources_seen(results)
    citations = 0
    invalid = []
    for match in TEXT_CITATION.finditer(answer):
        citations += 1
        filing_id, line = match.group(1).lower(), int(match.group(2))
        if not any(seen_id == filing_id and low <= line <= high for seen_id, low, high in ranges):
            invalid.append(match.group(0))
    for match in XBRL_CITATION.finditer(answer):
        citations += 1
        if (match.group(1).lower(), match.group(2)) not in fact_keys:
            invalid.append(match.group(0))

    return {
        "figures_checked": len(figures),
        "unsupported_figures": unsupported,
        "citations": citations,
        "invalid_citations": invalid,
        "refused": is_refusal(answer),
        "passed": bool(answer.strip()) and not unsupported and not invalid and (citations > 0 or not figures),
    }


def is_refusal(answer: str) -> bool:
    """'NOT FOUND: ...', also after a forced stop's 'PARTIAL ANSWER:' label."""
    text = answer.strip()
    if text.upper().startswith(PARTIAL_PREFIX):
        text = text[len(PARTIAL_PREFIX):].strip()
    return text.upper().startswith("NOT FOUND")


def extract_figures(answer: str) -> list[dict]:
    """Dollar amounts, scaled amounts ('9.5 billion'), comma numbers and percentages. Skips citations, dates and years."""
    text = ISO_DATE.sub(" ", ANY_BRACKET.sub(" ", answer))
    figures = []
    for match in FIGURE.finditer(text):
        dollar, whole, decimals, unit = match.groups()
        number = float(whole.replace(",", "") + (decimals or ""))
        unit = (unit or "").lower()
        if unit in ("%", "percent"):
            figures.append({"text": match.group(0).strip(), "value": number, "kind": "percent"})
            continue
        scale = SCALE_WORDS.get(unit, 1)
        if not dollar and scale == 1 and "," not in whole:
            continue  # years, item numbers, counts
        figures.append({"text": match.group(0).strip(), "value": number * scale, "kind": "amount"})
    return figures


def collect_numbers(value, key: str, text_numbers: set, fact_values: set) -> None:
    """Numbers a run has seen: get_fact values, and numbers inside returned text and snippets."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            collect_numbers(child, child_key, text_numbers, fact_values)
    elif isinstance(value, list):
        for child in value:
            collect_numbers(child, key, text_numbers, fact_values)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if key in ("value", "other_values"):
            fact_values.add(abs(float(value)))
    elif isinstance(value, str) and key in ("text", "snippet"):
        for match in NUMBER.finditer(LINE_PREFIX.sub("", value)):
            text_numbers.add(float(match.group().replace(",", "")))


def is_supported(figure: dict, numbers: list[float], fact_values: set) -> bool:
    """Backed directly by a tool result, or derived from two get_fact values."""
    value = abs(figure["value"])
    if figure["kind"] == "percent":
        if has_close(numbers, value, RATE_TOLERANCE) or has_close(numbers, value / 100, RATE_TOLERANCE / 100):
            return True  # a rate quoted in text, or stored in XBRL as a fraction
        return any(abs(abs(new - old) / old * 100 - value) <= CHANGE_TOLERANCE
                   for old in fact_values for new in fact_values if old and new != old)

    # Scale either way: "$85.75 billion" vs a table's 85,750 (millions), or "85,750" vs raw XBRL dollars.
    for scale in SCALES:
        for target in (value / scale, value * scale):
            if target >= 1 and has_close(numbers, target, target * TOLERANCE):
                return True
    # "decreased by $9.5 billion": the difference between two reported facts
    return any(abs((a - b) - value) <= value * TOLERANCE for a in fact_values for b in fact_values if a > b)


def is_derived(figure: dict, anchors: set[float]) -> bool:
    """A sum, difference or percent change of two figures the answer already backed with evidence."""
    value = abs(figure["value"])
    pairs = [(a, b) for a in anchors for b in anchors if a != b]
    if figure["kind"] == "percent":
        return any(abs(abs(a - b) / b * 100 - value) <= CHANGE_TOLERANCE for a, b in pairs if b)
    return any(abs((a + b) - value) <= value * TOLERANCE or abs(abs(a - b) - value) <= value * TOLERANCE
               for a, b in pairs)


def has_close(numbers: list[float], target: float, tolerance: float) -> bool:
    index = bisect.bisect_left(numbers, target - tolerance)
    return index < len(numbers) and numbers[index] <= target + tolerance


def sources_seen(results: list[dict]) -> tuple[list[tuple[str, int, int]], set[tuple[str, str]]]:
    """Line ranges and XBRL facts that tool results pointed at, taken from their cite strings."""
    ranges, fact_keys = [], set()
    for cite in find_cites(results):
        if match := XBRL_CITATION.fullmatch(cite):
            fact_keys.add((match.group(1).lower(), match.group(2)))
        elif match := TEXT_CITATION.fullmatch(cite):
            start = int(match.group(2))
            ranges.append((match.group(1).lower(), start, int(match.group(3) or start)))
    return ranges, fact_keys


def find_cites(value) -> list[str]:
    if isinstance(value, dict):
        found = [value["cite"]] if isinstance(value.get("cite"), str) else []
        return found + [cite for child in value.values() for cite in find_cites(child)]
    if isinstance(value, list):
        return [cite for child in value for cite in find_cites(child)]
    return []
