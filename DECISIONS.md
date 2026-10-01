# Decisions log

What was chosen, why, and what was rejected. Add to the bottom; don't rewrite history.

## Phase 1: data

**1. Universe.** Apple Inc. only: the FY2022, FY2023 and FY2024 10-Ks and the Q3FY2026 10-Q have text;
XBRL facts cover every period Apple has reported. *Why:* this data was already fetched and cached,
and one company keeps the first build small. *Rejected for now:* the plan's 5-10 companies, which
is the obvious next step.

**2. Filing identifier.** `filing_id` is the reading-file name without `.txt`, e.g. `aapl-10k-fy2024`.
Every tool speaks it. *Why:* readable by a model and a human, and it maps 1:1 to a file.
*Rejected:* accession numbers (opaque, easy for a model to mistype).

**3. Fiscal convention.** Periods are labelled the way Apple reports them: `FY2024` (ended 2024-09-28),
`Q3FY2026` (ended 2026-06-27). Labels are derived from `reportDate` in `submissions.json`.
*Limit:* the label rule in `facts.fiscal_label` assumes a late-September year end, so it is Apple-only.

**4. Cache policy.** Tools never touch the network. `scripts/fetch_edgar.py` owns fetching; everything
at question time reads `data/`. The Phase 1 gate (runs offline) therefore holds for the whole system.

**5. Line numbers.** A line number is a body line number, the same number printed in the
`----- line N -----` markers of the reading files, so a human can find any citation by eye.
Tested in `test_line_numbers_agree_with_markers`.

## Phase 2: tools

**6. Return budgets** (measured on the FY2024 10-K):

| Tool | Typical | Worst case | Cap |
|---|---|---|---|
| `list_filings` | 0.8 KB | 20 rows | `MAX_FILINGS = 20` |
| `get_fact` with period | 0.7 KB | ~1.5 KB | one row per matching period |
| `get_fact` no period | 3.9 KB | ~4 KB | `MAX_FACT_ROWS = 12` |
| `get_fact` bad name | 0.9 KB | ~1 KB | `MAX_CANDIDATES = 8` |
| `search_filing` | 2.7 KB | ~3.5 KB | 8 hits x 300 chars |
| `read_section` | 6.5 KB | ~6.5 KB | `MAX_READ_CHARS = 6000` of text |
| `read_section` table of sections | 3.5 KB | ~3.5 KB | one row per section |

*Why 6,000 characters:* about 1,500 tokens. The debt note fits in one or two calls; Item 8 (about
1,270 lines) takes many, which is intended. The agent should search, then read, not read everything.

**7. Truncation protocol.** A truncated read returns `truncated: true`, `next_start_line`, and a hint
sentence saying exactly what to call next. Nothing is ever cut silently.

**8. Concept resolution: candidates, not synonyms.** `get_fact` accepts exact concept names only.
A wrong name returns up to 8 candidates with labels, ranked by shared words. *Why:* a synonym map
would silently choose between `LongTermDebt` and `LongTermDebtNoncurrent`, and that choice is the
whole Phase 1 lesson. *Rejected:* a synonym table.

**9. Restatement policy.** When several filings report the same period, `get_fact` returns the value
from the earliest filing (the original) and lists any different later values in `other_values`,
with `times_reported`. Example: `LongTermDebt` at 2024-09-28 is reported to the million in the
10-K and rounded to the hundred million in later 10-Qs.

**10. Search is lexical.** Words match at word starts, scored over 3-line windows: distinct query
words found, plus a bonus for the exact phrase. At least half the words must appear. No index,
no embeddings. *Why:* explainable, and the agent deciding what to read is the mechanism being built.

**11. One citation shape.** Every result carries a ready-made `cite`: `[aapl-10k-fy2024 L2011-2015]`
for text, `[xbrl LongTermDebtNoncurrent 2024-09-28]` for facts. The model copies it; the checker
parses it.

**12. One error shape.** `{"error": ..., "hint": ...}` plus `candidates`, `available` or `options`
when there is something to choose from. `run_tool` never raises, so a tool bug becomes a message
the model can route around.

