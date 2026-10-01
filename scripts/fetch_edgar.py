#!/usr/bin/env python3
"""Filing Desk - Phase 1 fetcher.

Pulls the raw material for the Phase 1 exercise (read one number out of an
Apple filing by hand, then reconcile it against XBRL) from SEC EDGAR.

Everything it downloads is written to data/raw/ byte-for-byte and never
touched again. Derived, human-readable material is written to data/reading/
and data/ANSWER_KEY/ from those cached bytes.

Re-running is safe and cheap: a file that already exists under data/raw/ is
never re-requested, so a second run needs no network at all.

Usage:
    python scripts/fetch_edgar.py            # fetch (using cache) + rebuild derived files
    python scripts/fetch_edgar.py --offline  # fail loudly instead of touching the network
    python scripts/fetch_edgar.py --rebuild  # derived files only, no network, no fetch attempt

Requires a .env in the project root with:
    SEC_CONTACT_EMAIL=you@example.com
    SEC_USER_AGENT=FilingDesk/0.1 (you@example.com)
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from html_to_text import add_line_markers, html_to_text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
READING = ROOT / "data" / "reading"
ANSWER_KEY = ROOT / "data" / "ANSWER_KEY"
MANIFEST_PATH = RAW / "_manifest.json"

CIK = "0000320193"          # Apple Inc., confirmed against company_tickers.json at runtime
CIK_SHORT = "320193"
TICKER = "AAPL"

MIN_INTERVAL = 0.30         # seconds between requests -> ~3.3 req/s, well under EDGAR's 10
TARGET_10K_FISCAL_YEARS = [2024, 2023, 2022]

# Concepts that are unambiguously borrowings but whose names contain no "debt".
# A substring search of companyfacts misses all three - which is itself worth
# knowing before you trust name matching to resolve a concept.
EXTRA_DEBT_CONCEPTS = [
    "CommercialPaper",
    "OtherShortTermBorrowings",
    "ProceedsFromRepaymentsOfCommercialPaper",
]


def is_investment_concept(name: str) -> bool:
    """True for concepts about debt securities Apple *holds*, not debt it owes."""
    return "AvailableForSale" in name or "DebtSecurities" in name


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------
def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(
            f"Missing {path}. Copy .env.example to .env and put a real contact "
            "email in it - EDGAR rejects requests without one."
        )
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    ua = env.get("SEC_USER_AGENT", "").strip()
    email = env.get("SEC_CONTACT_EMAIL", "").strip()
    if not ua or "@" not in ua:
        sys.exit("SEC_USER_AGENT in .env must contain a contact email, e.g. 'FilingDesk/0.1 (you@example.com)'.")
    if not email or "@" not in email:
        sys.exit("SEC_CONTACT_EMAIL in .env must be a real email address.")
    if "example.com" in ua or "example.com" in email:
        sys.exit("Your .env still holds the placeholder email from .env.example. Put a real one in.")
    return env


# --------------------------------------------------------------------------
# fetching + caching
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, user_agent: str, offline: bool = False):
        self.user_agent = user_agent
        self.offline = offline
        self.last_request = 0.0
        self.manifest: dict[str, dict] = {}
        if MANIFEST_PATH.exists():
            try:
                self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.manifest = {}
        self.hits = 0
        self.misses = 0
        self.failures: list[tuple[str, str]] = []

    def _throttle(self):
        gap = time.monotonic() - self.last_request
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        self.last_request = time.monotonic()

    def get(self, url: str, dest: Path, note: str = "") -> bytes | None:
        """Return the exact response bytes, fetching only if not already cached."""
        rel = dest.relative_to(ROOT).as_posix()
        if dest.exists() and dest.stat().st_size > 0:
            self.hits += 1
            data = dest.read_bytes()
            entry = self.manifest.setdefault(rel, {})
            entry.setdefault("url", url)
            entry.setdefault("fetched_at", dt.datetime.fromtimestamp(dest.stat().st_mtime, dt.timezone.utc)
                             .isoformat(timespec="seconds"))
            entry["bytes"] = len(data)
            if note:
                entry["note"] = note
            print(f"  cache  {rel}  ({len(data):,} bytes)")
            return data

        if self.offline:
            self.failures.append((url, "offline mode: not cached and network disabled"))
            print(f"  MISS   {rel}  (offline, not cached)")
            return None

        self._throttle()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "*/*",
                "Host": urllib.parse.urlsplit(url).netloc,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.GzipFile(fileobj=io.BytesIO(payload)).read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code} {exc.reason}"
            self.failures.append((url, reason))
            print(f"  FAIL   {url} -> {reason}")
            return None
        except Exception as exc:  # noqa: BLE001 - want the reason surfaced, whatever it is
            reason = f"{type(exc).__name__}: {exc}"
            self.failures.append((url, reason))
            print(f"  FAIL   {url} -> {reason}")
            return None

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)  # exact bytes, before anything else happens to them
        self.misses += 1
        self.manifest[rel] = {
            "url": url,
            "bytes": len(payload),
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "http_status": status,
        }
        if note:
            self.manifest[rel]["note"] = note
        print(f"  GET    {rel}  ({len(payload):,} bytes)")
        return payload

    def save_manifest(self):
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(
            json.dumps(dict(sorted(self.manifest.items())), indent=2) + "\n", encoding="utf-8"
        )


# --------------------------------------------------------------------------
# fiscal-period helpers (Apple's fiscal year ends the last Saturday of September)
# --------------------------------------------------------------------------
def fy_for_period_end(period_end: str) -> int:
    """Fiscal year label for a period end date. Apple's FY2024 ended 2024-09-28."""
    y, m, _ = (int(x) for x in period_end.split("-"))
    return y + 1 if m >= 11 else y


