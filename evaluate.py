"""Run the eval question set and write the results tables.

    python evaluate.py                                  # default model + prompt -> eval/results/<model>.md
    python evaluate.py --prompt v2 --tag prompt-v2-run1 # a prompt experiment, saved under its own name
    python evaluate.py --questions eval/long_questions.json --compact-after 30000 --tag memory-on-run1
    python evaluate.py --models qwen/qwen3.7-flash openai/gpt-5-nano   # also writes eval/comparison.md
    python evaluate.py --bake-off                       # every model in config.BAKE_OFF_MODELS
    python evaluate.py --only t1-cp-q3fy2026            # a few questions, printed only
    python evaluate.py --regrade [--tag ...]            # re-score saved traces: no model calls, no cost

Answer keys for figures come straight from XBRL through the same get_fact tool the agent uses,
so no number is typed in by hand. Text answers are graded by required words or figures.
Use --regrade after changing the grader or the answer checks, so the fix is measured on the same
answers instead of on a fresh (and different) set of model runs.
"""

import argparse
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

from filing_desk import config, llm, tools
from filing_desk.agent import run_agent
from filing_desk.checks import SCALES, TOLERANCE, check_answer, extract_figures, is_refusal
from filing_desk.config import ROOT
from filing_desk.prompts import DEFAULT_PROMPT_VERSION, PROMPTS

QUESTIONS_PATH = ROOT / "eval" / "questions.json"
RESULTS_DIR = ROOT / "eval" / "results"
COMPARISON_MD = ROOT / "eval" / "comparison.md"

TIER_NAMES = {
    1: "single fact",
    2: "single document, text",
    3: "multi-period / multi-filing",
    4: "traps (refuse, or correct the premise)",
}


# ----------------------------------------------------------------------------- grading
def expected_value(concept: str, period: str) -> float:
    """The XBRL value for a concept and period. For flows, the longest duration (the full year)."""
    facts = tools.get_fact(concept=concept, period=period)["facts"]
    instants = [f for f in facts if f["period_type"] == "instant"]
    if instants:
        return instants[0]["value"]
    return min(facts, key=lambda f: f["period"])["value"]


def mentions(amounts: list[float], expected: float) -> bool:
    return any(abs(amount * scale - expected) <= abs(expected) * TOLERANCE for amount in amounts for scale in SCALES)


def grade(expect: dict, answer: str) -> tuple[bool, str]:
    refused = is_refusal(answer)
    if expect.get("refusal"):
        return refused, "refused as expected" if refused else "answered a question it should have refused"
    if refused and not expect.get("allow_refusal"):
        return False, "refused an answerable question"

    figures = extract_figures(answer)
    amounts = [f["value"] for f in figures if f["kind"] == "amount"]
    percents = [f["value"] for f in figures if f["kind"] == "percent"]
    lowered = answer.lower()
    problems = []

    for fact in expect.get("facts", []):
        options = [expected_value(concept, fact["period"]) for concept in fact["concepts"]]
        if not any(mentions(amounts, value) for value in options):
            problems.append(f"missing {' or '.join(fact['concepts'])} for {fact['period']}")
    for value in expect.get("amounts", []):
        if not mentions(amounts, value):
            problems.append(f"missing amount {value:,.0f}")
    for value in expect.get("percents", []):
        if not any(abs(p - value) <= 0.051 for p in percents):
            problems.append(f"missing {value}%")
    for word in expect.get("keywords_all", []):
        if word.lower() not in lowered:
            problems.append(f"missing '{word}'")
    any_words = expect.get("keywords_any", [])
    if any_words and not any(word.lower() in lowered for word in any_words):
        problems.append(f"none of {any_words}")

    return not problems, "; ".join(problems) or "ok"


# ----------------------------------------------------------------------------- running
def build_row(question: dict, run: dict) -> dict:
    """One results row from a run record (fresh from run_agent, or loaded from a saved trace)."""
    if run.get("error"):
        correct, note = False, f"run failed: {run['error']}"
    else:
        correct, note = grade(question["expect"], run["answer"])
    usage = run["usage"]
    return {
        "id": question["id"],
        "tier": question["tier"],
        "should_refuse": bool(question["expect"].get("refusal")),
        "correct": correct,
        "checks_passed": run["checks"]["passed"],
        "refused": run["checks"]["refused"],
        "tool_calls": len(run["tool_calls"]),
        "duplicate_calls": sum(1 for call in run["tool_calls"] if call["duplicate_of"]),
        "refetches": sum(1 for call in run["tool_calls"] if call.get("refetch_of")),
        "peak_context_chars": max((turn["context_chars"] for turn in run["turns"]), default=0),
        "compactions": len(run.get("compactions", [])),
        "compact_after_chars": run["settings"]["compact_after_chars"],
        "tokens": usage["prompt_tokens"] + usage["completion_tokens"],
        "cost_usd": usage["cost_usd"],
        "seconds": run["seconds"],
        "stop_reason": run["stop_reason"],
        "model": run["model"],
        "prompt_version": run.get("prompt_version", "v1"),
        "note": note,
        "trace": run["trace_path"],
    }


