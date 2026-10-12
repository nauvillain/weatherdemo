"""Configuration from environment variables (optionally a local .env file).

Every integration is optional except the edge URL. Missing pieces just mean
fewer checks and fewer tools, which lets you run the agent on day one with
only the CDN in place and grow it as the Databricks side comes online.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:  # blank entries must not shadow SDK defaults such as a CLI login
            os.environ.setdefault(key.strip(), value)


def _get(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Config:
    edge_base_url: str
    origin_container_url: str | None = None
    origin_read_sas: str | None = None
    databricks_job_id: int | None = None
    databricks_warehouse_id: str | None = None
    catalog: str = "main"
    schema: str = "weather"
    cf_api_token: str | None = None
    cf_account_id: str | None = None
    cf_zone_id: str | None = None
    worker_name: str = "weather-edge"
    model: str = "claude-sonnet-5-5"
    max_data_age_min: float = 90.0
    behind_slack_min: float = 10.0
    slack_webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        job_id = _get("DATABRICKS_JOB_ID")
        return cls(
            edge_base_url=(_get("EDGE_BASE_URL") or "https://weather.vilnico.com").rstrip("/"),
            origin_container_url=_get("ORIGIN_CONTAINER_URL"),
            origin_read_sas=_get("ORIGIN_READ_SAS"),
            databricks_job_id=int(job_id) if job_id else None,
            databricks_warehouse_id=_get("DATABRICKS_WAREHOUSE_ID"),
            catalog=_get("WEATHER_CATALOG") or "main",
            schema=_get("WEATHER_SCHEMA") or "weather",
            cf_api_token=_get("CLOUDFLARE_API_TOKEN"),
            cf_account_id=_get("CLOUDFLARE_ACCOUNT_ID"),
            cf_zone_id=_get("CLOUDFLARE_ZONE_ID"),
            worker_name=_get("WORKER_NAME") or "weather-edge",
            model=_get("AGENT_MODEL") or "claude-sonnet-5-5",
            max_data_age_min=float(_get("MAX_DATA_AGE_MIN") or 90),
            behind_slack_min=float(_get("BEHIND_SLACK_MIN") or 10),
            slack_webhook_url=_get("SLACK_WEBHOOK_URL"),
        )

    @property
    def edge_host(self) -> str:
        return urlparse(self.edge_base_url).netloc

    @property
    def has_origin(self) -> bool:
        return bool(self.origin_container_url and self.origin_read_sas)

    @property
    def has_databricks(self) -> bool:
        return self.databricks_job_id is not None

    @property
    def has_cloudflare(self) -> bool:
        return bool(self.cf_api_token and self.cf_account_id and self.cf_zone_id)