## Phase 3: MCP server

**13. Thin server.** `filing_desk/mcp_server.py` only declares parameters, calls `tools.run_tool`,
and logs each call to `runs/mcp_calls.jsonl`. Descriptions are the same strings the agent sees.
Uses `mcp` 2.x (`MCPServer`, formerly `FastMCP`).

## Phase 4: agent

**14. Model.** Default `qwen/qwen3.7-flash`, chosen on price after a live scan of OpenRouter's model
list on 2026-09-10 (265 paid models with tool calling, `:batch` variants excluded because the agent
needs synchronous calls). Headline prices are misleading for an agent, because every call re-sends
the growing conversation. So each model was costed on a simulated run: 9 calls, context growing
6K to 38K tokens, 450 output tokens per call, cache-read discounts and prompt-size price tiers
applied.

| Model | $/M in | $/M out | Est. per question |
|---|---:|---:|---:|
| `inclusionai/ling-3.0-flash` | 0.021 | 0.063 | $0.0017 |
| `qwen/qwen3.7-flash` (tiered: $0.10/$0.40 above 32K prompt tokens) | 0.030 | 0.130 | $0.0042 |
| `openai/gpt-5-nano` | 0.050 | 0.400 | $0.0042 |
| `deepseek/deepseek-v4-flash-0731` | 0.065 | 0.180 | $0.0056 |
| `deepseek/deepseek-v4-flash` (first pick, replaced) | 0.089 | 0.177 | $0.0067 |
| `openai/gpt-5.6-luna` | 0.200 | 1.200 | $0.0150 |
| `google/gemini-3.1-flash-lite` | 0.250 | 1.500 | $0.0188 |
| `anthropic/claude-haiku-4.5` | 1.000 | 5.000 | $0.0710 |
| `x-ai/grok-4.6` | 2.000 | 6.000 | $0.1749 |

*Caveats:* estimates exclude reasoning tokens, which bill as output on reasoning models (gpt-5-nano,
Luna). Real cost is OpenRouter's `usage.cost`, recorded in every trace. Price alone does not pick
the final default: `python evaluate.py --bake-off` runs the five models in `config.BAKE_OFF_MODELS`
on the same questions, and the winner is the best accuracy per dollar. Requests ask OpenRouter for
the cheapest provider that supports every parameter sent (`provider.sort = price`,
`require_parameters = true`); the provider that served each call is logged in the trace.
Temperature 0. Switch models with `FILING_DESK_MODEL` in `.env`.

**14b. Default switched to `openai/gpt-5.6-luna`** (same day). Chosen for answer quality and tone,
accepting about 3-4x the estimated cost of the cheapest models (~$0.015 per question, ~$0.20 per
13-question eval run, before reasoning tokens). The cheaper models stay in `BAKE_OFF_MODELS`, so the
trade can be measured rather than assumed.

**14c. Temperature only where accepted.** None of Luna's providers accept `temperature` (it is a
reasoning model), and `require_parameters` would then match no provider at all. `llm.py` looks up
each model's supported parameters once from OpenRouter and sends `temperature = 0` only when it is
accepted. Luna runs therefore use the provider's default sampling, so run-to-run variation is
expected and repeated runs matter more when comparing experiments.

**15. No framework, no SDK.** The loop is `filing_desk/agent.py`; HTTP is `urllib` in `llm.py`.

**16. Stop conditions.** 15 model calls, 400K total tokens, 300 seconds, each logged as a distinct
`stop_reason`. A forced stop makes one last call with tools switched off and the answer is
prefixed `PARTIAL ANSWER:`.

**17. Repeat calls get a pointer.** An identical call (same tool, same arguments) is not run again;
the result says which step already has it. *Why:* the earlier result is still in context and
re-serving it doubles the context. *Exception:* once that result has been compacted, a repeat
runs for real.

**18. System prompt v1** (`filing_desk/prompts.py`): numbers only from tool results, one citation
per figure, `NOT FOUND:` instead of a guess, and "say so" when no reason is given.

