"""Minimal stdlib HTML -> readable plain text converter.

Written for SEC filing primary documents (inline-XBRL HTML). Goals, in order:

1. Tables stay legible. The numbers in a filing live in tables, so cells are
   laid out in fixed-width columns that actually line up.
2. Headings and blocks stay separated by blank lines.
3. Nothing clever. No external dependencies.

It is not a general-purpose HTML renderer and does not try to be.
"""

from __future__ import annotations

import re
import textwrap
from html.parser import HTMLParser

# Tags whose *content* we throw away entirely.
DROP_CONTENT = {
    "script", "style", "head", "title", "noscript",
    "ix:header", "ix:hidden", "ix:references", "ix:resources",
}

BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main",
    "li", "ul", "ol", "dl", "dt", "dd", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

WRAP_WIDTH = 108
MAX_TABLE_WIDTH = 150
MAX_COL_WIDTH = 60

_NUMERIC = re.compile(r"^[\s$(]*-?[\d,]+(\.\d+)?[\s%)]*$")
_WS = re.compile(r"[ \t\r\f\v ]+")


class _Cell:
    __slots__ = ("parts", "colspan", "is_header")

    def __init__(self, colspan: int = 1, is_header: bool = False):
        self.parts: list[str] = []
        self.colspan = colspan
        self.is_header = is_header

    def text(self) -> str:
        return _WS.sub(" ", "".join(self.parts)).strip()


class _Table:
    def __init__(self):
        self.rows: list[list[_Cell]] = []

    def new_row(self):
        self.rows.append([])

    def new_cell(self, colspan: int, is_header: bool):
        if not self.rows:
            self.new_row()
        cell = _Cell(colspan, is_header)
        self.rows[-1].append(cell)
        return cell


def _render_table(table: _Table) -> list[str]:
    """Lay a parsed table out in fixed-width columns."""
    grid: list[list[str]] = []
    for row in table.rows:
        out_row: list[str] = []
        for cell in row:
            txt = cell.text()
            out_row.append(txt)
            for _ in range(max(0, cell.colspan - 1)):
                out_row.append("")
        grid.append(out_row)

    if not grid:
        return []

    width = max(len(r) for r in grid)
    for r in grid:
        r.extend([""] * (width - len(r)))

    # Drop columns that are empty everywhere (filings are full of spacer cells).
    keep = [c for c in range(width) if any(r[c] for r in grid)]
    if not keep:
        return []
    grid = [[r[c] for c in keep] for r in grid]

    # Drop rows that ended up completely empty.
    grid = [r for r in grid if any(cell for cell in r)]
    if not grid:
        return []

    ncols = len(grid[0])
    widths = []
    for c in range(ncols):
        w = max(len(r[c]) for r in grid)
        widths.append(min(w, MAX_COL_WIDTH))

    # If the table is absurdly wide, shrink the widest text columns.
    while sum(widths) + 2 * (ncols - 1) > MAX_TABLE_WIDTH and max(widths) > 12:
        widest = widths.index(max(widths))
        widths[widest] = max(12, widths[widest] - 4)

    right_align = []
    for c in range(ncols):
        vals = [r[c] for r in grid if r[c]]
        right_align.append(bool(vals) and all(_NUMERIC.match(v) for v in vals))

    lines: list[str] = []
    for r in grid:
        # A cell longer than its column gets wrapped into continuation lines.
        wrapped = []
        for c, val in enumerate(r):
            if len(val) <= widths[c]:
                wrapped.append([val])
            else:
                wrapped.append(textwrap.wrap(val, widths[c]) or [""])
        height = max(len(w) for w in wrapped)
        for i in range(height):
            pieces = []
            for c in range(ncols):
                piece = wrapped[c][i] if i < len(wrapped[c]) else ""
                pieces.append(piece.rjust(widths[c]) if right_align[c] else piece.ljust(widths[c]))
            lines.append("  ".join(pieces).rstrip())
    return lines


