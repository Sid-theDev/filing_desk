"""Offline tests for the agent loop and the answer checks, using a scripted fake model (no API calls)."""

import json
from pathlib import Path

from filing_desk import config, tools
from filing_desk.agent import run_agent
from filing_desk.checks import check_answer, extract_figures
from filing_desk.prompts import PROMPTS


def fake_model(replies: list[dict]):
    """A stand-in for llm.call_model that returns the given assistant messages in order."""
    queue = list(replies)

    def call_model(messages, tool_specs, model, tool_choice="auto"):
        return {
            "message": queue.pop(0),
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": 0.0001},
            "finish_reason": "stop",
        }

    return call_model


def ask_tool(call_id: str, name: str, **arguments) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
    ]}


def say(text: str) -> dict:
    return {"role": "assistant", "content": text}


def noncurrent_fy2024() -> dict:
    return tools.get_fact("LongTermDebtNoncurrent", "FY2024")["facts"][0]


# ----------------------------------------------------------------------------- loop
def test_answer_with_valid_citation_passes_checks(tmp_path):
    fact = noncurrent_fy2024()
    model = fake_model([
        ask_tool("c1", "get_fact", concept="LongTermDebtNoncurrent", period="FY2024"),
        ask_tool("c2", "get_fact", concept="LongTermDebtNoncurrent", period="FY2024"),
        say(f"Non-current term debt was ${fact['value'] / 1e9:.2f} billion as of 2024-09-28 {fact['cite']}."),
    ])
    run = run_agent("question", model="fake", call_model=model, runs_dir=tmp_path)

    assert run["stop_reason"] == "answered"
    assert run["tool_calls"][1]["duplicate_of"] == 1
    assert "Duplicate call" in run["tool_calls"][1]["result"]["note"]
    assert run["checks"]["passed"], run["checks"]
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_invented_figure_fails_checks(tmp_path):
    model = fake_model([
        ask_tool("c1", "get_fact", concept="LongTermDebtNoncurrent", period="FY2024"),
        say("Non-current term debt was $123.45 billion [xbrl LongTermDebtNoncurrent 2024-09-28]."),
    ])
    run = run_agent("question", model="fake", call_model=model, runs_dir=tmp_path)
    assert run["checks"]["unsupported_figures"] == ["$123.45 billion"]
    assert not run["checks"]["passed"]


def test_tool_error_reaches_model_instead_of_crashing(tmp_path):
    model = fake_model([
        ask_tool("c1", "read_section", filing_id="aapl-10k-fy2099"),
        say("NOT FOUND: that filing is not available."),
    ])
    run = run_agent("question", model="fake", call_model=model, runs_dir=tmp_path)
    assert "Unknown filing_id" in run["tool_calls"][0]["error"]
    assert run["checks"]["refused"]


def test_step_limit_forces_partial_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_STEPS", 3)
    model = fake_model([
        ask_tool("c1", "read_section", filing_id="aapl-10k-fy2024", start_line=1),
        ask_tool("c2", "read_section", filing_id="aapl-10k-fy2024", start_line=200),
        ask_tool("c3", "read_section", filing_id="aapl-10k-fy2024", start_line=400),
        say("I could not finish."),
    ])
    run = run_agent("question", model="fake", call_model=model, runs_dir=tmp_path)
    assert run["stop_reason"] == "step_limit"
    assert run["answer"].startswith("PARTIAL ANSWER")


def test_results_the_model_has_not_seen_are_never_compacted(tmp_path, monkeypatch):
    """Experiment 4's bug: a parallel batch was compacted before the model ever read it."""
    monkeypatch.setattr(config, "COMPACT_AFTER_CHARS", 100)
    batch = {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{n}", "type": "function",
         "function": {"name": "read_section", "arguments": json.dumps({"filing_id": "aapl-10k-fy2024", "start_line": n})}}
        for n in (1, 300, 600)
    ]}
    run = run_agent("question", model="fake", call_model=fake_model([batch, say("done")]), runs_dir=tmp_path)
    trace = json.loads(Path(run["trace_path"]).read_text(encoding="utf-8"))
    assert run["compactions"] == []
    assert all("compacted" not in m["content"] for m in trace["messages"] if m["role"] == "tool")


def test_search_hits_are_never_compacted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COMPACT_AFTER_CHARS", 100)
    model = fake_model([
        ask_tool("c1", "search_filing", filing_id="aapl-10k-fy2024", query="commercial paper"),
        ask_tool("c2", "search_filing", filing_id="aapl-10k-fy2024", query="term debt"),
        ask_tool("c3", "search_filing", filing_id="aapl-10k-fy2024", query="notes"),
        say("done"),
    ])
    run = run_agent("question", model="fake", call_model=model, runs_dir=tmp_path)
    assert run["compactions"] == []


