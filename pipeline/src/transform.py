# Databricks notebook source
# Silver: parse the batch into typed rows and run the quality gate.
import json
from datetime import datetime, timezone

import opslog
import weatherlib as wl

dbutils.widgets.text("fault", "none")
dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "weather")
fault = dbutils.widgets.get("fault")
ns = f"{dbutils.widgets.get('catalog')}.{dbutils.widgets.get('schema')}"
bronze = f"{ns}.bronze_observations"
silver = f"{ns}.silver_observations"
ops = f"{ns}.ops_run_log"
batch_id = dbutils.jobs.taskValues.get(taskKey="ingest", key="batch_id", debugValue="")
SILVER_SCHEMA = ("batch_id STRING, location_id STRING, observed_at TIMESTAMP, temperature_c DOUBLE, "
                 "humidity_pct DOUBLE, wind_kmh DOUBLE, weather_code INT, processed_at TIMESTAMP")

# COMMAND ----------

try:
    raw = spark.table(bronze).where(f"batch_id = '{batch_id}'").collect()
    parsed = [wl.parse_observation(r["location_id"], json.loads(r["payload"])) for r in raw]
    parsed = wl.apply_transform_fault(parsed, fault)
    if fault != "null_values":
        wl.check_quality(parsed)
    now = datetime.now(timezone.utc)
    rows = [
        (batch_id, p["location_id"],
         datetime.fromisoformat(p["observed_at"].replace("Z", "+00:00")),
         p["temperature_c"], p["humidity_pct"], p["wind_kmh"], p["weather_code"], now)
        for p in parsed
    ]
    spark.sql(f"CREATE TABLE IF NOT EXISTS {silver} ({SILVER_SCHEMA})")
    spark.createDataFrame(rows, SILVER_SCHEMA).write.mode("append").saveAsTable(silver)
except Exception as exc:
    opslog.log_event(spark, ops, batch_id=batch_id, task="transform", status="FAILED",
                     detail=f"{type(exc).__name__}: {exc}")
    raise

summary = {"batch_id": batch_id, "rows_written": len(rows)}
opslog.log_event(spark, ops, batch_id=batch_id, task="transform", status="OK",
                 detail=json.dumps(summary))
dbutils.notebook.exit(json.dumps(summary))