def quarter_label(period_end: str) -> str:
    """Q1..Q3 label for a 10-Q period end, using Apple's late-September year end."""
    y, m, _ = (int(x) for x in period_end.split("-"))
    if m in (12, 1):
        q, fy = 1, (y + 1 if m == 12 else y)
    elif m in (3, 4):
        q, fy = 2, y
    elif m in (6, 7):
        q, fy = 3, y
    else:
        q, fy = 4, y
    return f"Q{q}FY{fy}"


def accn_nodash(accn: str) -> str:
    return accn.replace("-", "")


# --------------------------------------------------------------------------
# submissions handling
# --------------------------------------------------------------------------
def iter_filings(submissions: dict):
    """Yield every filing record in a submissions document's `recent` block."""
    recent = submissions.get("filings", {}).get("recent", {})
    if not recent:
        return
    n = len(recent.get("accessionNumber", []))
    keys = list(recent.keys())
    for i in range(n):
        yield {k: (recent[k][i] if i < len(recent[k]) else None) for k in keys}


def iter_extra_filings(blob: dict):
    """Older filings come back as a flat dict-of-lists, same shape, no `filings` wrapper."""
    n = len(blob.get("accessionNumber", []))
    keys = list(blob.keys())
    for i in range(n):
        yield {k: (blob[k][i] if i < len(blob[k]) else None) for k in keys}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Phase 1 raw material from SEC EDGAR.")
    ap.add_argument("--offline", action="store_true", help="never touch the network; use the cache only")
    ap.add_argument("--rebuild", action="store_true", help="rebuild derived files from cache, no fetching")
    args = ap.parse_args()

    env = load_env(ROOT / ".env")
    ua = env["SEC_USER_AGENT"]
    fetch = Fetcher(ua, offline=args.offline or args.rebuild)

    for d in (RAW, RAW / "aapl" / "filings", READING, ANSWER_KEY):
        d.mkdir(parents=True, exist_ok=True)

    print("\n[1/7] ticker -> CIK map")
    tickers_raw = fetch.get(
        "https://www.sec.gov/files/company_tickers.json",
        RAW / "company_tickers.json",
        note="SEC ticker -> CIK mapping for every registrant",
    )
    if tickers_raw:
        tmap = json.loads(tickers_raw)
        found = [v for v in tmap.values() if v.get("ticker") == TICKER]
        if found:
            cik = str(found[0]["cik_str"]).zfill(10)
            print(f"         {TICKER} -> CIK {cik}  ({found[0]['title']})")
            if cik != CIK:
                print(f"         WARNING: expected {CIK}, got {cik}")

    print("\n[2/7] submissions record")
    subs_raw = fetch.get(
        f"https://data.sec.gov/submissions/CIK{CIK}.json",
        RAW / "aapl" / "submissions.json",
        note="Apple filing history: form, filing date, period, accession, primary document",
    )
    if not subs_raw:
        print("Cannot continue without the submissions record.")
        fetch.save_manifest()
        return 1
    submissions = json.loads(subs_raw)

    print("\n[3/7] company facts (XBRL)")
    facts_raw = fetch.get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json",
        RAW / "aapl" / "companyfacts.json",
        note="Every XBRL fact Apple has ever reported, all concepts, all periods",
    )
    if not facts_raw:
        print("Cannot continue without company facts.")
        fetch.save_manifest()
        return 1
    companyfacts = json.loads(facts_raw)

    # ---- 4. debt concepts that actually exist for Apple --------------------
    print("\n[4/7] debt-related us-gaap concepts present in Apple's facts")
    usgaap = companyfacts.get("facts", {}).get("us-gaap", {})
    debt_concepts = sorted(
        {n for n in usgaap if "debt" in n.lower()}
        | {n for n in EXTRA_DEBT_CONCEPTS if n in usgaap}
    )
    for name in debt_concepts:
        units = ", ".join(usgaap[name].get("units", {}).keys())
        count = sum(len(v) for v in usgaap[name].get("units", {}).values())
        print(f"         {name:<52} {count:>4} facts  [{units}]")
    if not debt_concepts:
        print("         (none found - that would be very surprising)")

    for name in debt_concepts:
        fetch.get(
            f"https://data.sec.gov/api/xbrl/companyconcept/CIK{CIK}/us-gaap/{name}.json",
            RAW / "aapl" / f"concept_{name}.json",
            note=f"Every reported value of us-gaap:{name}",
        )

    # ---- 5/6. pick the filings --------------------------------------------
    print("\n[5/7] locating filings")
    all_filings = list(iter_filings(submissions))
    for extra in submissions.get("filings", {}).get("files", []):
        name = extra.get("name")
        if not name:
            continue
        blob = fetch.get(
            f"https://data.sec.gov/submissions/{name}",
            RAW / "aapl" / f"submissions_{name}",
            note="Older page of Apple's filing history",
        )
        if blob:
            all_filings.extend(iter_extra_filings(json.loads(blob)))

    tenks = [f for f in all_filings if f.get("form") == "10-K"]
    tenqs = [f for f in all_filings if f.get("form") == "10-Q"]
    tenks.sort(key=lambda f: f.get("reportDate") or "", reverse=True)
    tenqs.sort(key=lambda f: f.get("reportDate") or "", reverse=True)

    selected: list[dict] = []
    for fy in TARGET_10K_FISCAL_YEARS:
        match = next((f for f in tenks if f.get("reportDate") and fy_for_period_end(f["reportDate"]) == fy), None)
        if match is None:
            print(f"  WARNING: no 10-K found for FY{fy}")
            continue
        selected.append({
            "form": "10-K",
            "label": f"FY{fy}",
            "fiscal_year": fy,
            "record": match,
        })
    if tenqs:
        q = tenqs[0]
        selected.append({
            "form": "10-Q",
            "label": quarter_label(q["reportDate"]),
            "fiscal_year": fy_for_period_end(q["reportDate"]),
            "record": q,
        })
    else:
        print("  WARNING: no 10-Q found")

    for sel in selected:
        rec = sel["record"]
        print(f"         {sel['form']:<5} {sel['label']:<8} period {rec.get('reportDate')}  "
              f"filed {rec.get('filingDate')}  {rec.get('accessionNumber')}  {rec.get('primaryDocument')}")

    print("\n[6/7] filing index + primary document")
    for sel in selected:
        rec = sel["record"]
        accn = rec["accessionNumber"]
        nod = accn_nodash(accn)
        folder = RAW / "aapl" / "filings" / f"{sel['form']}_{sel['label']}_{accn}"
        folder.mkdir(parents=True, exist_ok=True)
        base = f"https://www.sec.gov/Archives/edgar/data/{CIK_SHORT}/{nod}"

        idx = fetch.get(f"{base}/index.json", folder / "index.json",
                        note=f"File listing for {sel['form']} {sel['label']}")
        sel["index_ok"] = idx is not None

        primary = rec.get("primaryDocument")
        if not primary:
            print(f"  WARNING: no primaryDocument recorded for {accn}")
            continue
        doc = fetch.get(f"{base}/{primary}", folder / primary,
                        note=f"Primary document of {sel['form']} {sel['label']}")
        sel["primary_path"] = folder / primary
        sel["primary_url"] = f"{base}/{primary}"
        sel["folder"] = folder
        sel["ok"] = doc is not None

    fetch.save_manifest()

    # ---- 7. derived material ----------------------------------------------
    print("\n[7/7] building reading copies, answer key and DATA_README.md")
    build_reading_files(selected, fetch.manifest, submissions)
    build_answer_key(companyfacts, debt_concepts, selected)
    build_data_readme(selected, fetch.manifest, debt_concepts, submissions)

    print(f"\nDone. cache hits: {fetch.hits}, network fetches: {fetch.misses}, failures: {len(fetch.failures)}")
    for url, reason in fetch.failures:
        print(f"  FAILED  {url}\n          {reason}")
    return 0 if not fetch.failures else 2