def print_row(row: dict) -> None:
    print(
        f"    {'correct' if row['correct'] else 'WRONG'} | checks {'pass' if row['checks_passed'] else 'FAIL'} "
        f"| {row['tool_calls']} tool calls | peak {row['peak_context_chars']:,} chars | "
        f"{row['compactions']} compactions | ${row['cost_usd']:.4f} | {row['seconds']:.1f}s | {row['note']}",
        flush=True,
    )


def run_questions(questions: list[dict], model: str, prompt_version: str) -> list[dict]:
    rows = []
    for number, question in enumerate(questions, start=1):
        print(f"[{number}/{len(questions)}] {question['id']}", flush=True)
        run = run_agent(question["question"], model=model, prompt_version=prompt_version)
        row = build_row(question, run)
        rows.append(row)
        print_row(row)
    return rows


def regrade(questions: list[dict], model: str, tag: str | None) -> list[dict]:
    """Re-score the saved traces of a results file with the current checks and grader."""
    by_id = {question["id"]: question for question in questions}
    saved = json.loads((RESULTS_DIR / f"{results_name(model, tag)}.json").read_text(encoding="utf-8"))
    rows = []
    for old in saved:
        if old["id"] not in by_id:
            print(f"skipping {old['id']}: not in the questions file")
            continue
        run = json.loads(Path(old["trace"]).read_text(encoding="utf-8"))
        results = [call["result"] for call in run["tool_calls"] if not call["error"] and not call["duplicate_of"]]
        run["checks"] = check_answer(run["answer"], results)
        run["trace_path"] = old["trace"]
        row = build_row(by_id[old["id"]], run)
        print(f"{row['id']}")
        print_row(row)
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- reporting
def model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", model.lower()).strip("-")


def results_name(model: str, tag: str | None) -> str:
    """eval/results/<model>.md, or <model>__<tag>.md for a named experiment run."""
    return model_slug(model) + (f"__{model_slug(tag)}" if tag else "")


def tier_score(rows: list[dict], tier: int, key: str = "correct") -> str:
    group = [row for row in rows if row["tier"] == tier]
    return f"{sum(row[key] for row in group)}/{len(group)}" if group else "-"