def test_compaction_drops_old_text_but_keeps_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COMPACT_AFTER_CHARS", 5_000)
    model = fake_model([
        ask_tool("c1", "get_fact", concept="LongTermDebtNoncurrent", period="FY2024"),
        ask_tool("c2", "read_section", filing_id="aapl-10k-fy2024", start_line=1),
        ask_tool("c3", "read_section", filing_id="aapl-10k-fy2024", start_line=300),
        ask_tool("c4", "read_section", filing_id="aapl-10k-fy2024", start_line=1),
        say("done"),
    ])
    run = run_agent("question", model="fake", call_model=model, runs_dir=tmp_path)

    assert run["compactions"]
    dropped = [summary for event in run["compactions"] for summary in event["dropped"]]
    assert all(not summary.startswith("get_fact") for summary in dropped)
    # the repeat of a compacted call is executed again, not answered with a pointer, and counted
    assert run["tool_calls"][3]["duplicate_of"] is None
    assert run["tool_calls"][3]["refetch_of"] == 2


def test_tools_can_be_swapped_for_another_runner(tmp_path):
    """The loop only needs a run_tool(name, arguments) -> dict, which is how --via-mcp plugs in."""
    called = []

    def fake_runner(name, arguments):
        called.append(name)
        return {"cite": "[aapl-10k-fy2024 L1]", "text": "1| hello"}

    model = fake_model([ask_tool("c1", "read_section", filing_id="aapl-10k-fy2024", start_line=1), say("done")])
    run = run_agent("question", model="fake", call_model=model, run_tool=fake_runner, tools_via="fake", runs_dir=tmp_path)
    assert called == ["read_section"]
    assert run["tools_via"] == "fake"


# ----------------------------------------------------------------------------- prompts
def test_prompt_v2_adds_exactly_one_rule():
    v1, v2 = PROMPTS["v1"].splitlines(), PROMPTS["v2"].splitlines()
    assert len(v2) == len(v1) + 1
    assert [line for line in v2 if line not in v1] == [line for line in v2 if "Answer every part" in line]


def test_run_uses_and_records_chosen_prompt(tmp_path):
    run = run_agent("question", model="fake", prompt_version="v2",
                    call_model=fake_model([say("NOT FOUND: nothing.")]), runs_dir=tmp_path)
    trace = json.loads(Path(run["trace_path"]).read_text(encoding="utf-8"))
    assert run["prompt_version"] == "v2"
    assert trace["messages"][0]["content"] == PROMPTS["v2"]


# ----------------------------------------------------------------------------- checks
def test_figures_skip_years_dates_and_citations():
    figures = extract_figures("In FY2024 (September 28, 2024, Item 8) debt was $85.8 billion [aapl-10k-fy2024 L2015].")
    assert [f["text"] for f in figures] == ["$85.8 billion"]


def test_rounded_and_millions_figures_are_supported():
    result = tools.get_fact("LongTermDebtNoncurrent", "FY2024")
    fact = result["facts"][0]
    millions = f"{fact['value'] / 1e6:,.0f}"
    billions = f"${fact['value'] / 1e9:.1f} billion"
    checks = check_answer(f"It was {billions} {fact['cite']}, shown as {millions} in millions.", [result])
    assert checks["passed"], checks


def test_difference_between_facts_is_supported():
    fy2023 = tools.get_fact("LongTermDebtNoncurrent", "FY2023")
    fy2024 = tools.get_fact("LongTermDebtNoncurrent", "FY2024")
    change = fy2023["facts"][0]["value"] - fy2024["facts"][0]["value"]
    answer = f"It fell by ${change / 1e9:.2f} billion [xbrl LongTermDebtNoncurrent 2024-09-28]."
    assert check_answer(answer, [fy2023, fy2024])["passed"]


def test_sum_and_difference_of_backed_figures_are_supported():
    """'Total = current + non-current' must not be flagged as invented."""
    current = tools.get_fact("LongTermDebtCurrent", "FY2023")
    noncurrent = tools.get_fact("LongTermDebtNoncurrent", "FY2023")
    a, b = current["facts"][0], noncurrent["facts"][0]
    answer = (
        f"Current ${a['value'] / 1e9:.3f} billion {a['cite']}, non-current ${b['value'] / 1e9:.3f} billion "
        f"{b['cite']}: total ${(a['value'] + b['value']) / 1e9:.3f} billion, "
        f"gap ${(b['value'] - a['value']) / 1e9:.3f} billion."
    )
    checks = check_answer(answer, [current, noncurrent])
    assert checks["passed"], checks


def test_derived_figure_still_needs_backed_parts():
    current = tools.get_fact("LongTermDebtCurrent", "FY2023")
    a = current["facts"][0]
    answer = f"Current ${a['value'] / 1e9:.3f} billion {a['cite']}, total $150.000 billion."
    assert check_answer(answer, [current])["unsupported_figures"] == ["$150.000 billion"]


def test_partial_answer_can_still_be_a_refusal():
    assert check_answer("PARTIAL ANSWER: NOT FOUND: no rating is disclosed.", [])["refused"]


def test_citation_to_unread_lines_is_invalid():
    read = tools.read_section("aapl-10k-fy2024", start_line=100)
    checks = check_answer("See [aapl-10k-fy2024 L3000].", [read])
    assert checks["invalid_citations"] == ["[aapl-10k-fy2024 L3000]"]
