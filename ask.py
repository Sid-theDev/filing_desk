"""Ask Filing Desk one question from the command line.

    python ask.py                                   # the project's target question
    python ask.py "How much commercial paper did Apple have at the end of FY2024?"
    python ask.py --via-mcp                         # same agent, tools called through the MCP server
    python ask.py --model qwen/qwen3.7-flash "..."  # try another OpenRouter model
"""

import argparse
import sys

from filing_desk.agent import run_agent
from filing_desk.mcp_client import MCPTools
from filing_desk.prompts import PROMPTS

TARGET_QUESTION = "How has Apple's long-term debt changed across FY2022-FY2024, and what reason does management give?"


def print_tool_call(record: dict) -> None:
    flag = " (duplicate)" if record["duplicate_of"] else f" (error: {record['error']})" if record["error"] else ""
    print(f"  step {record['step']}: {record['summary']} -> {record['result_chars']:,} chars{flag}", flush=True)


def print_run(run: dict) -> None:
    print("\nAnswer:\n" + (run["answer"] or "(none)"))

    checks = run["checks"]
    verdict = "PASSED" if checks["passed"] else "FAILED"
    print(f"\nChecks {verdict}: {checks['figures_checked']} figures, {checks['citations']} citations")
    for figure in checks["unsupported_figures"]:
        print(f"  figure not found in any tool result: {figure}")
    for cite in checks["invalid_citations"]:
        print(f"  citation not backed by this run: {cite}")

    usage = run["usage"]
    peak = max((turn["context_chars"] for turn in run["turns"]), default=0)
    print(
        f"\nModel {run['model']} | prompt {run['prompt_version']} | tools via {run['tools_via']} | "
        f"stop: {run['stop_reason']} | model calls: {len(run['turns'])} | tool calls: {len(run['tool_calls'])} | "
        f"peak context: {peak:,} chars | tokens: {usage['prompt_tokens']:,} in, {usage['completion_tokens']:,} out | "
        f"cost: ${usage['cost_usd']:.4f} | {run['seconds']}s"
    )
    if run.get("error"):
        print(f"Error: {run['error']}")
    print(f"Trace: {run['trace_path']}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Ask Filing Desk a question about Apple's SEC filings.")
    parser.add_argument("question", nargs="?", default=TARGET_QUESTION)
    parser.add_argument("--model", help="OpenRouter model id (default: FILING_DESK_MODEL in .env)")
    parser.add_argument("--prompt", choices=sorted(PROMPTS), help="system prompt version (default: prompts.py)")
    parser.add_argument("--via-mcp", action="store_true", help="call the tools through the MCP server (logs to runs/mcp_calls.jsonl)")
    args = parser.parse_args()

    print(f"Question: {args.question}\n")
    options = {"model": args.model, "prompt_version": args.prompt, "on_tool_call": print_tool_call}
    if args.via_mcp:
        with MCPTools() as server:
            print(f"Connected to the MCP server. Tools: {', '.join(server.list_tool_names())}\n")
            run = run_agent(args.question, run_tool=server.run_tool, tools_via="mcp", **options)
    else:
        run = run_agent(args.question, **options)
    print_run(run)


if __name__ == "__main__":
    main()