def tier_table(rows: list[dict]) -> list[str]:
    lines = [
        "| Tier | Questions | Correct | Checks passed | Avg tool calls | Max tool calls | Avg tokens | Cost (USD) | Median s | Max s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for tier in sorted({row["tier"] for row in rows}):
        group = [row for row in rows if row["tier"] == tier]
        calls = [row["tool_calls"] for row in group]
        seconds = [row["seconds"] for row in group]
        lines.append(
            f"| {tier} · {TIER_NAMES.get(tier, '')} | {len(group)} | {tier_score(rows, tier)} "
            f"| {tier_score(rows, tier, 'checks_passed')} | {statistics.mean(calls):.1f} | {max(calls)} "
            f"| {statistics.mean(row['tokens'] for row in group):,.0f} "
            f"| {sum(row['cost_usd'] for row in group):.4f} "
            f"| {statistics.median(seconds):.1f} | {max(seconds):.1f} |"
        )
    return lines


def write_results(rows: list[dict], model: str, tag: str | None = None, regraded: bool = False) -> str:
    traps = [row for row in rows if row["should_refuse"]]
    answerable = [row for row in rows if not row["should_refuse"]]
    seconds = [row["seconds"] for row in rows]
    total_cost = sum(row["cost_usd"] for row in rows)
    prompts = ", ".join(sorted({row["prompt_version"] for row in rows}))
    ceilings = ", ".join(f"{c:,}" for c in sorted({row["compact_after_chars"] for row in rows}))
    how = "`python evaluate.py --regrade` (saved traces re-scored)" if regraded else "`python evaluate.py`"
    md = [
        f"# Eval results: `{model}`" + (f" · `{tag}`" if tag else ""),
        "",
        f"Generated by {how} on {datetime.now():%Y-%m-%d %H:%M} · prompt {prompts} · compaction after "
        f"{ceilings} chars · {len(rows)} questions. Nothing in this file is edited by hand.",
        "",
        "## By tier",
        "",
        *tier_table(rows),
        "",
        "## Refusals",
        "",
        f"- Correct refusals on questions that should be refused: {sum(row['refused'] for row in traps)}/{len(traps)}",
        f"- Refusals on answerable questions: {sum(row['refused'] for row in answerable)}/{len(answerable)}",
        "",
        "## Cost, latency and memory",
        "",
        f"- Total cost: ${total_cost:.4f} (${total_cost / len(rows):.4f} per question)",
        f"- Latency: median {statistics.median(seconds):.1f}s, max {max(seconds):.1f}s",
        f"- Peak context sent to the model: {max(row['peak_context_chars'] for row in rows):,} chars",
        f"- Compactions: {sum(row['compactions'] for row in rows)} · refetches after compaction: "
        f"{sum(row['refetches'] for row in rows)} · duplicate calls caught: {sum(row['duplicate_calls'] for row in rows)}",
        "",
        "## Per question",
        "",
        "| Question | Tier | Correct | Checks | Tool calls | Peak context | Compactions | Tokens | Cost | Seconds | Stop | Note |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        md.append(
            f"| `{row['id']}` | {row['tier']} | {'yes' if row['correct'] else '**no**'} "
            f"| {'pass' if row['checks_passed'] else '**fail**'} | {row['tool_calls']} "
            f"| {row['peak_context_chars']:,} | {row['compactions']} | {row['tokens']:,} "
            f"| {row['cost_usd']:.4f} | {row['seconds']:.1f} | {row['stop_reason']} | {row['note']} |"
        )
    name = results_name(model, tag)
    md += ["", f"Traces for every run are in `runs/`; `{name}.json` names the file for each question."]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{name}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (RESULTS_DIR / f"{name}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return f"eval/results/{name}.md"


def write_comparison(results: dict[str, list[dict]]) -> None:
    tiers = sorted({row["tier"] for rows in results.values() for row in rows})
    header = " | ".join(f"Tier {t} correct" for t in tiers)
    md = [
        "# Model comparison",
        "",
        f"Generated by `python evaluate.py` on {datetime.now():%Y-%m-%d %H:%M}. Same questions, prompt and "
        "budgets for every model; cost is what OpenRouter billed.",
        "",
        f"| Model | {header} | Checks passed | Avg tool calls | Cost / question | Median s | Max s |",
        "|---|" + "---:|" * len(tiers) + "---:|---:|---:|---:|---:|",
    ]
    for model, rows in results.items():
        seconds = [row["seconds"] for row in rows]
        scores = " | ".join(tier_score(rows, t) for t in tiers)
        md.append(
            f"| `{model}` | {scores} | {sum(row['checks_passed'] for row in rows)}/{len(rows)} "
            f"| {statistics.mean(row['tool_calls'] for row in rows):.1f} "
            f"| ${sum(row['cost_usd'] for row in rows) / len(rows):.4f} "
            f"| {statistics.median(seconds):.1f} | {max(seconds):.1f} |"
        )
    md += ["", "Per-model detail: `eval/results/<model>.md`."]
    COMPARISON_MD.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run the Filing Desk eval set.")
    parser.add_argument("--models", nargs="+", metavar="MODEL", help="OpenRouter model ids (default: FILING_DESK_MODEL)")
    parser.add_argument("--bake-off", action="store_true", help="run every model in config.BAKE_OFF_MODELS")
    parser.add_argument("--prompt", choices=sorted(PROMPTS), default=DEFAULT_PROMPT_VERSION, help="system prompt version")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH, help="questions file (default: eval/questions.json)")
    parser.add_argument("--compact-after", type=int, metavar="CHARS", help="override config.COMPACT_AFTER_CHARS")
    parser.add_argument("--tag", help="name this run; results go to eval/results/<model>__<tag>.md")
    parser.add_argument("--only", nargs="+", metavar="ID", help="run only these question ids (results files untouched)")
    parser.add_argument("--regrade", action="store_true", help="re-score saved traces with the current grader (free)")
    args = parser.parse_args()

    if args.compact_after:
        config.COMPACT_AFTER_CHARS = args.compact_after
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]
    if not questions:
        print("No questions matched.")
        return
    models = config.BAKE_OFF_MODELS if args.bake_off else (args.models or [llm.default_model()])

    results = {}
    for model in models:
        label = "re-grading saved traces" if args.regrade else f"prompt {args.prompt}"
        print(f"\n=== {model} · {label}{f' · {args.tag}' if args.tag else ''} ===", flush=True)
        if args.regrade:
            rows = regrade(questions, model, args.tag)
        else:
            rows = run_questions(questions, model, args.prompt)
        results[model] = rows
        cost = sum(row["cost_usd"] for row in rows)
        print(f"{model}: {sum(row['correct'] for row in rows)}/{len(rows)} correct, "
              f"{sum(row['checks_passed'] for row in rows)}/{len(rows)} checks passed, ${cost:.4f}")
        if not args.only:
            print(f"Wrote {write_results(rows, model, args.tag, regraded=args.regrade)}")

    if len(models) > 1 and not args.only:
        write_comparison(results)
        print("Wrote eval/comparison.md")


if __name__ == "__main__":
    main()
