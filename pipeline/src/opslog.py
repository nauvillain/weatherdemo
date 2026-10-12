"""Append-only run log in Delta. Gives humans and the agent one table to query
for "what did each task say it did", independent of the Jobs UI."""
from datetime import datetime, timezone

_SCHEMA = "logged_at TIMESTAMP, batch_id STRING, task STRING, status STRING, detail STRING"


def log_event(spark, table: str, *, batch_id: str, task: str, status: str, detail: str) -> None:
    spark.sql(f"CREATE TABLE IF NOT EXISTS {table} ({_SCHEMA})")
    row = (datetime.now(timezone.utc), batch_id, task, status, (detail or "")[:4000])
    spark.createDataFrame([row], _SCHEMA).write.mode("append").saveAsTable(table)
