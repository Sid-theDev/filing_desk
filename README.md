# Filing Desk

Answers questions about SEC filings with cited, mechanically checked figures, using four small
tools and a hand-written agent loop. No vector database: the agent decides what to search for and
what to read.

> **How has Apple's long-term debt changed across FY2022–FY2024, and what reason does management give?**

Similarity search can't answer that question. The balance sheet never says "long-term debt" (it
says "term debt"), the MD&A calls the same borrowings "Notes", the figure is spread across three
filings, and the explanation lives in a different section from the number. You have to know where
to look and what each part of a filing calls things. That's a job for tools and a loop, not for
nearest-neighbour chunks.

## Two projects in one repo

1. **Project 1: the toolbox, as an MCP server.** Four tools over Apple's cached EDGAR filings,
   installable and usable from any MCP client (`serve_mcp.py`). Checkable in five minutes: connect
   a client and call `get_fact`.
2. **Project 2: the agent.** A ~250-line loop with no framework that answers questions no single
   document contains. It has limits, repeat-call memory, context compaction, mechanical citation
   checks, a 27-question eval set, and a logged experiment history, including the changes that made
   things worse.

## Architecture

```
  question
     │
  ask.py / evaluate.py
     │
  agent.py ───── call_model() ─────► OpenRouter ─► openai/gpt-5.6-luna
  loop · limits · repeat-call memory      (llm.py)
  compaction · answer checks · traces
     │
     │ run_tool(name, arguments) -> dict
     ├──── direct ─────────────────────────────────────────┐
     └──── --via-mcp ─► mcp_client.py ─stdio─► serve_mcp.py │
                                                (mcp_server.py: validate, dispatch, log)
                                                            ▼
                     tools.py:  list_filings · get_fact · search_filing · read_section
                                   │                                  │
                     facts.py (XBRL companyfacts)        filings.py (text lines, Item/Note map)
                                   └──────────── data/ (cached EDGAR) ─┘
```

## The four tools

| Tool | For | Returns (typical size) |
|---|---|---|
| `list_filings(ticker, form, fiscal_year_from, fiscal_year_to)` | which filings exist | ids, periods, dates (0.7 KB) |
| `get_fact(concept, period)` | a reported number, from XBRL | value, unit, period, reporting filing, `cite` (0.7 KB); a wrong concept name returns candidates |
| `search_filing(filing_id, query, section)` | *where* words appear | up to 8 line hits, 300-char snippets (0.4-2.7 KB) |
| `read_section(filing_id, section, start_line)` | reading text | up to 6,000 chars with line numbers, plus a cursor to continue (6.5 KB) |

Every result carries a `cite` string, e.g. `[aapl-10k-fy2024 L2011-2015]` or
`[xbrl LongTermDebtNoncurrent 2024-09-28]`. Errors come back as `{"error", "hint"}` with the options
to choose from, never as a traceback.

## Results

Model `openai/gpt-5.6-luna` · prompt v2 · 27 questions × 2 runs (the model can't be pinned to
temperature 0, so every configuration runs twice). From `eval/results/openai-gpt-5.6-luna__prompt-v2-run{1,2}.md`:

| Tier | Runs | Correct | Checks passed | Avg tool calls | Cost / question | Median latency |
|---|---:|---:|---:|---:|---:|---:|
| 1 · single fact | 16 | 16/16 | 16/16 | 3.6 | $0.0010 | 7.2s |
| 2 · single document, text | 14 | 14/14 | 14/14 | 3.5 | $0.0009 | 6.8s |
| 3 · multi-period / multi-filing | 12 | 12/12 | 12/12 | 8.4 | $0.0024 | 10.8s |
| 4 · traps (refuse or correct the premise) | 12 | 12/12 | 12/12 | 6.6 | $0.0018 | 9.3s |

- **Refusals:** 10/10 correct refusals (company outside the universe, period not filed, undisclosed
  facts). 0/44 refusals on answerable questions. The false premise "why did debt *increase*?" was
  corrected, not answered.
- **Cost:** $0.0015 per question on average, $0.0085 at worst; a full 27-question run costs about 4 cents.
- **Latency:** median 8.4s, p90 24.4s, max 40.9s.
- **Tool calls:** median 4, p90 12, max 30.

**Re-verified on the shipped code** (2026-09-12, `final-run1` / `final-run2`): 54/54 correct, 54/54 checks passed, $0.0749 for both runs, median 8.3s, p90 21.7s, no run hit a step, token or time limit, and compaction never fired (peak context 45,789 of 60,000 chars).