# --------------------------------------------------------------------------
# derived: reading copies
# --------------------------------------------------------------------------
def build_reading_files(selected: list[dict], manifest: dict, submissions: dict):
    company = submissions.get("name", "Apple Inc.")
    for sel in selected:
        path = sel.get("primary_path")
        if not path or not path.exists():
            continue
        rec = sel["record"]
        html = path.read_bytes().decode("utf-8", errors="replace")
        body = html_to_text(html)
        body_lines = body.count("\n") + 1
        body = add_line_markers(body, 200)

        rel = path.relative_to(ROOT).as_posix()
        fetched = manifest.get(rel, {}).get("fetched_at", "unknown")

        if sel["form"] == "10-K":
            out_name = f"aapl-10k-fy{sel['fiscal_year']}.txt"
            period_desc = f"Fiscal year {sel['fiscal_year']} (Apple's FY ends the last Saturday of September)"
        else:
            out_name = f"aapl-10q-{sel['label'].lower()}.txt"
            period_desc = f"Fiscal quarter {sel['label']}"

        header = [
            "=" * 100,
            f"  {company}  ({TICKER}, CIK {CIK})",
            f"  Form:              {sel['form']}",
            f"  Fiscal period:     {sel['label']}   {period_desc}",
            f"  Period end date:   {rec.get('reportDate')}",
            f"  Filing date:       {rec.get('filingDate')}",
            f"  Accession number:  {rec.get('accessionNumber')}",
            f"  Source URL:        {sel.get('primary_url', '')}",
            f"  Raw bytes cached:  {rel}",
            f"  Fetched (UTC):     {fetched}",
            f"  Converted by:      scripts/html_to_text.py (stdlib only; tables laid out fixed-width)",
            f"  Body lines:        {body_lines:,}   (markers excluded)",
            f"  Total lines:       {{TOTAL_LINES}}   (this header + body + markers)",
            "",
            "  Navigation: a line reading `----- line N -----` marks the Nth line of the body,",
            "  counting from the first line after this header block. Cite locations by that number.",
            "=" * 100,
            "",
        ]
        text = "\n".join(header) + body + "\n"
        total = text.count("\n")
        text = text.replace("{TOTAL_LINES}", f"{total:,}")
        (READING / out_name).write_text(text, encoding="utf-8")
        print(f"  wrote  data/reading/{out_name}  ({body_lines:,} body lines)")


