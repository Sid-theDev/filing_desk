# Experiment log

One change at a time. Re-run the whole set with `python evaluate.py`, then record before and after.
Keep the experiments that made things worse.

| # | Date | Change | Correct (by tier) | Checks passed | Avg tool calls | Cost / question | Verdict |
|---|---|---|---|---|---|---|---|
| 0 | — | Model bake-off (`evaluate.py --bake-off`) | not run | | | | Skipped: Luna chosen by preference (DECISIONS #14b) |
| 1 | 2026-09-10 | Baseline: `openai/gpt-5.6-luna`, prompt v1, budgets as in DECISIONS.md | T1 4/4 · T2 3/3 · T3 3/3 · T4 3/3 | 13/13 | 4.5 (max 18) | $0.0014 | Baseline. A perfect score means the set is too easy (see notes) |
| 2 | 2026-09-10 | Harder set: 27 questions (DECISIONS #22), same model, prompt and budgets | T1 8/8 · T2 6/7 · T3 4/6 · T4 5/6 | 24/27 first run; 27/27 after checker fix #19b (`--regrade`) | 5.4 (max 21) | $0.0014 | **New baseline: 23/27.** The set now discriminates |
| 3 | 2026-09-10 | Prompt v2: + "answer every part; give each year" (DECISIONS #23-24). 2 runs per arm | v1: 23/27, 26/27 · v2: **27/27, 27/27** | 27/27 in all 4 runs | v1 5.0 / 5.3 · v2 5.6 / 5.0 | v1 $0.0014 · v2 $0.0015 / $0.0014 | **Kept, v2 is the default.** Targeted failure fixed 2/2 at the same cost |
| 4 | 2026-09-10 | Compaction policy v1 on the long question: off ×2, 25K ×2, 12K ×2 | off 2/2 · 25K 1/2 · 12K 0/2 | 6/6 | off 17-19 · compacted runs 50-64 | off $0.0051 · compacted $0.022-0.027 | **Made things worse.** Compaction thrashed; design fixed (DECISIONS #20b) |
| 5 | 2026-09-10 | Compaction policy v2 (only `read_section` text, only if already seen): 25K ×2, 12K ×2 | 4/4 | 4/4 | 13-22 | $0.0030-0.0057 | **Kept.** No thrash; never needed to fire on this question |
| 6 | 2026-09-12 | Final verification of the shipped configuration (prompt v2, compaction v2, 60K ceiling): 27 questions ×2 | T1 16/16 · T2 14/14 · T3 12/12 · T4 12/12 | 54/54 | 5.2 (max 23) | $0.0014 | **54/54, no budget stops, 0 compactions/refetches** |

## Notes

Write what surprised you here, with the trace file names from `runs/`.

### 2026-09-10 · first live run, target question, `openai/gpt-5.6-luna`

Trace: `runs/20260910-122609_how-has-apple-s-long-term-debt-changed-across-fy.json`

- Answered with checks passed (7 figures, 7 citations). 9 model calls, 24 tool calls,
  55.5K prompt + 2.0K completion tokens, **$0.0062**, 29s. OpenRouter routed every call to OpenAI.
  That's under half the $0.015 estimate, thanks to OpenAI's cheaper "flex" tier and cached input.
- Peak context was 36K characters (11.6K prompt tokens), so compaction never triggered.
- Luna fires tool calls in parallel: up to 6 in one step.
- **7 of 14 searches returned zero hits**, all restricted to Item 7 using words like "term debt"
  and "repayments". Apple's MD&A calls the borrowings "Notes" and "Debt"; "term debt" and "repayments"
  appear in Note 9 and the cash flow statement instead. The agent recovered by searching "debt
  maturities". This is the vocabulary-mismatch failure the plan predicts.
- Candidate experiment: when a multi-word search finds nothing, return the best partial matches
  (lines holding the rarest query word) instead of an empty list. Measure tool calls per question
  before and after.

### 2026-09-10 · experiment 1, baseline eval, `openai/gpt-5.6-luna`

Results: `eval/results/openai-gpt-5.6-luna.md`. 13/13 correct, 13/13 checks passed, $0.0176 total,
median 6.5s, max 32.5s.

**A perfect score is not good news.** It means the set cannot tell a good change from a bad one.
Before any experiment is worth running, the set needs harder questions: more narrative Tier 2
and Tier 3, more traps, and the 50-100 the plan asks for.

The failures are hiding behind correct answers. Counts across the 13 traces:

1. **Figures read from text instead of `get_fact`: 2 of 7 XBRL-keyed questions**
   (`t1-cp-q3fy2026`, `t1-term-debt-repaid-fy2024`). Both were right, but they rest on table reading,
   which the prompt says to avoid. This is a wrong-tool problem (description / prompt).
   The repaid question took 6 calls where one `get_fact(RepaymentsOfLongTermDebt)` would do.
2. **Empty searches: 5 of 27 (19%).** Vocabulary mismatch: the question's words are not the
   filing's words, especially inside Item 7. This is a tool-return problem (no partial matches).
3. **Expensive refusals: `t4-not-disclosed` took 13 tool calls and 73K tokens** before answering
   NOT FOUND, 27% of the whole eval's cost. The refusal was correct, but there is no policy for when
   to stop looking.

Also noted:
- `t4-other-company` refused with zero tool calls, relying on the prompt's universe statement
  rather than checking. This is acceptable, but it's not "say what you checked".
- The target question took 18 tool calls here vs 24 in the first run: same model, same prompt.
  Luna can't run at temperature 0, so each question should be run several times before
  comparing experiments.
- Peak context was 37K characters and compaction never fired. The Phase 5 gate is **not met**:
  no question yet needs 8+ calls *and* crosses the 60K ceiling.
- Duplicate calls caught: 0.

Next experiments, one at a time:
(a) steer toward `get_fact` in its description, then re-count pattern 1;
(b) partial matches on empty search, then re-count pattern 2 and average calls;
(c) a search-budget line in the prompt ("after ~5 fruitless searches, answer NOT FOUND"),
then re-measure tool calls on Tier 4.

### 2026-09-10 · experiment 2, harder 27-question set, `openai/gpt-5.6-luna`

Results: `eval/results/openai-gpt-5.6-luna.md` (re-graded with checker fix #19b, same traces).
**23/27 correct**, 27/27 checks passed, $0.0385 total ($0.0014 per question), median 8.1s, max 48.9s.
No answerable question was refused. The false premise ("why did debt increase?") was corrected.

**Checker false alarms before the fix: 3.** Found by reading the traces, not by the score:
- `t1-total-term-debt-fy2023`: "total = current + non-current". The checker knew differences, not sums.
- `t3-repayments-fy2022-fy2024` and `t3-notes-principal-fy2022-fy2024`: year-over-year differences of
  numbers read from text. The checker only derived from `get_fact` values.
Fixed in `checks.py` (sum / difference / % change of figures the answer already backed). Re-scored
with `evaluate.py --regrade` at zero cost, so the fix is measured on the same answers.

**Failure taxonomy, 4 wrong answers** (the plan's categories):

| Cause | Count | Questions | What happened |
|---|---:|---|---|
| Right data, wrong conclusion | 1 | `t2-future-interest-fy2023` | Reported "$9.9 billion payable within 12 months" as interest. That's the notes' *principal*. The search snippet was cut at 300 chars, probably just before "$2.9 billion", and the agent never read the passage. **Checks passed**: the number is real, just mislabelled. |
| Incomplete answer | 2 | `t3-cp-rate-fy2022-fy2024`, `t3-notes-principal-fy2022-fy2024` | Both questions ask for "each year". Both answers give only the first and last year and skip FY2023, which the agent had searched in one case. |
| Ran out of budget | 1 | `t4-credit-rating` | 15 model calls of searching for a rating that isn't there, then `step_limit`. The forced answer said the rating isn't stated but didn't say NOT FOUND. 49s, the slowest question. |

Wrong tool chosen: 0 answers wrong. But `get_fact` was still skipped on text-answerable figures
(`t3-repayments` used 13 searches where 3 `get_fact` calls would do).

Next experiments, in order of wrong answers fixed per change:
(d) prompt v2: "answer every part; when a question asks for each year, give each year" → targets 2;
(e) search snippets end at a sentence boundary, or the prompt requires `read_section` before quoting a
figure from a snippet → targets 1;
(f) (c) above, a stop-searching rule → targets 1 plus the cost of Tier 4.
Run each at least twice: Luna can't run at temperature 0, and the target question has already
varied from 18 to 24 to 21 tool calls across three runs.

### 2026-09-10 · experiment 3, prompt v2 ("answer every part")

One change: a single added rule in the system prompt (`prompts.py`, v2 = v1 + 1 line). Everything
else identical. Four runs of the 27 questions in parallel:
`eval/results/openai-gpt-5.6-luna__prompt-v{1,2}-run{1,2}.md`.

Per question, only the questions that were ever wrong:

| Question | v1 run 1 | v1 run 2 | v2 run 1 | v2 run 2 |
|---|---|---|---|---|
| `t3-cp-rate-fy2022-fy2024` (**targeted**) | wrong | wrong | right | right |
| `t2-future-interest-fy2023` | wrong | right | right | right |
| `t3-notes-principal-fy2022-fy2024` (targeted) | wrong | right | right | right |
| `t4-credit-rating` | wrong (step limit) | right | right | right |

Totals: v1 49/54, v2 54/54. Cost per question $0.0014-0.0015 for both. Avg tool calls unchanged
(v1 5.0-5.3, v2 5.0-5.6).

**What this does and doesn't show:**
- The targeted failure went from 0/2 to 2/2. That's the only difference clear enough to credit to
  the rule.
- The other three questions were wrong once and right once under v1. That's run-to-run noise, and
  v2 got lucky on them as much as it fixed them. **One run of v1 alone would have given a different
  story (23 vs 26)**, which is why every arm runs twice.
- The rule was written after reading the failures, so this set now partly measures tuning. New
  failure types need questions written after this change.
- Cost of tool-call variance: the target question took **19 to 30** tool calls across the runs, and
  the "not disclosed" trap 11 to 19. Stopping policy is the largest remaining inefficiency.

### 2026-09-10 · Phase 3 gate, target question through the MCP server

`python ask.py --via-mcp --prompt v2`. The agent's tools were called only through `serve_mcp.py` over
stdio (`filing_desk/mcp_client.py`). Correct answer, checks passed (10 figures, 4 citations), 8 model
calls, 19 tool calls, peak context 37,436 chars, $0.0055, 27s.
Trace: `runs/20260910-135313_how-has-apple-s-long-term-debt-changed-across-fy.json`; server-side log:
the matching 19 entries in `runs/mcp_calls.jsonl` (08:22:49-08:23:06 UTC), one per agent call, with
arguments, result size (429-6,471 chars) and duration (0.2-41 ms), no errors.
A clean-install check passed too: the repo copied without `.venv`, `.env` or `runs/`, set up with only
the README commands, and ran the full test suite.

### 2026-09-10 · Phase 2 and 4 gates: a hand call sequence vs the agent's

**The hand sequence** (run directly against `tools.py`; Claude's stand-in, so redo it yourself):

| # | Call | Result | Why |
|---|---|---:|---|
| 1 | `list_filings(form="10-K", fiscal_year_from=2022, fiscal_year_to=2024)` | 653 chars | ids for the three years |
| 2 | `get_fact(concept="long-term debt")` | 926 chars, error | the name is wrong on purpose: returns candidates `LongTermDebt`, `LongTermDebtCurrent`, `LongTermDebtNoncurrent` |
| 3-5 | `get_fact("LongTermDebtNoncurrent", FY2022 / FY2023 / FY2024)` | 667 chars each | the balance-sheet figure, three years |
| 6-8 | `get_fact("RepaymentsOfLongTermDebt", FY2022 / FY2023 / FY2024)` | ~680 chars each | what drove the change |
| 9-11 | `get_fact("ProceedsFromIssuanceOfLongTermDebt", FY2022 / FY2023 / FY2024)` | ~700 chars each | new issuance, the other side |
| 12 | `search_filing("aapl-10k-fy2024", "Notes principal payable", section="Item 7")` | 1,012 chars | MD&A's own words are "Notes", not "term debt" |
| 13 | `read_section("aapl-10k-fy2024", start_line=1686)` | 6,325 chars | read the liquidity passage to check for a stated reason |

About 13 calls and ~15 KB. Finding: calling `get_fact` **without** a period for a flow concept
returns the 12 most recent rows, mixing 3-, 6-, 9- and 12-month durations, and never reaches FY2022.
For flows, always pass a period. That's a return-shape weakness to fix.

**The agent's sequence** (MCP run above): 19 calls, ~33 KB. Every difference explained:
1. **It skipped `get_fact` for repayments and issuance.** It read the cash flow statement instead:
   2 searches plus 1 `read_section` of Item 8. Same numbers, but table reading instead of XBRL.
   This is the "wrong tool" pattern from experiment 1.
2. **Vocabulary misses:** 7 of 12 searches returned 450 chars or less (no or weak hits). It
   searched Item 7 for "term debt", "repayments", "issuances", "financing activities", words MD&A
   doesn't use. The hand sequence searched "Notes", because a human had read the section.
3. **Fan-out:** it issued 3-5 calls per step in parallel, including the same search on all three
   filings. That costs calls but not steps: 8 model calls vs 13 for the one-call-at-a-time hand run.
4. **It read Item 7 once and found no stated reason**, the same conclusion as the hand run.

Both reach the same answer. The agent spends ~6 extra calls on vocabulary misses and table reading.
Experiments (a) (`get_fact` steering) and (b) (partial matches) target exactly those.

### 2026-09-10 · experiment 4, compaction on a long question: **made things worse**

Question: `eval/long_questions.json` (3 fiscal years × commercial paper rate, notes principal, amount
due in 12 months, future interest: 12 figures from 3 filings). Prompt v2. Only the compaction
threshold changes (`--compact-after`).

| Run | Ceiling | Correct | Tool calls | Model calls | Peak context sent | Compactions | Refetches | Prompt tokens | Cost | Time |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| off run 1 | none | yes | 19 | 5 | 38,114 | 0 | 0 | 29,636 | $0.0051 | 22s |
| off run 2 | none | yes | 17 | 4 | 47,968 | 0 | 0 | 22,848 | $0.0051 | 18s |
| 25K run 1 | 25,000 | yes | 13 | 4 | 19,926 | **0** (never crossed) | 0 | 15,226 | $0.0030 | 16s |
| 25K run 2 | 25,000 | **no** | **51** | **15 (step limit)** | 32,234 | **12** | **11** | 111,305 | **$0.0236** | 47s |
| 12K run 1 | 12,000 | **no** (0 of 12 figures) | **50** | **15 (step limit)** | 40,762 | **12** | 0 | 105,803 | **$0.0216** | 41s |
| 12K run 2 | 12,000 | **no** (missing 2.31%) | **64** | **15 (step limit)** | 42,940 | **13** | 4 | 119,943 | **$0.0265** | 61s |

**Three of the four runs where compaction actually fired were wrong, all at the step limit,
4-5× the cost of no compaction.** The one 25K run that passed never compacted.

The refetch counter understates the damage: 12K run 1 shows 0 refetches but 43 searches. After its
results were stubbed, the model searched again with *different* words, which an identical-signature
counter can't see. Re-looking needs a looser measure, such as repeat searches of the same filing
and section.

Run 25K-1 never crossed its ceiling; it just happened to need fewer calls, so it tells us nothing
about compaction. Run 25K-2 is the real test, and compaction **broke the answer and cost 4.6×**:
- Compaction 1 fired before the 4th model call and dropped 9 results, some from a batch the model
  had **not yet read** (it had issued 8 calls in one step; the rule kept only the newest 4).
- The model re-ran those searches (11 refetches). The context refilled past 25K, compaction fired
  again, and it did so 12 times in all. Context per call: 1.8K, 2.7K, 12.8K, 20.6K, 15.9K, 20.7K, ... 32.2K.
- It hit the 15-call step limit. The forced answer was honest: `PARTIAL ANSWER`, with FY2023's three
  figures marked "Not recovered in the available results". The checks passed, because nothing was
  invented; the grader caught the missing values.
- 49 of its 51 calls were `search_filing`. Search hits are small, so dropping them saved almost
  nothing and destroyed the figures the model had located.

**Why (the design flaw):** "keep the newest 4 results" assumed one tool call per step. With
parallel fan-out it compacts unseen results. And stubbing search hits drops established facts, which
the plan explicitly forbids ("compaction never drops an established fact or its citation").

**Change made → compaction policy v2** (DECISIONS #20b): compact only `read_section` text, and only
results the model has already been sent. Re-tested below.

### 2026-09-10 · experiment 5, compaction policy v2 on the same long question

Same question, prompt and ceilings as experiment 4. Only the compaction policy changed.

| Run | Ceiling | Correct | Tool calls | Model calls | Peak context sent | Compactions | Refetches | Prompt tokens | Cost | Time |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2 25K run 1 | 25,000 | yes | 13 | 4 | 21,670 | 0 | 0 | 15,568 | $0.0032 | 13s |
| v2 25K run 2 | 25,000 | yes | 22 | 5 | 49,487 | 0 | 0 | 29,673 | $0.0057 | 18s |
| v2 12K run 1 | 12,000 | yes | 13 | 4 | 19,418 | 0 | 0 | 14,142 | $0.0030 | 14s |
| v2 12K run 2 | 12,000 | yes | 13 | 4 | 22,933 | 0 | 0 | 16,078 | $0.0036 | 17s |

**Before and after, where compaction was allowed to fire:**

| Policy | Runs | Correct | Tool calls | Step-limit stops | Cost per run |
|---|---:|---:|---:|---:|---:|
| No compaction | 2 | 2/2 | 17-19 | 0 | $0.0051 |
| Policy v1 (count-based, drops search hits) | 4 | 1/4 | 13-64 | 3 | $0.0030-0.0265 |
| Policy v2 (read text only, only once seen) | 4 | 4/4 | 13-22 | 0 | $0.0030-0.0057 |

**What this shows, honestly:**
- v2 removed the thrash: no refetches, no step-limit stops, and correct every time.
- **v2 never compacted anything.** On this question Luna mostly searches; the few `read_section`
  results arrived in the last step before answering, so none were ever "already seen" when a check
  ran. The mechanism is covered by unit tests (`test_compaction_drops_old_text_but_keeps_facts`,
  `test_results_the_model_has_not_seen_are_never_compacted`), not by a live run that needed it.
- Peak context sent: 19-49K chars on the longest question, and 37-48K on the target question,
  **under the 60K ceiling in every run in this project**. With this model's small tool returns and
  parallel fan-out, the context budget is not the binding constraint. Tool calls and search misses
  are.

**Phase 5 gate** ("8+ call question: zero duplicate fetches, peak under ceiling, still correct, with
before/after numbers"): met at the stated 60K ceiling, with 13-22 tool calls, 0 duplicates, 0
refetches, peak ≤ 49,487 chars, correct 4/4, and the before/after tables above. Caveat: the protective
path of v2 has not yet been exercised live; that needs a question that reads several whole sections
early and keeps working afterwards.
