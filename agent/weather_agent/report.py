"""Turn an investigation into a Markdown incident report (and optionally a Slack post)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .loop import AgentResult


def render(findings: list[dict], result: AgentResult, model: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = result.report or {}
    out = [f"# weather-edge investigation, {now}", ""]
    if not result.report:
        out += [f"**The agent did not produce a report** (stopped: {result.stop} after "
                f"{result.steps} steps). Treat the monitor findings below as unexplained.", ""]
    else:
        out += [
            f"**Status:** {report.get('status')}  ",
            f"**Root cause(s):** {', '.join(report.get('root_causes', []))}  ",
            f"**Layer:** {report.get('affected_layer')}  ",
            f"**Confidence:** {report.get('confidence')}",
            "", "## Summary", "", report.get("summary", ""),
            "", "## User impact", "", report.get("user_impact", ""),
            "", "## Evidence", "",
            *[f"- {item}" for item in report.get("evidence", [])],
            "", "## Recommended actions", "",
            *[f"- {item}" for item in report.get("recommended_actions", [])],
        ]
    out += ["", "## Monitor findings", ""]
    out += [f"- [{f['severity']}] {f['check']}: {f['detail']}" for f in findings] or ["- none"]
    out += ["", "## Audit trail", "",
            f"Model `{model}`, {result.steps} steps, {result.input_tokens} input / "
            f"{result.output_tokens} output tokens.", ""]
    for entry in result.trace:
        flag = " (error)" if entry["is_error"] else ""
        out.append(f"{entry['step']}. `{entry['tool']}` `{json.dumps(entry['input'])}`{flag}")
    return "\n".join(out) + "\n"


def save(markdown: str, directory: str | Path = "reports") -> Path:
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"investigation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
    path.write_text(markdown)
    return path


def post_to_slack(webhook_url: str, result: AgentResult) -> None:
    import requests

    report = result.report or {}
    text = (f"*weather-edge:* {report.get('status', 'no report')} "
            f"({', '.join(report.get('root_causes', ['unexplained']))})\n{report.get('summary', '')}")
    requests.post(webhook_url, json={"text": text}, timeout=10)