# --------------------------------------------------------------------------
# derived: answer key
# --------------------------------------------------------------------------
def build_answer_key(companyfacts: dict, debt_concepts: list[str], selected: list[dict]):
    ANSWER_KEY.mkdir(parents=True, exist_ok=True)
    (ANSWER_KEY / "README.md").write_text(
        "Don't open the file next to this until you've read the filing yourself.\n",
        encoding="utf-8",
    )

    period_ends = {sel["record"].get("reportDate") for sel in selected if sel["record"].get("reportDate")}
    accn_of_interest = {sel["record"]["accessionNumber"] for sel in selected}
    earliest = min(period_ends) if period_ends else "2021-09-01"
    # Widen the window one fiscal year back, so the comparative column in the
    # FY2022 10-K also has a matching fact.
    window_start = f"{int(earliest[:4]) - 1}-01-01"

    usgaap = companyfacts.get("facts", {}).get("us-gaap", {})
    obligation = [c for c in debt_concepts if not is_investment_concept(c)]
    investment = [c for c in debt_concepts if is_investment_concept(c)]
    out: list[str] = []
    out.append("# Long-term debt: the XBRL side")
    out.append("")
    out.append("Read the filings by hand first. This file is the reconciliation target, not the shortcut.")
    out.append("")
    out.append(f"Generated by `scripts/fetch_edgar.py` from `data/raw/aapl/companyfacts.json` "
               f"on {dt.date.today().isoformat()}.")
    out.append("")
    out.append("## How to read these tables")
    out.append("")
    out.append("- **Period type** - `instant` is a balance at a single date (balance-sheet items work this way); "
               "`duration` is a flow measured across a range (income-statement and cash-flow items).")
    out.append("- **Accession** - the filing that reported this value. The same period often appears in several "
               "filings: the original, then again as a comparative in later ones.")
    out.append("- **Frame / fy / fp** - EDGAR's own fiscal labelling of the *filing* the fact came from, not of the "
               "fact's period. They disagree more often than you would like.")
    out.append("- Units are as reported. Nothing here has been rescaled.")
    out.append("")
    out.append("Facts are limited to periods ending on or after "
               f"`{window_start}`, which covers every column of every filing fetched for Phase 1.")
    out.append("")
    out.append("Accession numbers belonging to the four filings you are reading by hand are marked "
               "**(fetched)**.")
    out.append("")
    out.append("Only concepts describing money Apple **owes** are tabulated below. Concepts about debt "
               "securities Apple **holds** as investments are listed at the end instead - they match a "
               "search for the word 'debt' and mean the opposite thing, which is the first trap in this "
               "data.")
    out.append("")
    summary_slot = len(out)

    restatement_notes: list[str] = []

    for concept in obligation:
        entry = usgaap.get(concept, {})
        label = entry.get("label") or concept
        desc = (entry.get("description") or "").strip()
        rows = []
        for unit, facts in entry.get("units", {}).items():
            for f in facts:
                end = f.get("end", "")
                if end < window_start:
                    continue
                start = f.get("start")
                ptype = "duration" if start else "instant"
                period = f"{start} -> {end}" if start else end
                rows.append({
                    "unit": unit,
                    "val": f.get("val"),
                    "ptype": ptype,
                    "period": period,
                    "start": start,
                    "end": end,
                    "accn": f.get("accn", ""),
                    "form": f.get("form", ""),
                    "filed": f.get("filed", ""),
                    "fy": f.get("fy"),
                    "fp": f.get("fp"),
                    "frame": f.get("frame", ""),
                })
        if not rows:
            continue

        rows.sort(key=lambda r: (r["end"], r["start"] or "", r["filed"]))

        out.append(f"## us-gaap:{concept}")
        out.append("")
        out.append(f"*{label}*")
        if desc:
            out.append("")
            out.append("> " + desc.replace("\n", " "))
        out.append("")
        out.append("| Period type | Period | Value | Unit | Form | Filed | Accession | fy / fp | Frame |")
        out.append("|---|---|---:|---|---|---|---|---|---|")
        for r in rows:
            mark = " **(fetched)**" if r["accn"] in accn_of_interest else ""
            val = f"{r['val']:,}" if isinstance(r["val"], (int, float)) else str(r["val"])
            out.append(
                f"| {r['ptype']} | `{r['period']}` | {val} | {r['unit']} | {r['form']} | {r['filed']} | "
                f"`{r['accn']}`{mark} | {r['fy']} / {r['fp']} | {r['frame'] or '-'} |"
            )
        out.append("")

        # restatement / duplicate detection
        by_period: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            by_period.setdefault((r["unit"], r["period"]), []).append(r)
        conflicts = []
        repeats = []
        for (unit, period), group in sorted(by_period.items()):
            if len(group) < 2:
                continue
            vals = {g["val"] for g in group}
            if len(vals) > 1:
                conflicts.append((unit, period, group))
            else:
                repeats.append((unit, period, group))

        if conflicts:
            out.append("### :: SAME PERIOD, DIFFERENT VALUES ::")
            out.append("")
            out.append("The same concept and the same period, reported with more than one value. "
                       "Candidate explanations, none of which you should assume: a restatement, a "
                       "reclassification, a change in what the concept covers, the same figure disclosed "
                       "to a different precision, or two genuinely different measurements sharing a tag. "
                       "Work out which one it is before you accept either number - the reason is worth "
                       "more than the match.")
            out.append("")
            for unit, period, group in conflicts:
                out.append(f"- `{concept}` / {unit} / period `{period}`:")
                for g in sorted(group, key=lambda x: x["filed"]):
                    val = f"{g['val']:,}" if isinstance(g["val"], (int, float)) else g["val"]
                    out.append(f"  - **{val}** reported in {g['form']} `{g['accn']}` filed {g['filed']}")
                restatement_notes.append(f"{concept} / {unit} / {period}")
            out.append("")
        if repeats:
            out.append(f"*{len(repeats)} period(s) appear in more than one filing with the **same** value "
                       f"(original filing plus later comparatives). Not a restatement, but worth noticing: "
                       f"a naive 'get the fact' call has to pick one.*")
            out.append("")

    if restatement_notes:
        summary = ["## Conflicts found up front", "",
                   "These concept/period pairs carry more than one value across filings. "
                   "Each one is a teaching moment - go and find out why before you trust either number.", ""]
        summary += [f"- `{n}`" for n in restatement_notes]
        summary.append("")
    else:
        summary = ["## Conflicts found up front", "",
                   "None. No concept/period pair in the window below carries two different values.", ""]
    out[summary_slot:summary_slot] = summary

    out.append("---")
    out.append("")
    out.append("## Not debt Apple owes - debt securities Apple holds")
    out.append("")
    out.append("These us-gaap concepts also contain the word 'debt' and are cached under "
               "`data/raw/aapl/concept_<Name>.json`, but they describe Apple's investment portfolio "
               "(marketable securities), not its borrowings. Nothing here belongs in a long-term debt "
               "reconciliation:")
    out.append("")
    for concept in investment:
        n = sum(len(v) for v in usgaap.get(concept, {}).get("units", {}).values())
        out.append(f"- `us-gaap:{concept}` ({n} facts)")
    out.append("")
    out.append("Three concepts go the other way: `CommercialPaper`, `OtherShortTermBorrowings` and "
               "`ProceedsFromRepaymentsOfCommercialPaper` are borrowings with no 'debt' in the name at "
               "all. A substring search would have missed them, so they are pulled in explicitly.")
    out.append("")

    (ANSWER_KEY / "long_term_debt_facts.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"  wrote  data/ANSWER_KEY/long_term_debt_facts.md  "
          f"({len(obligation)} borrowing concepts tabulated, {len(investment)} investment concepts "
          f"listed, {len(restatement_notes)} conflicting period(s))")


