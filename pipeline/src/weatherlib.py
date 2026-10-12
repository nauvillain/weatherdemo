"""Core pipeline logic: fetch -> parse -> quality gate -> build documents -> upload.

Deliberately free of Spark and dbutils so the same code runs in three places:
a Databricks notebook task, a laptop (local_publish.py), and unit tests.

The `fault` arguments exist for failure drills (see evals/). They change what
the code does, never what it logs, so an investigator has to find the cause
from evidence instead of reading a label.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

API_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = ("temperature_2m", "relative_humidity_2m", "wind_speed_10m", "weather_code")
SCHEMA_VERSION = 1
PUBLISH_PREFIX = "v1/"
FAULTS = ("none", "upstream_down", "schema_drift", "null_values", "wrong_path", "publish_denied")

LOCATIONS = [
    {"id": "state-college", "name": "State College, PA, USA", "lat": 40.79, "lon": -77.86},
    {"id": "taipei", "name": "Taipei, Taiwan", "lat": 25.03, "lon": 121.57},
    {"id": "paris", "name": "Paris, France", "lat": 48.86, "lon": 2.35},
    {"id": "morzine", "name": "Morzine, France", "lat": 46.18, "lon": 6.71},
    {"id": "tokyo", "name": "Tokyo, Japan", "lat": 35.68, "lon": 139.69},
    {"id": "new-york", "name": "New York City, NY, USA", "lat": 40.71, "lon": -74.01},
]
_BY_ID = {loc["id"]: loc for loc in LOCATIONS}


class UpstreamError(RuntimeError):
    """The weather API could not be reached or kept returning errors."""


class SchemaError(ValueError):
    """The upstream payload no longer has the shape the pipeline expects."""


class QualityError(ValueError):
    """Parsed rows failed the quality gate and must not be published."""


class PublishError(RuntimeError):
    """The origin rejected an upload."""


def utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_get(url: str, params: dict) -> tuple[int, str]:
    import requests

    resp = requests.get(url, params=params, timeout=15)
    return resp.status_code, resp.text


def _default_put(url: str, data: bytes, headers: dict) -> tuple[int, str]:
    import requests

    resp = requests.put(url, data=data, headers=headers, timeout=20)
    return resp.status_code, resp.text


def fetch_current(loc: dict, *, http_get=None, attempts: int = 3, backoff_s: float = 2.0,
                  fault: str = "none") -> dict:
    """Fetch current conditions for one location, with bounded retries."""
    if fault == "upstream_down":
        raise UpstreamError(
            f"GET {API_URL} for {loc['id']} failed after {attempts} attempts: "
            "HTTP 503 Service Unavailable"
        )
    http_get = http_get or _default_get
    params = {
        "latitude": loc["lat"],
        "longitude": loc["lon"],
        "current": ",".join(CURRENT_FIELDS),
        "timezone": "UTC",
    }
    last_error = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            status, body = http_get(API_URL, params)
            if status == 200:
                payload = json.loads(body)
                break
            last_error = f"HTTP {status}"
        except Exception as exc:  # network errors, bad JSON
            last_error = repr(exc)
        if attempt < attempts:
            time.sleep(backoff_s * attempt)
    else:
        raise UpstreamError(
            f"GET {API_URL} for {loc['id']} failed after {attempts} attempts: {last_error}"
        )

    if fault == "schema_drift":
        current = payload.get("current")
        if isinstance(current, dict) and "temperature_2m" in current:
            current["temperature"] = current.pop("temperature_2m")
    return payload


def _num(value):
    return None if value is None else float(value)


def parse_observation(location_id: str, payload: dict) -> dict:
    """Turn one raw API payload into a typed row. Fails loudly on schema changes."""
    current = payload.get("current") if isinstance(payload, dict) else None
    if not isinstance(current, dict):
        keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise SchemaError(f"{location_id}: payload has no 'current' object (top level: {keys})")
    missing = [f for f in ("time", *CURRENT_FIELDS) if f not in current]
    if missing:
        raise SchemaError(
            f"{location_id}: upstream payload is missing expected field(s) {missing}; "
            f"fields present: {sorted(current)}"
        )
    observed = datetime.fromisoformat(str(current["time"]).replace("Z", "+00:00"))
    code = current["weather_code"]
    return {
        "location_id": location_id,
        "observed_at": utc_iso(observed),
        "temperature_c": _num(current["temperature_2m"]),
        "humidity_pct": _num(current["relative_humidity_2m"]),
        "wind_kmh": _num(current["wind_speed_10m"]),
        "weather_code": None if code is None else int(code),
    }


def apply_transform_fault(rows: list[dict], fault: str) -> list[dict]:
    if fault == "null_values":
        return [{**row, "temperature_c": None} for row in rows]
    return rows


def check_quality(rows: list[dict]) -> None:
    """The gate between silver and publish. Raise instead of shipping bad data."""
    if not rows:
        raise QualityError("no rows to publish")
    problems = []
    for row in rows:
        temp = row.get("temperature_c")
        if temp is None:
            problems.append(f"{row['location_id']}: temperature_c is null")
        elif not -90.0 <= temp <= 60.0:
            problems.append(f"{row['location_id']}: temperature_c {temp} out of range")
    if problems:
        raise QualityError("; ".join(problems))


def build_documents(rows: list[dict], generated_at: datetime) -> dict[str, dict]:
    """Build the JSON documents the CDN serves, keyed by path under the container."""
    stamp = utc_iso(generated_at)
    docs: dict[str, dict] = {}
    summaries = []
    for row in sorted(rows, key=lambda r: r["location_id"]):
        loc = _BY_ID.get(row["location_id"], {"id": row["location_id"]})
        current = {k: row.get(k) for k in
                   ("observed_at", "temperature_c", "humidity_pct", "wind_kmh", "weather_code")}
        docs[f"{PUBLISH_PREFIX}current/{row['location_id']}.json"] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": stamp,
            "location": loc,
            "current": current,
        }
        summaries.append({"id": row["location_id"], "name": loc.get("name"), **current})
    docs[f"{PUBLISH_PREFIX}index.json"] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": stamp,
        "count": len(summaries),
        "locations": summaries,
    }
    return docs


def upload_documents(docs: dict[str, dict], container_url: str, sas: str, *, http_put=None,
                     fault: str = "none", max_age_s: int = 60) -> dict:
    """PUT each document to Azure Blob Storage using a SAS token.

    Uses the plain Blob REST API so the notebook needs no extra packages.
    """
    http_put = http_put or _default_put
    sas = sas.lstrip("?")
    target_prefix = ""
    if fault == "wrong_path":
        target_prefix = "staging/"
    if fault == "publish_denied":
        sas = "&".join("sig=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA%3D" if part.startswith("sig=")
                       else part for part in sas.split("&"))
    headers = {
        "x-ms-blob-type": "BlockBlob",
        "x-ms-blob-content-type": "application/json; charset=utf-8",
        "x-ms-blob-cache-control": f"public, max-age={max_age_s}",
    }
    written = []
    for path, doc in docs.items():
        blob_path = f"{target_prefix}{path}"
        url = f"{container_url.rstrip('/')}/{blob_path}?{sas}"
        status, body = http_put(url, json.dumps(doc, separators=(",", ":")).encode("utf-8"), headers)
        if status not in (200, 201):
            raise PublishError(
                f"PUT {container_url.rstrip('/')}/{blob_path} returned HTTP {status}: {body[:300]}"
            )
        written.append(blob_path)
    return {"files_written": len(written), "target_prefix": f"{target_prefix}{PUBLISH_PREFIX}",
            "paths": written}