**Read these numbers with care.** The first version of prompt v1 scored 23/27 and 26/27. v2 adds one
rule written *after* reading those failures, so the set now partly measures that tuning. The set
is 27 questions; the plan calls for 50-100.

### A real trace

The target question, answered through the MCP server. Full output in [docs/demo.md](docs/demo.md)
(it contains the answer).

```
step 1: list_filings(form='10-K', fiscal_year_from=2022, fiscal_year_to=2024)   ->   653 chars
step 2: get_fact(LongTermDebtNoncurrent, FY2022 / FY2023 / FY2024)               ->   667 chars each
step 2: search_filing(aapl-10k-fy2024, 'term debt increase decrease reason', Item 7)  -> 450 chars (no hits)
 ...    12 more searches across Item 7, Item 8, Note 9: 7 of them miss because MD&A says "Notes"
step 4: read_section(aapl-10k-fy2024, Item 7, from line 1689)                    -> 6,471 chars
step 7: read_section(aapl-10k-fy2024, Item 8, from line 2048)                    -> 6,453 chars
Checks PASSED: 10 figures, 4 citations
openai/gpt-5.6-luna | prompt v2 | tools via mcp | 8 model calls | 19 tool calls | peak context 37,436 chars | $0.0055 | 27s
```

## What fails, and how often

From the prompt v1 baseline (27 questions, `EXPERIMENTS.md` experiment 2), which had 4 wrong answers:

| Failure | Count | Example |
|---|---:|---|
| **Incomplete answer** | 2 | Asked for the rate "at each year end" for FY2022-FY2024; gave only the first and last year. Fixed by prompt v2 (2/2 runs). |
| **Right data, wrong conclusion** | 1 | Reported the notes' *principal* due in 12 months as the *interest* due. The search snippet was cut off before the right figure and the agent never read the passage. The mechanical check passed, because the number was real. |
| **Ran out of budget** | 1 | Searched 15 times for a Moody's rating that isn't disclosed, then hit the step limit. |

Efficiency failures that still produce right answers:
- **Vocabulary misses:** 19% of searches returned nothing (5 of 27 in one run), mostly Item 7 queries using the balance sheet's words.
- **Skipped `get_fact`:** 2 of 7 XBRL-keyed questions read tables instead.
- **Expensive refusals:** the "not disclosed" trap took 11-19 tool calls before answering NOT FOUND.

## Experiments

One change at a time, each configuration run at least twice. Full log in [EXPERIMENTS.md](EXPERIMENTS.md).

| # | Change | Result | Verdict |
|---|---|---|---|
| 1 | Baseline, 13 questions | 13/13 | Set too easy: it couldn't tell a good change from a bad one |
| 2 | Harder set, 27 questions | 23/27 | New baseline; 3 checker false alarms found and fixed (`--regrade`, no model cost) |
| 3 | Prompt v2: "answer every part, give each year" | v1 23 & 26 → v2 27 & 27 | **Kept**; the targeted failure went 0/2 → 2/2 |
| 4 | Context compaction (v1 policy) on a long question | Compaction fired in 3 runs: all 3 wrong, 50-64 calls, 4-5× cost | **Made things worse**: it dropped results the model hadn't read yet, the model re-fetched, compaction fired again |
| 5 | Compaction v2: only already-read `read_section` text | 4/4 correct, 13-22 calls, no re-fetches | **Kept** |

## Setup

Requires Python 3.11+ (tested on 3.14).

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

- `OPENROUTER_API_KEY`: needed for `ask.py` and `evaluate.py`. Create one at https://openrouter.ai/keys.
- `FILING_DESK_MODEL`: optional. Default `openai/gpt-5.6-luna`; any OpenRouter model with tool
  calling works (DECISIONS.md #14 compares prices).
- `SEC_CONTACT_EMAIL` and `SEC_USER_AGENT`: needed only to re-fetch data. EDGAR refuses requests
  without a real contact email.

`data/` already holds the cached EDGAR files, so nothing touches EDGAR at question time. Rebuild the
reading copies from the raw files with `python scripts/fetch_edgar.py --rebuild`.

## Run

Offline tests (no network, no key; includes a real MCP client talking to the server):

```bash
.venv\Scripts\python -m pytest
```

Ask the target question, or any other:

```bash
.venv\Scripts\python ask.py
```

```bash
.venv\Scripts\python ask.py --via-mcp "How much commercial paper did Apple have at the end of FY2024?"
```

Regenerate the results table (about 4 cents):

```bash
.venv\Scripts\python evaluate.py
```

Other eval options: `--prompt v1|v2`, `--tag NAME` (save under its own name), `--regrade` (re-score
saved traces for free after changing the grader), `--questions eval/long_questions.json`,
`--compact-after CHARS`, `--models ...` / `--bake-off` (compare models).

## Use the MCP server from a client

Add this to your client's MCP config (e.g. Claude Desktop's `claude_desktop_config.json`), with
your own paths:

