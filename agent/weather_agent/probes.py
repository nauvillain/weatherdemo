"""HTTP probes: what does a user see at the edge, and what is really at the origin?

Comparing the two is the single most useful move in CDN debugging, so both
the rule-based detector and the agent use these.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

KEEP_HEADERS = ("x-cache", "x-cache-age", "x-edge-ttl", "x-origin-status", "cache-control",
                "age", "last-modified", "content-type", "cf-cache-status", "cf-ray")
REQUIRED_VALUES = ("observed_at", "temperature_c", "humidity_pct", "wind_kmh")


@dataclass
class Probe:
    target: str          # "edge" or "origin"
    path: str
    status: int | None = None
    error: str | None = None
    headers: dict = field(default_factory=dict)
    doc: dict | None = None
    generated_at: str | None = None
    data_age_min: float | None = None
    body_excerpt: str = ""

    def to_dict(self) -> dict:
        out = {"target": self.target, "path": self.path, "status": self.status,
               "headers": self.headers, "generated_at": self.generated_at,
               "data_age_min": self.data_age_min}
        if self.error:
            out["error"] = self.error
        if self.doc is not None:
            out["null_fields"] = null_fields(self.doc)
            out["body"] = self.doc
        elif self.body_excerpt:
            out["body_excerpt"] = self.body_excerpt
        return out


def _default_get(url: str) -> tuple[int, dict, str]:
    import requests

    resp = requests.get(url, timeout=15, headers={"user-agent": "weather-edge-agent/1.0"})
    return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.text


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def probe(target: str, path: str, url: str, *, http_get=None, now: datetime | None = None) -> Probe:
    result = Probe(target=target, path=path)
    now = now or datetime.now(timezone.utc)
    try:
        status, headers, text = (http_get or _default_get)(url)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.status = status
    result.headers = {k: headers[k] for k in KEEP_HEADERS if k in headers}
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        result.doc = doc
        result.generated_at = doc.get("generated_at")
        stamp = parse_time(result.generated_at)
        if stamp:
            result.data_age_min = round((now - stamp).total_seconds() / 60, 1)
    else:
        result.body_excerpt = text[:300]
    return result


def probe_edge(cfg, path: str = "/v1/index.json", **kw) -> Probe:
    return probe("edge", path, f"{cfg.edge_base_url}{path}", **kw)


def probe_origin(cfg, path: str = "/v1/index.json", **kw) -> Probe:
    """Read the blob directly, bypassing the CDN. `path` may also be a non-published
    prefix such as /staging/v1/index.json."""
    url = f"{cfg.origin_container_url.rstrip('/')}/{path.lstrip('/')}?{cfg.origin_read_sas.lstrip('?')}"
    return probe("origin", path, url, **kw)


def null_fields(doc: dict) -> list[str]:
    """Names of required values that are null or missing in a published document."""
    problems = []
    if isinstance(doc.get("locations"), list):
        for loc in doc["locations"]:
            problems += [f"{loc.get('id', '?')}.{f}" for f in REQUIRED_VALUES if loc.get(f) is None]
    elif isinstance(doc.get("current"), dict):
        problems += [f"current.{f}" for f in REQUIRED_VALUES if doc["current"].get(f) is None]
    return problems
