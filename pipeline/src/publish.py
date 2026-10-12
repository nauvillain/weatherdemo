# Databricks notebook source
# Gold/serve: build the JSON documents and push them to the CDN origin (Azure Blob Storage).
import json
from datetime import datetime, timezone

import opslog
import weatherlib as wl

dbutils.widgets.text("fault", "none")
dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "weather")
fault = dbutils.widgets.get("fault")
ns = f"{dbutils.widgets.get('catalog')}.{dbutils.widgets.get('schema')}"
silver = f"{ns}.silver_observations"
ops = f"{ns}.ops_run_log"
batch_id = dbutils.jobs.taskValues.get(taskKey="ingest", key="batch_id", debugValue="")

# Written by Terraform (infra/02-databricks). The job only ever holds a write-scoped SAS.
container_url = dbutils.secrets.get("weather-edge", "origin_container_url")
write_sas = dbutils.secrets.get("weather-edge", "origin_write_sas")

# COMMAND ----------

try:
    rows = [
        {**r.asDict(), "observed_at": wl.utc_iso(r["observed_at"])}
        for r in spark.table(silver).where(f"batch_id = '{batch_id}'").collect()
    ]
    docs = wl.build_documents(rows, datetime.now(timezone.utc))
    result = wl.upload_documents(docs, container_url, write_sas, fault=fault)
except Exception as exc:
    opslog.log_event(spark, ops, batch_id=batch_id, task="publish", status="FAILED",
                     detail=f"{type(exc).__name__}: {exc}")
    raise

summary = {"batch_id": batch_id, "files_written": result["files_written"],
           "target_prefix": result["target_prefix"]}
opslog.log_event(spark, ops, batch_id=batch_id, task="publish", status="OK",
                 detail=json.dumps(summary))
dbutils.notebook.exit(json.dumps(summary))