```json
{
  "mcpServers": {
    "filing-desk": {
      "command": "C:\\path\\to\\FINANCE-RAG-PIPELINE\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\FINANCE-RAG-PIPELINE\\serve_mcp.py"]
    }
  }
}
```

Every call is logged to `runs/mcp_calls.jsonl` with its arguments, result size and duration.

## How a run works

1. The question, the system prompt and the four tool schemas go to the model.
2. Tool calls are validated and run. Bad arguments, bad JSON and repeated calls come back as
   ordinary results the model can read, so a run never crashes on them.
3. The loop stops when the model answers in text, or at 15 model calls, 400K tokens or 300 seconds.
   A forced stop makes one more call with tools off, and the answer is labelled `PARTIAL ANSWER`.
4. Past 60K characters of context, `read_section` text the model has already read is replaced by a
   stub. Search hits and facts are never dropped.
5. The answer is checked. Every figure must appear in a tool result from this run (or be a sum,
   difference or % change of backed figures), and every citation must point at lines or facts the
   run actually returned.
6. The whole run is saved to `runs/<time>_<question>.json`: messages, tool calls with results,
   context size per call, tokens, cost, provider, stop reason and checks.

## Layout

```
filing_desk/
  config.py       paths, return budgets, agent limits, model
  filings.py      filings on disk: text lines and section map (Items, Notes)
  facts.py        XBRL facts: concept lookup, fiscal period labels
  tools.py        the four tools + their descriptions and JSON schemas
  checks.py       mechanical answer checks (figures and citations)
  prompts.py      system prompts, every version kept (v1, v2)
  llm.py          call_model() over OpenRouter
  agent.py        the agent loop, memory, compaction, traces
  mcp_server.py   the four tools over MCP
  mcp_client.py   a synchronous MCP client (used by ask.py --via-mcp and the tests)
ask.py            ask one question
evaluate.py       run / re-grade the eval set, write eval/results/*.md
serve_mcp.py      start the MCP server (stdio)
eval/             questions.json (27), long_questions.json (1), results/
docs/demo.md      a real run, pasted as printed
docs/INTERVIEW.md why the project exists, how it works, why these tests, likely questions
tests/            58 offline tests (tools, agent loop with a fake model, checks, MCP client ↔ server)
scripts/          Phase 1 EDGAR fetcher and HTML-to-text converter
data/             cached EDGAR data (raw) and plain-text reading copies
runs/             one JSON trace per run, plus the MCP call log (not committed)
DECISIONS.md      what was chosen, why, what was rejected
EXPERIMENTS.md    every experiment, including the ones that failed
```

## What's next, and what was left out on purpose

Next, in order of expected impact:
1. **A stop-searching rule.** Tier 4 traps take 7-19 calls to refuse; that's the biggest remaining cost.
2. **Partial matches on empty searches** (return lines holding the rarest query word) against the 19% miss rate.
3. **Steer figure questions to `get_fact`** in its description, instead of reading tables.
4. **Snippets that end at a sentence boundary**, which would have prevented the principal-vs-interest error.
5. **One citation per figure**, enforced by the checker.
6. **More companies** (the plan's 5-10) and **50-100 questions** written after the current tuning.

Left out deliberately:
- **No vector index.** The point is an agent that decides what to read, with every read visible in a trace.
- **No agent framework.** Stopping, error handling, memory and citation checks are the parts worth
  owning, and a framework hides them.
- **One company.** Section finding relies on "Item N." / "Note N –" headings and an Apple-specific
  fiscal calendar, both of which need work before a wider universe.

## Limits

- The figure check is a heuristic. A number can pass by coincidence, and a real number with the
  wrong label passes (see the principal-vs-interest failure). Only the grader caught that one.
- Compaction policy v2 has never had to fire on a real run (peak context stayed under 50K chars);
  its protective path is covered by unit tests.
- Results are from one model and small repeated samples; the model can't be pinned to temperature 0.
