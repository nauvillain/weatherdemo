"""The agent loop, written out by hand so every moving part is visible.

An "agent" is this and nothing more:
    1. send the model the conversation so far plus the list of tools
    2. if it asks for tools, run them and append the results
    3. repeat until it submits a report or runs out of steps

Frameworks wrap this loop; none of them change it. Once this file makes
sense, the natural next step is to swap the hand-written tools for MCP
servers (Databricks and Cloudflare both publish them) and keep the loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .prompts import SYSTEM_PROMPT, opening_message
from .tools import ToolRegistry


@dataclass
class AgentResult:
    report: dict | None
    steps: int = 0
    stop: str = ""            # "report", "max_steps", or "no_report"
    input_tokens: int = 0
    output_tokens: int = 0
    trace: list[dict] = field(default_factory=list)   # one entry per tool call, for the audit trail


def deny_all(_name: str, _args: dict) -> bool:
    return False


def run_agent(client, model: str, registry: ToolRegistry, findings: list[dict], *,
              forced: bool = False, approve: Callable[[str, dict], bool] = deny_all,
              max_steps: int = 15, max_tokens: int = 4096,
              on_event: Callable[[str], None] = lambda _msg: None) -> AgentResult:
    result = AgentResult(report=None)
    messages: list[dict] = [{"role": "user", "content": opening_message(findings, forced)}]
    nudged = False

    for step in range(1, max_steps + 1):
        result.steps = step
        response = client.messages.create(
            model=model, max_tokens=max_tokens, system=SYSTEM_PROMPT,
            tools=registry.specs(), messages=messages,
        )
        usage = getattr(response, "usage", None)
        result.input_tokens += getattr(usage, "input_tokens", 0) or 0
        result.output_tokens += getattr(usage, "output_tokens", 0) or 0
        messages.append({"role": "assistant", "content": response.content})

        calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not calls:
            # The model answered in prose instead of filing a report. Ask once, then give up.
            if nudged:
                result.stop = "no_report"
                return result
            nudged = True
            messages.append({"role": "user",
                             "content": "Please file your conclusion by calling submit_report."})
            continue

        tool_results = []
        for call in calls:
            if call.name == "submit_report":
                result.report = dict(call.input)
                content, is_error = "Report received.", False
            else:
                on_event(f"step {step}: {call.name} {call.input}")
                content, is_error = registry.execute(call.name, dict(call.input), approve)
            result.trace.append({"step": step, "tool": call.name, "input": dict(call.input),
                                 "is_error": is_error, "output_excerpt": content[:400]})
            tool_results.append({"type": "tool_result", "tool_use_id": call.id,
                                 "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": tool_results})

        if result.report is not None:
            result.stop = "report"
            return result

    # A hard step limit is a cost and safety control: a confused agent must stop, not spin.
    result.stop = "max_steps"
    return result
