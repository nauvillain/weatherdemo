"""Thin wrappers over the Databricks and Cloudflare APIs.

They return plain dicts so the detector, the agent tools, and the tests all
share one shape, and so nothing else in the codebase imports a vendor SDK.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from itertools import islice

from .guards import drop_drill_lines

# Columns that would hand the agent the answer during a drill.
HIDDEN_COLUMNS = {"job_parameters"}


def _val(x):
    return getattr(x, "value", x)


def _ms_iso(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DatabricksClient:
    def __init__(self, job_id: int, warehouse_id: str | None = None, workspace=None):
        if workspace is None:
            from databricks.sdk import WorkspaceClient

            workspace = WorkspaceClient()  # DATABRICKS_HOST / token / CLI profile from env
        self.w = workspace
        self.job_id = job_id
        self.warehouse_id = warehouse_id

    @staticmethod
    def _run_to_dict(run) -> dict:
        state = getattr(run, "state", None)
        status = getattr(run, "status", None)
        result = _val(getattr(state, "result_state", None))
        if result is None and status is not None and getattr(status, "termination_details", None):
            result = _val(status.termination_details.code)
        tasks = []
        for t in getattr(run, "tasks", None) or []:
            t_state = getattr(t, "state", None)
            tasks.append({"task_key": t.task_key, "run_id": t.run_id,
                          "result_state": _val(getattr(t_state, "result_state", None))})
        start, end = getattr(run, "start_time", None), getattr(run, "end_time", None)
        return {
            "run_id": run.run_id,
            "start_time": _ms_iso(start),
            "end_time": _ms_iso(end),
            "duration_s": round((end - start) / 1000) if start and end else None,
            "life_cycle_state": _val(getattr(state, "life_cycle_state", None)),
            "result_state": result,
            "state_message": (getattr(state, "state_message", "") or "")[:300],
            "tasks": tasks,
            # job parameters are deliberately left out: see HIDDEN_COLUMNS
        }

    def list_runs(self, limit: int = 5) -> list[dict]:
        """Most recent runs first."""
        runs = self.w.jobs.list_runs(job_id=self.job_id, expand_tasks=True, limit=min(limit, 25))
        return [self._run_to_dict(r) for r in islice(runs, limit)]

    def task_output(self, run_id: int, task_key: str) -> dict:
        run = self.w.jobs.get_run(run_id)
        task = next((t for t in run.tasks or [] if t.task_key == task_key), None)
        if task is None:
            raise ValueError(f"run {run_id} has no task '{task_key}'; "
                             f"tasks: {[t.task_key for t in run.tasks or []]}")
        out = self.w.jobs.get_run_output(task.run_id)
        notebook = getattr(out, "notebook_output", None)
        return {
            "task_key": task_key,
            "result_state": _val(getattr(getattr(task, "state", None), "result_state", None)),
            "error": getattr(out, "error", None),
            "error_trace_tail": drop_drill_lines(getattr(out, "error_trace", None) or "")[-1500:],
            "notebook_exit_value": getattr(notebook, "result", None),
        }

    def sql(self, statement: str, max_rows: int = 50) -> dict:
        if not self.warehouse_id:
            raise RuntimeError("DATABRICKS_WAREHOUSE_ID is not set")
        api = self.w.statement_execution
        resp = api.execute_statement(statement=statement, warehouse_id=self.warehouse_id,
                                     wait_timeout="30s", row_limit=max_rows)
        deadline = time.time() + 120  # a cold serverless warehouse needs a moment to start
        while _val(resp.status.state) in ("PENDING", "RUNNING") and time.time() < deadline:
            time.sleep(3)
            resp = api.get_statement(resp.statement_id)
        state = _val(resp.status.state)
        if state != "SUCCEEDED":
            message = getattr(getattr(resp.status, "error", None), "message", "") or ""
            raise RuntimeError(f"statement {state}: {message}")
        columns = [c.name for c in resp.manifest.schema.columns]
        keep = [i for i, name in enumerate(columns) if name.lower() not in HIDDEN_COLUMNS]
        rows = getattr(resp.result, "data_array", None) or []
        return {"columns": [columns[i] for i in keep],
                "rows": [[row[i] for i in keep] for row in rows[:max_rows]],
                "row_count": len(rows)}

    def run_now(self, fault: str = "none", wait: bool = True, timeout_min: int = 20) -> dict:
        waiter = self.w.jobs.run_now(job_id=self.job_id, job_parameters={"fault": fault})
        run_id = waiter.response.run_id
        if wait:
            try:
                waiter.result(timeout=timedelta(minutes=timeout_min))
            except Exception:
                pass  # a failed run is an expected outcome here; report its state below
        return self._run_to_dict(self.w.jobs.get_run(run_id))


_WORKER_QUERY = """
query ($account: string!, $script: string!, $since: Time!, $until: Time!) {
  viewer { accounts(filter: {accountTag: $account}) {
    workersInvocationsAdaptive(limit: 100,
        filter: {scriptName: $script, datetime_geq: $since, datetime_leq: $until}) {
      sum { requests errors subrequests }
      dimensions { status }
    } } }
}"""

_ZONE_QUERY = """
query ($zone: string!, $host: string!, $since: Time!, $until: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequestsAdaptiveGroups(limit: 100,
        filter: {clientRequestHTTPHost: $host, datetime_geq: $since, datetime_leq: $until}) {
      count
      dimensions { edgeResponseStatus }
    } } }
}"""


class CloudflareClient:
    API = "https://api.cloudflare.com/client/v4"

    def __init__(self, token: str, account_id: str, zone_id: str, worker_name: str, host: str,
                 http_post=None):
        self.token, self.account_id, self.zone_id = token, account_id, zone_id
        self.worker_name, self.host = worker_name, host
        self._post = http_post or self._default_post

    def _default_post(self, url: str, payload: dict) -> dict:
        import requests

        resp = requests.post(url, json=payload, timeout=20,
                             headers={"authorization": f"Bearer {self.token}"})
        return resp.json()

    def _graphql(self, query: str, variables: dict) -> dict:
        body = self._post(f"{self.API}/graphql", {"query": query, "variables": variables})
        # GraphQL reports failures inside a 200 response, so check the body, not the status.
        if body.get("errors"):
            raise RuntimeError(f"Cloudflare GraphQL error: {body['errors'][0].get('message')}")
        return body["data"]["viewer"]

    @staticmethod
    def _window(minutes: int) -> dict:
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return {"since": (now - timedelta(minutes=minutes)).strftime(fmt), "until": now.strftime(fmt)}

    def worker_metrics(self, minutes: int = 30) -> dict:
        data = self._graphql(_WORKER_QUERY, {"account": self.account_id, "script": self.worker_name,
                                             **self._window(minutes)})
        accounts = data.get("accounts") or [{}]
        groups = accounts[0].get("workersInvocationsAdaptive") or []
        by_status: dict[str, int] = {}
        for g in groups:
            by_status[g["dimensions"]["status"]] = by_status.get(g["dimensions"]["status"], 0) + g["sum"]["requests"]
        return {"window_min": minutes,
                "requests": sum(g["sum"]["requests"] for g in groups),
                "errors": sum(g["sum"]["errors"] for g in groups),
                "by_invocation_status": by_status}

    def zone_status(self, minutes: int = 30) -> dict:
        data = self._graphql(_ZONE_QUERY, {"zone": self.zone_id, "host": self.host,
                                           **self._window(minutes)})
        zones = data.get("zones") or [{}]
        groups = zones[0].get("httpRequestsAdaptiveGroups") or []
        by_status: dict[str, int] = {}
        for g in groups:
            code = str(g["dimensions"]["edgeResponseStatus"])
            by_status[code] = by_status.get(code, 0) + g["count"]
        return {"window_min": minutes, "host": self.host, "requests_by_http_status": by_status}

    def purge(self, urls: list[str] | None = None) -> dict:
        """Purge specific URLs, or everything cached for this hostname when urls is empty."""
        payload = {"files": urls} if urls else {"hosts": [self.host]}
        body = self._post(f"{self.API}/zones/{self.zone_id}/purge_cache", payload)
        if not body.get("success"):
            raise RuntimeError(f"purge failed: {body.get('errors')}")
        return {"purged": payload}
