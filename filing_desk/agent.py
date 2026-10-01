"""The agent loop, written by hand. No framework.

    1. Send the conversation and the tool definitions to the model.
    2. If it asks for tools: run them, append the results, go again.
    3. If it answers in text: stop, check the answer mechanically, save the trace.

The message list is the single source of truth for a run. Limits on steps, tokens and time stop
the loop on purpose, and a forced stop still returns an answer, labelled PARTIAL ANSWER.

Memory within a run:
- An identical repeat call is not executed again; the model gets a pointer to the earlier result.
- When the context grows past COMPACT_AFTER_CHARS, old read_section text is replaced by a one-line
  stub, but only text the model has already read. Search hits, get_fact and list_filings results
  are never dropped: they are small and carry the figures and citations.
- A call repeated after its result was compacted runs again and is counted as a refetch.

Tools are called directly (tools.run_tool) or through the MCP server (mcp_client.MCPTools.run_tool);
both return the same dicts, so the loop does not care which.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, llm
from .checks import check_answer
from .prompts import DEFAULT_PROMPT_VERSION, FINAL_ANSWER_PROMPT, PROMPTS
from .tools import TOOL_SPECS, run_tool

# Only bulky text is compacted. Dropping search hits made the model search again (experiment 4).
COMPACTABLE_TOOLS = {"read_section"}


def run_agent(question: str, model: str | None = None, prompt_version: str | None = None,
              call_model=llm.call_model, run_tool=run_tool, tools_via: str = "direct",
              runs_dir: Path = config.RUNS_DIR, on_tool_call=None) -> dict:
    """Answer one question. Returns the run record (answer, checks, usage, trace path)."""
    model = model or llm.default_model()
    prompt_version = prompt_version or DEFAULT_PROMPT_VERSION
    started = time.monotonic()
    messages = [
        {"role": "system", "content": PROMPTS[prompt_version]},
        {"role": "user", "content": question},
    ]
    run = new_run_record(question, model, prompt_version, tools_via)
    calls = {}      # tool_call_id -> name, signature, summary, step (used by compaction)
    seen = {}       # signature -> step, for results still in the context
    executed = {}   # signature -> step it first ran, including results since compacted
    results = []    # full tool results, for the answer checks
    answer = ""
    stop_reason = "step_limit"

    try:
        for step in range(1, config.MAX_STEPS + 1):
            limit = budget_exceeded(run, started)
            if limit:
                stop_reason = limit
                break
            compact_old_results(messages, calls, seen, run, step)

            sent_chars = context_size(messages)
            reply = call_model(messages, TOOL_SPECS, model)
            add_usage(run, reply["usage"])
            message = reply["message"]
            messages.append(message)
            tool_calls = message.get("tool_calls") or []
            run["turns"].append({
                "step": step,
                "context_chars": sent_chars,
                **reply["usage"],
                "tool_calls": len(tool_calls),
                "finish_reason": reply.get("finish_reason"),
                "provider": reply.get("provider"),
            })

            if not tool_calls:
                answer = message.get("content") or ""
                stop_reason = "answered"
                break

            for call in tool_calls:
                record = execute_call(call, step, seen, executed, run_tool)
                result = record["result"]
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })
                calls[call["id"]] = {"name": record["tool"], "signature": record["signature"],
                                     "summary": record["summary"], "step": step}
                run["tool_calls"].append(record)
                if "error" not in result and not record["duplicate_of"]:
                    results.append(result)
                if on_tool_call:
                    on_tool_call(record)

        if stop_reason != "answered":
            answer = force_final_answer(messages, call_model, model, stop_reason, run)
    except llm.ModelError as error:
        stop_reason = "model_error"
        run["error"] = str(error)

    run["answer"] = answer
    run["stop_reason"] = stop_reason
    run["checks"] = check_answer(answer, results)
    run["seconds"] = round(time.monotonic() - started, 2)
    run["usage"]["cost_usd"] = round(run["usage"]["cost_usd"], 6)
    run["trace_path"] = save_trace(run, messages, runs_dir)
    return run


def new_run_record(question: str, model: str, prompt_version: str, tools_via: str) -> dict:
    return {
        "question": question,
        "model": model,
        "prompt_version": prompt_version,
        "tools_via": tools_via,
        "settings": {
            "temperature": config.TEMPERATURE,
            "max_output_tokens": config.MAX_OUTPUT_TOKENS,
            "max_steps": config.MAX_STEPS,
            "max_total_tokens": config.MAX_TOTAL_TOKENS,
            "max_seconds": config.MAX_SECONDS,
            "compact_after_chars": config.COMPACT_AFTER_CHARS,
            "compactable_tools": sorted(COMPACTABLE_TOOLS),
        },
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "turns": [],
        "tool_calls": [],
        "compactions": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
    }


def execute_call(call: dict, step: int, seen: dict, executed: dict, run_tool) -> dict:
    """Run one tool call. Bad JSON and repeats come back as results the model can read."""
    name = call["function"]["name"]
    started = time.monotonic()
    try:
        arguments = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        arguments = None

    signature = f"{name} {json.dumps(arguments, sort_keys=True)}"
    duplicate_of = refetch_of = None
    if arguments is None:
        result = {"error": "Arguments were not valid JSON.", "hint": "Send the arguments as a JSON object."}
    elif signature in seen:
        duplicate_of = seen[signature]
        result = {"note": f"Duplicate call: you made this exact call at step {duplicate_of}. "
                          "Use that result above instead of repeating it."}
    else:
        refetch_of = executed.get(signature)  # ran before, then compaction removed the result
        seen[signature] = step
        executed.setdefault(signature, step)
        result = run_tool(name, arguments)

    return {
        "step": step,
        "tool": name,
        "arguments": arguments,
        "summary": f"{name}({short_arguments(arguments)})",
        "signature": signature,
        "result_chars": len(json.dumps(result, ensure_ascii=False)),
        "ms": round((time.monotonic() - started) * 1000, 1),
        "error": result.get("error"),
        "duplicate_of": duplicate_of,
        "refetch_of": refetch_of,
        "result": result,
    }


def force_final_answer(messages: list[dict], call_model, model: str, reason: str, run: dict) -> str:
    """A limit was hit: ask for an answer from what is already held, with tools switched off."""
    messages.append({"role": "user", "content": FINAL_ANSWER_PROMPT.format(reason=reason.replace("_", " "))})
    reply = call_model(messages, TOOL_SPECS, model, tool_choice="none")
    add_usage(run, reply["usage"])
    messages.append(reply["message"])
    answer = (reply["message"].get("content") or "").strip() or "(no answer produced)"
    return answer if answer.startswith("PARTIAL ANSWER") else f"PARTIAL ANSWER: {answer}"


def compact_old_results(messages: list[dict], calls: dict, seen: dict, run: dict, step: int) -> None:
    """Replace old read_section text with stubs once the context passes the threshold.

    Runs just before the model call for `step`. Results from step - 1 have not been sent to the model
    yet, so only results from step - 2 or earlier (already read at least once) are eligible.
    """
    before = context_size(messages)
    if before <= config.COMPACT_AFTER_CHARS:
        return

    dropped = []
    for message in messages:
        if message["role"] != "tool":
            continue
        call = calls.get(message["tool_call_id"])
        if (not call or call["name"] not in COMPACTABLE_TOOLS or call.get("compacted")
                or call["step"] >= step - 1):
            continue
        message["content"] = json.dumps({
            "compacted": True,
            "was": call["summary"],
            "hint": "Removed to save space. Repeat the call if you need this text again.",
        })
        call["compacted"] = True
        seen.pop(call["signature"], None)  # a repeat is now a real call, not a duplicate
        dropped.append(call["summary"])

    if dropped:
        run["compactions"].append({"before_chars": before, "after_chars": context_size(messages), "dropped": dropped})


def budget_exceeded(run: dict, started: float) -> str | None:
    usage = run["usage"]
    if usage["prompt_tokens"] + usage["completion_tokens"] >= config.MAX_TOTAL_TOKENS:
        return "token_limit"
    if time.monotonic() - started >= config.MAX_SECONDS:
        return "time_limit"
    return None


def add_usage(run: dict, usage: dict) -> None:
    for key in ("prompt_tokens", "completion_tokens", "cost_usd"):
        run["usage"][key] += usage.get(key) or 0


def context_size(messages: list[dict]) -> int:
    """Characters in the conversation: a cheap stand-in for tokens."""
    return sum(len(m.get("content") or "") + (len(json.dumps(m["tool_calls"])) if m.get("tool_calls") else 0)
               for m in messages)


def short_arguments(arguments) -> str:
    if not isinstance(arguments, dict):
        return "?"
    return ", ".join(f"{key}={value!r}" for key, value in arguments.items())


def save_trace(run: dict, messages: list[dict], runs_dir: Path) -> str:
    """One JSON file per run: everything needed to read the run end to end.

    Files are created exclusively ('x' mode), so eval runs in parallel never overwrite each other.
    """
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", run["question"].lower()).strip("-")[:48]
    body = json.dumps({**run, "messages": messages}, indent=2, ensure_ascii=False)
    counter = 1
    while True:
        path = runs_dir / f"{stamp}_{slug}{'' if counter == 1 else f'-{counter}'}.json"
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(body)
            return str(path)
        except FileExistsError:
            counter += 1