class _Converter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.buf: list[str] = []
        self.drop_depth = 0
        self.drop_tag: str | None = None
        self.tables: list[_Table] = []
        self.cell: _Cell | None = None
        self.heading = 0

    # -- output helpers -------------------------------------------------
    def _flush(self, blank_after: bool = False):
        text = _WS.sub(" ", "".join(self.buf)).strip()
        self.buf = []
        if text:
            for para in text.split("\n"):
                para = para.strip()
                if not para:
                    continue
                self.lines.extend(textwrap.wrap(para, WRAP_WIDTH) or [para])
            if blank_after:
                self.lines.append("")

    def _emit(self, block: list[str]):
        self._flush()
        if not block:
            return
        if self.lines and self.lines[-1] != "":
            self.lines.append("")
        self.lines.extend(block)
        self.lines.append("")

    def _write(self, text: str):
        if self.cell is not None:
            self.cell.parts.append(text)
        else:
            self.buf.append(text)

    # -- parser callbacks -----------------------------------------------
    def handle_starttag(self, tag, attrs):
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth += 1
            return
        if tag in DROP_CONTENT:
            self.drop_depth = 1
            self.drop_tag = tag
            return

        if tag == "table":
            if not self.tables:
                self._flush(blank_after=True)
            self.tables.append(_Table())
            self.cell = None
            return
        if self.tables:
            if tag == "tr":
                self.tables[-1].new_row()
                self.cell = None
                return
            if tag in ("td", "th"):
                attrd = dict(attrs)
                try:
                    colspan = int(str(attrd.get("colspan", "1")).strip() or 1)
                except ValueError:
                    colspan = 1
                self.cell = self.tables[-1].new_cell(max(1, min(colspan, 40)), tag == "th")
                return
            if tag == "br":
                self._write(" ")
                return
            return

        if tag == "br":
            self._flush()
            return
        if tag in HEADING_TAGS:
            self._flush(blank_after=True)
            self.heading += 1
            return
        if tag in BLOCK_TAGS:
            self._flush(blank_after=True)
            return

    def handle_endtag(self, tag):
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth -= 1
                if self.drop_depth == 0:
                    self.drop_tag = None
            return

        if tag == "table" and self.tables:
            table = self.tables.pop()
            rendered = _render_table(table)
            if self.tables:
                # Nested table: fold it into the enclosing cell as inline text.
                self.cell = None
                if rendered and self.tables[-1].rows and self.tables[-1].rows[-1]:
                    self.tables[-1].rows[-1][-1].parts.append(" " + " ".join(rendered))
            else:
                self.cell = None
                self._emit(rendered)
            return

        if self.tables:
            if tag in ("td", "th"):
                self.cell = None
            elif tag == "tr":
                self.cell = None
            return

        if tag in HEADING_TAGS:
            text = _WS.sub(" ", "".join(self.buf)).strip()
            self.buf = []
            self.heading = max(0, self.heading - 1)
            if text:
                if self.lines and self.lines[-1] != "":
                    self.lines.append("")
                self.lines.append(text)
                self.lines.append("")
            return
        if tag in BLOCK_TAGS:
            self._flush(blank_after=True)

    def handle_data(self, data):
        if self.drop_depth:
            return
        if data.strip() or (self.cell is None and data):
            self._write(data)

    def close(self):
        super().close()
        self._flush(blank_after=True)


def html_to_text(html: str) -> str:
    conv = _Converter()
    conv.feed(html)
    conv.close()

    lines = conv.lines
    # Collapse runs of blank lines down to one.
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def add_line_markers(body: str, every: int = 200) -> str:
    """Insert `----- line N -----` markers so a reader can cite a location."""
    lines = body.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines, start=1):
        out.append(line)
        if i % every == 0:
            out.append("")
            out.append(f"----- line {i} -----")
            out.append("")
    return "\n".join(out)
