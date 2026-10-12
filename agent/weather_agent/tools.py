"""The agent's tools: what it can look at, and the two things it may ask to change.

A tool is a name, a description the model reads to decide when to use it,
a JSON schema for its arguments, and a Python function. That is all "tool
use" is. The descriptions matter as much as the code: they are the only
documentation the model gets.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from . import probes
from .guards import check_readonly_sql, redact
from .prompts import REPORT_SCHEMA

MAX_OUTPUT_CHARS = 6000


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., object] | None = None
    requires_approval: bool = False


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in self._tools.values()]

    def execute(self, name: str, args: dict, approve: Callable[[str, dict], bool]) -> tuple[str, bool]:
        """Run one tool call. Returns (text for the model, is_error)."""
        tool = self._tools.get(name)
        if tool is None or tool.fn is None:
            return f"Unknown tool '{name}'. Available: {', '.join(self._tools)}", True
        if tool.requires_approval and not approve(name, args):
            return ("The operator declined this action. Do not retry it; describe it under "
                    "recommended_actions instead."), False
        try:
            result = tool.fn(**args)
        except Exception as exc:  # the model should see failures and adapt, not crash the loop
            return redact(f"{type(exc).__name__}: {exc}"), True
        text = redact(json.dumps(result, default=str))
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + f"... [truncated, {len(text)} chars total]"
        return text, False


_PATH = {"type": "string",
         "description": "Path such as /v1/index.json or /v1/current/paris.json"}


def build_registry(cfg, dbx=None, cf=None, *, allow_actions: bool = False) -> ToolRegistry:
    reg = ToolRegistry()

    reg.add(Tool(
        "probe_edge",
        "Fetch a path from the public CDN exactly as a user would. Returns HTTP status, cache "
        "headers (x-cache = HIT/MISS/REVALIDATED/STALE, x-cache-age, x-edge-ttl, x-origin-status), "
        "the document, its generated_at, how many minutes old the data is, and any null fields.",
        {"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
        lambda path: probes.probe_edge(cfg, path).to_dict(),
    ))

    if cfg.has_origin:
        reg.add(Tool(
            "probe_origin",
            "Read a blob straight from the origin storage container, bypassing the CDN. Use it to "
            "tell 'the edge is serving old data' apart from 'the origin itself is old'. The path "
            "is relative to the container, so prefixes other than /v1/ can be inspected too.",
            {"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
            lambda path: probes.probe_origin(cfg, path).to_dict(),
        ))

    if dbx is not None:
        reg.add(Tool(
            "list_job_runs",
            "List the most recent runs of the Databricks pipeline job, newest first, with start "
            "and end times, overall result, and the result of each task (ingest, transform, publish).",
            {"type": "object",
             "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
             "required": []},
            lambda limit=5: dbx.list_runs(limit),
        ))
        reg.add(Tool(
            "get_task_output",
            "Get one task's output from one job run: its error message and stack trace if it "
            "failed, and the summary value it returned if it succeeded (for publish, that "
            "includes how many files were written and under which prefix).",
            {"type": "object",
             "properties": {"run_id": {"type": "integer"},
                            "task_key": {"type": "string", "enum": ["ingest", "transform", "publish"]}},
             "required": ["run_id", "task_key"]},
            lambda run_id, task_key: dbx.task_output(run_id, task_key),
        ))
        if cfg.databricks_warehouse_id:
            ns = f"{cfg.catalog}.{cfg.schema}"
            reg.add(Tool(
                "query_lakehouse",
                "Run one read-only SQL query (SELECT, WITH, DESCRIBE, SHOW) on the Databricks SQL "
                f"warehouse. Useful tables: {ns}.ops_run_log (logged_at, batch_id, task, status, "
                f"detail), {ns}.bronze_observations (raw API payloads as JSON strings), "
                f"{ns}.silver_observations (parsed rows), and system.lakeflow.job_run_timeline "
                "for run history. Returns at most 50 rows; use DESCRIBE if unsure of columns.",
                {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
                lambda sql: dbx.sql(check_readonly_sql(sql)),
            ))

    if cf is not None:
        reg.add(Tool(
            "edge_metrics",
            "Cloudflare analytics for the last N minutes: Worker invocations and errors by "
            "status, and HTTP response codes served for the hostname. Analytics lag real time "
            "by a minute or two, and low traffic means small numbers.",
            {"type": "object",
             "properties": {"minutes": {"type": "integer", "minimum": 5, "maximum": 1440}},
             "required": []},
            lambda minutes=30: {"worker": cf.worker_metrics(minutes), "zone": cf.zone_status(minutes)},
        ))

    if allow_actions:
        if dbx is not None:
            reg.add(Tool(
                "rerun_pipeline",
                "ACTION, needs human approval. Trigger a fresh run of the pipeline job and wait "
                "for it to finish. Appropriate when a transient fault has cleared and the data "
                "only needs regenerating. It will not fix a cause that is still present.",
                {"type": "object", "properties": {}, "required": []},
                lambda: dbx.run_now("none"),
                requires_approval=True,
            ))
        if cf is not None:
            reg.add(Tool(
                "purge_edge_cache",
                "ACTION, needs human approval. Evict cached copies at the edge so the next "
                "request re-reads the origin. Pass paths to purge specific documents, or omit "
                "them to purge everything for this hostname. Only helps when the origin already "
                "holds good data.",
                {"type": "object",
                 "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                 "required": []},
                lambda paths=None: cf.purge([f"{cfg.edge_base_url}{p}" for p in paths] if paths else None),
                requires_approval=True,
            ))

    reg.add(Tool(
        "submit_report",
        "Submit the final investigation report. Call this exactly once, when you are done.",
        REPORT_SCHEMA,
    ))
    return reg