**19. Mechanical answer checks** (`filing_desk/checks.py`), run after every answer:
every amount or percentage must appear in a tool result (with rounding and scale allowed) or be a
difference or percent change between two `get_fact` values; every citation must point at lines
or facts this run returned. *Known weakness:* a figure can pass by coincidence when the same digits
appear elsewhere in text the run read.

**19b. Derived figures** (after the 27-question baseline raised three false alarms). A figure also
passes when it is a sum, difference or percent change of two figures *the same answer* already
backed with evidence, e.g. "total $X" after stating current and non-current with citations.
Deriving from any two numbers in the text was rejected: with hundreds of numbers in a read section,
some pair matches almost anything. *Known blind spot, seen in `t2-future-interest-fy2023`:* a real
number attached to the wrong label (principal reported as interest) passes, because the number
itself is backed. Only the grader caught that one. `evaluate.py --regrade` re-scores saved traces
after a checker change, so checker fixes are measured without new model runs.

## Phase 5: memory

**20. Compaction.** When the conversation passes 60,000 characters, old `read_section` and
`search_filing` results (all but the newest 4 tool results) become one-line stubs. `get_fact` and
`list_filings` results are never dropped. The answer checks use the full results.

**20b. Compaction policy v2** (after experiment 4 made answers worse). Two rules change:
1. **Only compact what the model has already read.** Compaction runs just before a model call, and
   Luna issues 5-9 tool calls per step, so "keep the newest 4 results" dropped results the model had
   *never seen*. It then asked again, the context refilled, and compaction fired again: 12
   compactions, 11 refetches, step limit, wrong answer. Results from the previous step are now
   always kept.
2. **Only compact `read_section` text.** Search hits are ~300-char snippets that carry the
   located figure and its citation; dropping them is dropping established facts, which the plan
   forbids. `KEEP_RECENT_RESULTS` is gone.
Measured: peak context without compaction on the longest question was 38-48K chars, under the
60K ceiling, so the default ceiling rarely fires with this model. Re-tested in experiment 5 at 25K
and 12K: 4/4 correct, 0 refetches, no step-limit stops. It never needed to compact on that question,
so its protective path is verified by unit tests rather than a live run.

## Phase 6: evaluation

**21. Answer keys.** Numeric keys come from XBRL through `get_fact` at grading time, so no number
is typed by hand. Text questions are graded by required words or figures. 13 questions for now
(4 single-fact, 3 single-document text, 3 multi-period, 3 traps). The plan asks for 50-100.

**22. Harder question set: 27 questions** (8 single-fact, 7 single-document text, 6 multi-period /
multi-filing, 6 traps). The first 13 were all answered correctly, so they could not tell a good
change from a bad one. Additions: less obvious XBRL concepts (total carrying amount, principal due
in 12 months, 9-month flows), figures that live only in text (rate sensitivity in Item 7A, hedged
term debt, future interest payments), three-year trends that need several filings, and new traps:
a false premise ("why did debt increase?", answered by correcting it), an unfiled quarter, and an
undisclosed credit rating. Every text key was checked against the reading copies before use.
Refusal statistics now follow each question's `expect.refusal`, not its tier.

**23. Prompt versions live side by side.** `prompts.py` keeps every version unchanged in `PROMPTS`;
`run_agent(prompt_version=...)` and `evaluate.py --prompt` pick one, and every trace and results row
records it. The default moves only after an experiment shows the newer version is better. Luna
can't run at temperature 0, so each variant is run **twice** and `--tag` saves each run under its
own name (`eval/results/<model>__<tag>.md`). Trace files are created exclusively, so parallel eval
runs never overwrite each other.

**24. Default prompt is now v2.** Experiment 3: v1 scored 23/27 and 26/27, v2 scored 27/27 twice, at
the same cost. The question the new rule targets (`t3-cp-rate-fy2022-fy2024`, "give each year")
was wrong in both v1 runs and right in both v2 runs. *Caveat:* two runs per arm is a small sample,
and the rule was written after seeing the failures, so the set now partly measures that tuning.
New failure types should be judged on questions written after this change.
