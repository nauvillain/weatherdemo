# Databricks notebook source
# Bronze: fetch raw payloads and store them untouched, so any later bug can be replayed.
import json
import uuid
from datetime import datetime, timezone

import opslog
import weatherlib as wl

dbutils.widgets.text("fault", "none")
dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "weather")
fault = dbutils.widgets.get("fault")
ns = f"{dbutils.widgets.get('catalog')}.{dbutils.widgets.get('schema')}"
bronze = f"{ns}.bronze_observations"
ops = f"{ns}.ops_run_log"
batch_id = uuid.uuid4().hex[:12]
BRONZE_SCHEMA = "batch_id STRING, ingested_at TIMESTAMP, location_id STRING, payload STRING"

# COMMAND ----------

try:
    now = datetime.now(timezone.utc)
    rows = [
        (batch_id, now, loc["id"], json.dumps(wl.fetch_current(loc, fault=fault)))
        for loc in wl.LOCATIONS
    ]
    spark.sql(f"CREATE TABLE IF NOT EXISTS {bronze} ({BRONZE_SCHEMA})")
    spark.createDataFrame(rows, BRONZE_SCHEMA).write.mode("append").saveAsTable(bronze)
except Exception as exc:
    opslog.log_event(spark, ops, batch_id=batch_id, task="ingest", status="FAILED",
                     detail=f"{type(exc).__name__}: {exc}")
    raise

summary = {"batch_id": batch_id, "locations_fetched": len(rows)}
opslog.log_event(spark, ops, batch_id=batch_id, task="ingest", status="OK",
                 detail=json.dumps(summary))
dbutils.jobs.taskValues.set(key="batch_id", value=batch_id)
dbutils.notebook.exit(json.dumps(summary))
