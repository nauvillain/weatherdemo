"""Detection: plain, deterministic rules. No model involved.

Rules are cheap, fast, and never hallucinate, so they decide WHETHER
something is wrong. The agent is only asked WHY. Keeping those two jobs
separate is the main design decision in this project.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .probes import Probe, null_fields, parse_time


@dataclass
class Finding:
    check: str
    severity: str   # "critical" or "warning"
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(edge_probes: list[Probe], origin: Probe | None = None, runs: list[dict] | None = None,
             metrics: dict | None = None, *, max_age_min: float = 90, behind_slack_min: float = 10,
             now=None) -> list[Finding]:
    findings: list[Finding] = []

    for p in edge_probes:
        if p.error:
            findings.append(Finding("edge_unreachable", "critical", f"{p.path}: {p.error}"))
            continue
        if p.status != 200:
            findings.append(Finding("edge_http_error", "critical",
                                    f"{p.path} returned HTTP {p.status}"))
            continue
        if p.headers.get("x-cache") == "STALE":
            findings.append(Finding(
                "edge_serving_stale_fallback", "warning",
                f"{p.path} served from stale fallback; origin status "
                f"{p.headers.get('x-origin-status', 'unknown')}"))
        if p.data_age_min is None:
            findings.append(Finding("edge_bad_payload", "critical",
                                    f"{p.path} has no readable generated_at"))
        elif p.data_age_min > max_age_min:
            findings.append(Finding(
                "edge_stale", "critical",
                f"{p.path} data is {p.data_age_min:.0f} min old (limit {max_age_min:.0f})"))
        nulls = null_fields(p.doc or {})
        if nulls:
            findings.append(Finding("data_quality", "critical",
                                    f"{p.path} at edge has null values: {', '.join(nulls[:8])}"))

    if origin is not None:
        if origin.error or origin.status != 200:
            findings.append(Finding("origin_unreadable", "critical",
                                    f"origin {origin.path}: {origin.error or f'HTTP {origin.status}'}"))
        else:
            if origin.data_age_min is not None and origin.data_age_min > max_age_min:
                findings.append(Finding(
                    "origin_stale", "critical",
                    f"origin data is {origin.data_age_min:.0f} min old (limit {max_age_min:.0f})"))
            nulls = null_fields(origin.doc or {})
            if nulls:
                findings.append(Finding("data_quality", "critical",
                                        f"origin has null values: {', '.join(nulls[:8])}"))
            index = next((p for p in edge_probes if p.path == origin.path and p.status == 200), None)
            edge_time = parse_time(index.generated_at) if index else None
            origin_time = parse_time(origin.generated_at)
            if edge_time and origin_time:
                lag = (origin_time - edge_time).total_seconds() / 60
                if lag > behind_slack_min:
                    findings.append(Finding(
                        "edge_behind_origin", "warning",
                        f"edge copy is {lag:.0f} min older than the origin copy"))

    if runs:
        finished = [r for r in runs if r.get("end_time")]
        if finished and finished[0].get("result_state") != "SUCCESS":
            last = finished[0]
            failed = [t["task_key"] for t in last.get("tasks", [])
                      if t.get("result_state") not in ("SUCCESS", None)]
            findings.append(Finding(
                "job_failed", "critical",
                f"run {last['run_id']} ended {last.get('result_state')}"
                + (f"; failing task(s): {', '.join(failed)}" if failed else "")))

    if metrics and metrics.get("requests", 0) >= 20:
        rate = metrics.get("errors", 0) / metrics["requests"]
        if rate > 0.05:
            findings.append(Finding(
                "edge_error_rate", "warning",
                f"Worker error rate {rate:.0%} over {metrics['requests']} requests"))

    return findings