# --------------------------------------------------------------------------
# derived: DATA_README.md
# --------------------------------------------------------------------------
def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def build_data_readme(selected: list[dict], manifest: dict, debt_concepts: list[str], submissions: dict):
    rows = []
    for rel, meta in sorted(manifest.items()):
        rows.append(
            f"| `{rel}` | {meta.get('note', '')} | {meta.get('url', '')} | "
            f"{human_bytes(meta.get('bytes', 0))} | {meta.get('fetched_at', '')} |"
        )
    total_bytes = sum(m.get("bytes", 0) for m in manifest.values())

    tenk = [s for s in selected if s["form"] == "10-K"]
    tenq = [s for s in selected if s["form"] == "10-Q"]
    first_read = None
    for s in tenk:
        if s["fiscal_year"] == 2024:
            first_read = f"data/reading/aapl-10k-fy2024.txt"
    if first_read is None and tenk:
        first_read = f"data/reading/aapl-10k-fy{tenk[0]['fiscal_year']}.txt"

    period_rows = []
    for s in selected:
        rec = s["record"]
        if s["form"] == "10-K":
            reading = f"data/reading/aapl-10k-fy{s['fiscal_year']}.txt"
            covers = (f"Fiscal year {s['fiscal_year']}: the twelve months ended {rec.get('reportDate')}. "
                      f"Balance sheet shows {rec.get('reportDate')} and the prior year end side by side.")
        else:
            reading = f"data/reading/aapl-10q-{s['label'].lower()}.txt"
            covers = (f"Fiscal quarter {s['label']}, ended {rec.get('reportDate')}. Balance sheet shows "
                      f"{rec.get('reportDate')} against the preceding fiscal year end.")
        period_rows.append(
            f"| {s['form']} | {s['label']} | {rec.get('reportDate')} | {rec.get('filingDate')} | "
            f"`{rec.get('accessionNumber')}` | `{reading}` | {covers} |"
        )

    obligation = [c for c in debt_concepts if not is_investment_concept(c)]
    investment = [c for c in debt_concepts if is_investment_concept(c)]
    concept_list = "\n".join(f"- `us-gaap:{c}`" for c in obligation) or "- (none found)"
    investment_list = chr(10).join(f"- `us-gaap:{c}`" for c in investment) or "- (none)"

    doc = f"""# Filing Desk - Phase 1 data

Everything here was pulled from SEC EDGAR by `scripts/fetch_edgar.py`. It exists to support one
exercise: **find Apple's long-term debt in a filing with your own eyes, write down the value, the
units and the as-of date, then pull the same figure out of XBRL and reconcile the two.**

Nothing in this directory tells you the answer. That is deliberate.

Fetched for {submissions.get('name', 'Apple Inc.')} - ticker {TICKER}, CIK {CIK} -
totalling {human_bytes(total_bytes)} across {len(manifest)} files.

---

## Start here

1. **Open `{first_read}` first.** It is the FY2024 10-K, converted to plain text with the tables
   kept in fixed-width columns. Everything else is either older, structured, or supporting.
2. Find long-term debt in it yourself. See *Where to look* below for the sections worth opening -
   not the number.
3. Write down four things before you look at anything else: the **value**, the **units**
   (thousands? millions? as-reported dollars?), the **as-of date**, and **where you found it**
   (use the `----- line N -----` markers so you can cite the location).
4. Only then open `data/ANSWER_KEY/long_term_debt_facts.md` and reconcile. If the two disagree,
   the reason is the point of the exercise - scaling, a different concept, the wrong period, or a
   restatement.
5. Repeat on `data/reading/{'aapl-10q-' + tenq[0]['label'].lower() + '.txt' if tenq else '<10-Q>'}`.
   A quarterly filing is where fiscal-period labelling gets awkward, which is exactly why it is here.
6. Then do the same for FY2023 and FY2022, and watch what happens to the figure across the three
   years. That comparison is the question the whole project exists to answer.

---

## What each folder holds

| Folder | Holds | Rule |
|---|---|---|
| `data/raw/` | The exact bytes of every HTTP response, as EDGAR sent them | **Never edited, never regenerated by hand.** If it is wrong, delete the file and re-fetch. Everything else is rebuildable from here; this is not. |
| `data/raw/aapl/filings/<form>_<period>_<accession>/` | One folder per filing: `index.json` (the file listing EDGAR publishes for that accession) plus the primary `.htm` document | Same rule. |
| `data/reading/` | Plain-text conversions of the primary documents, prepared for a human to read | Derived. Safe to delete; `python scripts/fetch_edgar.py --rebuild` recreates them from `raw/`. |
| `data/ANSWER_KEY/` | The XBRL side of the reconciliation | Derived. Do not open until you have read the filing yourself. |
| `scripts/` | `fetch_edgar.py` (fetch + cache + build) and `html_to_text.py` (the HTML-to-text converter) | Source. |

`raw/` is immutable because the whole project rests on being able to say *this number came from
these bytes*. The moment you hand-fix something in `raw/`, that sentence stops being true and every
downstream check becomes a check of your edits rather than of EDGAR's data.

`data/raw/_manifest.json` records what was fetched, from where, how big, and when.

---

## Where to look in the 10-K for long-term debt

Long-term debt shows up in at least three places in the same document, worded differently each time.
Check them in this order:

1. **The consolidated balance sheet** - inside *Item 8, Financial Statements and Supplementary Data*.
   Look under the liabilities section. Current liabilities come first, then non-current. Long-term
   debt sits in the non-current block; there is usually a separate current-portion line up in the
   current block. Note the column headings - there are two dates, and only one of them is the
   filing's own period end. Note also the scale statement at the top of the statement.
2. **The debt note in the financial statement footnotes** - still Item 8, a few pages after the
   statements. Apple titles it around *Debt* / *Term Debt*. This is where the number gets broken
   apart: commercial paper vs term debt, current vs non-current, carrying value vs fair value,
   maturities by year, fixed vs floating. The balance sheet gives you one number; this note tells
   you what is inside it.
3. **Item 7, Management's Discussion and Analysis** - the *Liquidity and Capital Resources*
   subsection. Narrative rather than a table, and it is where management says *why* the figure moved.
   You need this for the project's compass question, not for the figure itself.

Four traps worth knowing before you start:

- **The words on the page are not the words in the taxonomy.** Do not text-search for "long-term
  debt", find nothing, and conclude it is not there. Read the liabilities block line by line instead.
- **Carrying value is not principal.** The note may give you both. They will not match, and neither
  is wrong - they are answers to different questions.
- **Two dates per column pair.** Every statement shows the current period and a comparative. Half of
  the reconciliation failures in this exercise are reading the wrong column.
- **The same figure appears in more than one place** - the balance sheet line, the subtotal in the
  note, and sometimes a fair-value disclosure elsewhere. If two of them differ, that is information,
  not an error to explain away.

The `.txt` files under `data/reading/` are a conversion, not the filing. If a table looks wrong or a
number looks implausible, go and check the original: the raw `.htm` is under `data/raw/`, and the
source URL is in the header block at the top of every reading file.

---

## Apple's fiscal year

Apple's fiscal year ends on **the last Saturday of September**, so it is a 52/53-week year and the
end date moves every year. Practical consequences:

- FY2024 ended 2024-09-28. FY2023 ended 2023-09-30. FY2022 ended 2022-09-24. None of these is
  31 December, and none of them is the same date.
- A "FY2024" figure is therefore *not* a calendar-2024 figure, and comparing it to another
  company's calendar-year number is comparing two different windows.
- Quarters end in late December, late March and late June. There is no Q4 10-Q - the fourth quarter
  is folded into the 10-K.
- Every so often a 53-week year appears, so one quarter is 14 weeks long. Duration facts (revenue,
  cash flow) shift accordingly; instant facts (debt balances, cash) do not care.

### What each fetched filing covers

| Form | Period label | Period end | Filed | Accession | Reading copy | Covers |
|---|---|---|---|---|---|---|
{chr(10).join(period_rows)}

---

## Debt-related XBRL concepts Apple actually reports

Found by scanning `companyfacts.json` for us-gaap concept names containing "debt" - not guessed.
Each has its own `concept_<Name>.json` under `data/raw/aapl/`. These are **borrowings** - money
Apple owes:

{concept_list}

Which of these is *the* long-term debt figure on the balance sheet is part of the exercise. More
than one is plausible, and the balance sheet itself does not use the phrase "long-term debt" at all.

The same scan also drags in {len(investment)} concepts that are about debt securities Apple **holds** as
investments - the opposite of what you want, matched purely on the word:

{investment_list}

And three borrowing concepts contain no "debt" in the name at all, so a substring search misses
them. They are fetched explicitly: `CommercialPaper`, `OtherShortTermBorrowings`,
`ProceedsFromRepaymentsOfCommercialPaper`. Worth remembering when you build concept resolution in
Phase 2.

---

## Every file fetched

| File | What it is | Source URL | Size | Fetched (UTC) |
|---|---|---|---|---|
{chr(10).join(rows)}

---

## Re-running the fetcher

```
python scripts/fetch_edgar.py
```

Requirements: Python 3.9+ and a `.env` in the project root holding

```
SEC_CONTACT_EMAIL=you@example.com
SEC_USER_AGENT=FilingDesk/0.1 (you@example.com)
```

Copy `.env.example` and put a real address in it. EDGAR requires a descriptive `User-Agent` with a
contact email and will throttle or refuse requests without one. The script reads it from `.env`; it
is never hardcoded in a `.py` file, and `.env` should never be committed.

**A second run makes no network requests.** Every response is cached by its destination path under
`data/raw/`, and a file that already exists is never re-requested - the run reports `cache` on every
line instead of `GET`. That is the Phase 1 gate: pull your network cable out and run it again.

Two flags:

- `--offline` - refuse to touch the network at all. Anything not already cached is reported as a
  miss rather than fetched. Use it to prove the cache is complete.
- `--rebuild` - skip fetching entirely and just regenerate `data/reading/`, `data/ANSWER_KEY/` and
  this file from the bytes already in `data/raw/`.

The rate limiter sleeps {MIN_INTERVAL:.2f}s between requests (~{1 / MIN_INTERVAL:.1f} req/s), comfortably under
EDGAR's 10 req/s ceiling.

To force a genuine re-fetch of one thing, delete that file from `data/raw/` and run again. To
re-fetch everything, delete `data/raw/` - and be aware that is several hundred requests' worth of
someone else's bandwidth, so do not do it casually.
"""
    (ROOT / "DATA_README.md").write_text(doc, encoding="utf-8")
    print("  wrote  DATA_README.md")


if __name__ == "__main__":
    raise SystemExit(main())
