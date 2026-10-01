# Demo: the target question, answered through the MCP server

A real run, pasted as printed. Nothing cleaned up. **This page contains the answer**, so if you are
doing the Phase 1 exercise by hand, read the filings first.

```bash
.venv\Scripts\python ask.py --via-mcp --prompt v2
```

The agent (`filing_desk/agent.py`) called its tools only through `serve_mcp.py`, over stdio, using
`filing_desk/mcp_client.py`. The server wrote one line per call to `runs/mcp_calls.jsonl`.

```
Question: How has Apple's long-term debt changed across FY2022-FY2024, and what reason does management give?

Connected to the MCP server. Tools: get_fact, list_filings, read_section, search_filing

  step 1: list_filings(ticker='AAPL', form='10-K', fiscal_year_from=2022, fiscal_year_to=2024) -> 653 chars
  step 2: get_fact(concept='LongTermDebtNoncurrent', period='FY2022', ticker='AAPL') -> 667 chars
  step 2: get_fact(concept='LongTermDebtNoncurrent', period='FY2023', ticker='AAPL') -> 667 chars
  step 2: get_fact(concept='LongTermDebtNoncurrent', period='FY2024', ticker='AAPL') -> 667 chars
  step 2: search_filing(filing_id='aapl-10k-fy2024', query='term debt increase decrease reason', section='Item 7') -> 450 chars
  step 3: search_filing(filing_id='aapl-10k-fy2024', query='term debt repayments', section='Item 7') -> 430 chars
  step 3: search_filing(filing_id='aapl-10k-fy2024', query='term debt issuances', section='Item 7') -> 429 chars
  step 3: search_filing(filing_id='aapl-10k-fy2024', query='debt decreased', section='Item 7') -> 1,582 chars
  step 3: search_filing(filing_id='aapl-10k-fy2024', query='term debt', section='Note 9') -> 1,999 chars
  step 4: read_section(filing_id='aapl-10k-fy2024', section='Item 7', start_line=1689) -> 6,471 chars
  step 5: search_filing(filing_id='aapl-10k-fy2024', query='repayment debt', section='Item 8') -> 1,940 chars
  step 5: search_filing(filing_id='aapl-10k-fy2024', query='repayments debt', section='Item 8') -> 1,941 chars
  step 5: search_filing(filing_id='aapl-10k-fy2024', query='maturities notes', section='Note 9') -> 2,649 chars
  step 5: search_filing(filing_id='aapl-10k-fy2024', query='issued notes', section='Note 9') -> 2,551 chars
  step 6: search_filing(filing_id='aapl-10k-fy2024', query='financing activities term debt', section='Item 7') -> 443 chars
  step 6: search_filing(filing_id='aapl-10k-fy2023', query='financing activities term debt', section='Item 7') -> 443 chars
  step 6: search_filing(filing_id='aapl-10k-fy2022', query='financing activities term debt', section='Item 7') -> 443 chars
  step 6: search_filing(filing_id='aapl-10k-fy2024', query='cash flows term debt', section='Item 7') -> 869 chars
  step 7: read_section(filing_id='aapl-10k-fy2024', section='Item 8', start_line=2048) -> 6,453 chars

Answer:
Apple’s **long-term debt, excluding current maturities**, declined each year:

| Fiscal year-end | Long-term debt | Change |
|---|---:|---:|
| FY2022 — Sep. 24, 2022 | **$98.959 billion** | — [xbrl LongTermDebtNoncurrent 2022-09-24] |
| FY2023 — Sep. 30, 2023 | **$95.281 billion** | **Down $3.678 billion** from FY2022 [xbrl LongTermDebtNoncurrent 2023-09-30] |
| FY2024 — Sep. 28, 2024 | **$85.750 billion** | **Down $9.531 billion** from FY2023 [xbrl LongTermDebtNoncurrent 2024-09-28] |

Management does not give a separate qualitative explanation for the decline; the filings show that **term-debt repayments exceeded new issuance**: FY2022 repayments were **$9.543 billion** versus **$5.465 billion** of issuance, FY2023 repayments were **$11.151 billion** versus **$5.228 billion** of issuance, and FY2024 repayments were **$9.958 billion** with **no term-debt issuance**. [aapl-10k-fy2024 L2061-2063]

Checks PASSED: 10 figures, 4 citations

Model openai/gpt-5.6-luna | prompt v2 | tools via mcp | stop: answered | model calls: 8 | tool calls: 19 | peak context: 37,436 chars | tokens: 46,717 in, 1,715 out | cost: $0.0055 | 26.89s
```

## Reading the trace

- **Steps are model calls; lines are tool calls.** Luna asks for several tools per step: 19 tool
  calls in 8 model calls.
- **Figures come from XBRL** (`get_fact`, cited `[xbrl ...]`); the repayment and issuance figures come
  from the cash flow statement text (cited by line). The mechanical check confirmed all 10 figures
  appear in this run's tool results, including the two year-over-year differences it computed.
- **The empty searches** (about 430-450 chars: no hits, only a hint) are Item 7 queries for "term debt"
  and "repayments". Apple's MD&A calls the debt "Notes", so those words never appear there. This is
  the vocabulary-mismatch failure counted in `EXPERIMENTS.md`.
- **The honest "no reason given"**: the agent read Item 7, found no stated reason, said so, and
  described what the filings show instead. That's the behaviour the prompt requires.
- One weakness is visible: the repayment/issuance sentence carries a single citation for six
  figures, and it points at the cash flow line only. The checker accepts it (the numbers are in that
  result), but a stricter citation rule would require one citation per figure.
