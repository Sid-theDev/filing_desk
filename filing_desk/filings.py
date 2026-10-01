"""Filings on disk: which ones exist, their text lines, and their section map.

A filing's text comes from data/reading/<filing_id>.txt. Line numbers used everywhere
(tool results, citations) are body line numbers, the same numbers printed in the
`----- line N -----` markers of those files, so a human can find a cited line by eye.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import READING_DIR
from .errors import ToolError

LINE_MARKER = re.compile(r"^----- line \d+ -----$")
HEADER_FIELD = re.compile(r"^\s+([A-Za-z ]+?):\s+(\S.*)$")
PART_HEADING = re.compile(r"^PART (IV|I{1,3})\b")        # upper case only: the table of contents says "Part I"
ITEM_HEADING = re.compile(r"^Item (\d{1,2}[A-C]?)\.\s+(\S.*)$")
NOTE_HEADING = re.compile(r"^Note (\d{1,2}) [–-] (\S.*)$")
ENDS_IN_PAGE_NUMBER = re.compile(r"\s\d{1,3}$")           # table-of-contents rows end in a page number
SECTION_REQUEST = re.compile(r"^(?:part\s*(iv|i{1,3})\b\W*)?(item|note)?\s*(\d{1,2}[a-c]?)\b", re.I)


@dataclass(frozen=True)
class Section:
    name: str         # "Item 7", "Note 9", or for a 10-Q "Part I Item 2"
    title: str
    start_line: int   # first line, 1-based
    end_line: int     # last line, inclusive

    def to_dict(self) -> dict:
        return {"section": self.name, "title": self.title, "lines": f"{self.start_line}-{self.end_line}"}


@dataclass
class Filing:
    filing_id: str        # e.g. "aapl-10k-fy2024"
    form: str             # "10-K" or "10-Q"
    fiscal_period: str    # "FY2024" or "Q3FY2026"
    fiscal_year: int
    period_end: str
    filed: str
    accession: str
    source_url: str
    lines: list[str]
    sections: list[Section]

    def summary(self) -> dict:
        return {
            "filing_id": self.filing_id,
            "form": self.form,
            "fiscal_period": self.fiscal_period,
            "period_end": self.period_end,
            "filed": self.filed,
            "accession": self.accession,
        }


@lru_cache(maxsize=1)
def load_filings() -> dict[str, Filing]:
    """Every filing with a reading copy, keyed by filing_id."""
    filings = {}
    for path in sorted(READING_DIR.glob("*.txt")):
        filing = read_filing(path)
        filings[filing.filing_id] = filing
    return filings


def get_filing(filing_id: str) -> Filing:
    filings = load_filings()
    key = (filing_id or "").strip().lower()
    if key not in filings:
        raise ToolError(
            f"Unknown filing_id '{filing_id}'.",
            available=sorted(filings),
            hint="Use list_filings to see valid filing ids.",
        )
    return filings[key]


def filing_id_for_accession(accession: str) -> str | None:
    return next((f.filing_id for f in load_filings().values() if f.accession == accession), None)


def read_filing(path: Path) -> Filing:
    all_lines = path.read_text(encoding="utf-8").split("\n")
    rules = [i for i, line in enumerate(all_lines) if line.startswith("=" * 20)]
    header_end = rules[1]

    header = {}
    for line in all_lines[1:header_end]:
        match = HEADER_FIELD.match(line)
        if match:
            header[match.group(1)] = match.group(2).strip()

    lines = remove_line_markers(all_lines[header_end + 1:])
    while lines and lines[-1] == "":
        lines.pop()

    form = header["Form"]
    fiscal_period = header["Fiscal period"].split()[0]
    return Filing(
        filing_id=path.stem,
        form=form,
        fiscal_period=fiscal_period,
        fiscal_year=int(re.search(r"FY(\d{4})", fiscal_period).group(1)),
        period_end=header["Period end date"],
        filed=header["Filing date"],
        accession=header["Accession number"],
        source_url=header["Source URL"],
        lines=lines,
        sections=find_sections(lines, form),
    )


def remove_line_markers(lines: list[str]) -> list[str]:
    """Undo add_line_markers(): each marker was inserted as a blank line, the marker, a blank line."""
    body = []
    skip_blank = False
    for line in lines:
        if skip_blank:
            skip_blank = False
            if line == "":
                continue
        if LINE_MARKER.match(line):
            if body and body[-1] == "":
                body.pop()
            skip_blank = True
            continue
        body.append(line)
    return body


def find_sections(lines: list[str], form: str) -> list[Section]:
    """Locate Item and Note headings, skipping the table of contents."""
    headings = []  # (line_number, kind, name, title)
    part = ""
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if match := PART_HEADING.match(line):
            part = match.group(1)
            headings.append((number, "part", "", ""))
        elif (match := ITEM_HEADING.match(line)) and not ENDS_IN_PAGE_NUMBER.search(line):
            name = f"Item {match.group(1)}"
            if form == "10-Q":
                name = f"Part {part} {name}"
            headings.append((number, "item", name, match.group(2)))
        elif match := NOTE_HEADING.match(line):
            headings.append((number, "note", f"Note {match.group(1)}", match.group(2)))

    sections = []
    seen = set()
    for index, (number, kind, name, title) in enumerate(headings):
        if kind == "part" or name in seen:
            continue
        # An Item ends at the next Item or Part. A Note also ends at the next Note.
        stops = {"item", "part"} if kind == "item" else {"item", "part", "note"}
        next_start = next((n for n, k, _, _ in headings[index + 1:] if k in stops), len(lines) + 1)
        sections.append(Section(name, title, number, next_start - 1))
        seen.add(name)
    return sections


def find_section(filing: Filing, requested: str) -> Section:
    """Resolve 'Item 7', '7A', 'Note 9' or 'Part I Item 2' to a section of this filing."""
    match = SECTION_REQUEST.match((requested or "").strip())
    matches = []
    if match:
        part, kind, number = match.groups()
        number = number.upper()
        if (kind or "item").lower() == "note":
            matches = [s for s in filing.sections if s.name == f"Note {number}"]
        else:
            matches = [s for s in filing.sections if s.name.endswith(f"Item {number}")]
            if part and filing.form == "10-Q":
                matches = [s for s in matches if s.name.startswith(f"Part {part.upper()} ")]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(
            f"'{requested}' is ambiguous in {filing.filing_id}.",
            options=[s.to_dict() for s in matches],
            hint="Name the part too, e.g. 'Part I Item 2'.",
        )
    raise ToolError(
        f"No section '{requested}' in {filing.filing_id}.",
        available=[f"{s.name} – {s.title}" for s in filing.sections],
    )


def section_label(filing: Filing, line_number: int) -> str:
    """Where a line sits, e.g. 'Item 8 > Note 9'."""
    item = note = None
    for section in filing.sections:
        if section.start_line <= line_number <= section.end_line:
            if section.name.startswith("Note"):
                note = section
            else:
                item = section
    return " > ".join(s.name for s in (item, note) if s) or "front matter"
