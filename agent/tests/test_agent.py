"""Unit tests for the parts that must not be wrong: detection rules, guardrails,
and the loop's control flow. The model is replaced by a scripted fake, so these
run offline and cost nothing."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from weather_agent import probes
from weather_agent.clients import HIDDEN_COLUMNS, CloudflareClient, DatabricksClient
from weather_agent.config import Config
from weather_agent.detect import evaluate
from weather_agent.guards import GuardError, check_readonly_sql, drop_drill_lines, redact
from weather_agent.loop import run_agent
from weather_agent.tools import Tool, ToolRegistry, build_registry

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def index_doc(minutes_ago: float, temp=12.5) -> dict:
    return {"generated_at": iso(minutes_ago),
            "locations": [{"id": "chicago", "observed_at": iso(minutes_ago), "temperature_c": temp,
                           "humidity_pct": 60.0, "wind_kmh": 10.0}]}


def make_probe(target="edge", path="/v1/index.json", minutes_ago=5.0, status=200, headers=None, temp=12.5):
    import json

    doc = index_doc(minutes_ago, temp)
    return probes.probe(target, path, "https://x", now=NOW,
                        http_get=lambda _url: (status, headers or {"x-cache": "HIT"}, json.dumps(doc)))


def checks(findings):
    return sorted({f.check for f in findings})


# ---- detection rules -------------------------------------------------------

def test_healthy_system_has_no_findings():
    runs = [{"run_id": 1, "end_time": iso(5), "result_state": "SUCCESS", "tasks": []}]
    assert evaluate([make_probe()], make_probe("origin"), runs, {"requests": 100, "errors": 0}) == []


def test_stale_edge_and_origin():
    found = evaluate([make_probe(minutes_ago=200)], make_probe("origin", minutes_ago=200))
    assert checks(found) == ["edge_stale", "origin_stale"]


def test_edge_behind_fresh_origin():
    found = evaluate([make_probe(minutes_ago=60)], make_probe("origin", minutes_ago=2))
    assert checks(found) == ["edge_behind_origin"]


def test_stale_fallback_and_http_error_and_unreachable():
    stale = make_probe(headers={"x-cache": "STALE", "x-origin-status": "403"})
    assert checks(evaluate([stale])) == ["edge_serving_stale_fallback"]
    assert "403" in evaluate([stale])[0].detail
    assert checks(evaluate([make_probe(status=500)])) == ["edge_http_error"]

    def boom(_url):
        raise ConnectionError("dns failure")

    assert checks(evaluate([probes.probe("edge", "/v1/index.json", "https://x", http_get=boom)])) == ["edge_unreachable"]


def test_null_values_are_a_data_quality_finding():
    found = evaluate([make_probe(temp=None)], make_probe("origin", temp=None))
    assert checks(found) == ["data_quality"]
    assert "chicago.temperature_c" in found[0].detail


def test_failed_job_names_the_failing_task_and_ignores_runs_in_progress():
    runs = [
        {"run_id": 3, "end_time": None, "result_state": None, "tasks": []},
        {"run_id": 2, "end_time": iso(4), "result_state": "FAILED",
         "tasks": [{"task_key": "ingest", "result_state": "SUCCESS"},
                   {"task_key": "transform", "result_state": "FAILED"}]},
    ]
    found = evaluate([make_probe()], None, runs)
    assert checks(found) == ["job_failed"] and "transform" in found[0].detail


def test_error_rate_needs_enough_traffic():
    assert evaluate([make_probe()], metrics={"requests": 5, "errors": 5}) == []
    assert checks(evaluate([make_probe()], metrics={"requests": 50, "errors": 10})) == ["edge_error_rate"]


# ---- guardrails ------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM main.weather.ops_run_log ORDER BY logged_at DESC LIMIT 5",
    "with x as (select 1) select * from x;",
    "DESCRIBE system.lakeflow.job_run_timeline",
    "select replace(detail, 'a', 'b') from t where task = 'drop table; update'",
    "-- note\nSELECT 1",
])
def test_readonly_sql_allows_queries(sql):
    assert check_readonly_sql(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE main.weather.silver_observations",
    "SELECT 1; DELETE FROM t",
    "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "select 1 /* sneaky */ ; update t set a = 1",
    "",
    "GRANT ALL ON t TO someone",
])
def test_readonly_sql_blocks_writes(sql):
    with pytest.raises(GuardError):
        check_readonly_sql(sql)


def test_redaction_and_drill_hygiene():
    url = "https://a.blob.core.windows.net/weather/v1/index.json?sv=2024-11-04&se=2026-12-31&sp=rl&sig=abc%2Bdef%3D"
    cleaned = redact(f"GET {url} failed; response=403 base=1")
    assert "abc%2Bdef" not in cleaned and "sig=REDACTED" in cleaned
    assert "response=403" in cleaned and "base=1" in cleaned
    trace = 'File "weatherlib.py"\n    if fault == "upstream_down":\nUpstreamError: HTTP 503'
    assert "upstream_down" not in drop_drill_lines(trace) and "HTTP 503" in drop_drill_lines(trace)


# ---- client wrappers (fake SDK objects) ------------------------------------

def fake_workspace():
    state = SimpleNamespace(life_cycle_state=SimpleNamespace(value="TERMINATED"),
                            result_state=SimpleNamespace(value="FAILED"), state_message="boom")
    task = SimpleNamespace(task_key="transform", run_id=77, state=state)
    run = SimpleNamespace(run_id=9, start_time=1_760_000_000_000, end_time=1_760_000_090_000, state=state,
                          status=None, tasks=[task], job_parameters=[SimpleNamespace(name="fault", value="schema_drift")])
    out = SimpleNamespace(error="SchemaError: missing field", notebook_output=None,
                          error_trace='line 1\n  wl.fetch_current(loc, fault=fault)\nSchemaError: missing field')
    stmt = SimpleNamespace(
        statement_id="s1", status=SimpleNamespace(state=SimpleNamespace(value="SUCCEEDED"), error=None),
        manifest=SimpleNamespace(schema=SimpleNamespace(columns=[SimpleNamespace(name="run_id"),
                                                                 SimpleNamespace(name="job_parameters")])),
        result=SimpleNamespace(data_array=[["9", '{"fault":"schema_drift"}']]))
    jobs = SimpleNamespace(list_runs=lambda **_kw: iter([run, run, run]), get_run=lambda _id: run,
                           get_run_output=lambda _id: out)
    return SimpleNamespace(jobs=jobs, statement_execution=SimpleNamespace(
        execute_statement=lambda **_kw: stmt, get_statement=lambda _id: stmt))


def test_databricks_wrapper_never_exposes_the_drill_switch():
    dbx = DatabricksClient(job_id=1, warehouse_id="w", workspace=fake_workspace())
    runs = dbx.list_runs(2)
    assert len(runs) == 2 and runs[0]["result_state"] == "FAILED" and runs[0]["duration_s"] == 90
    assert "schema_drift" not in str(runs)
    output = dbx.task_output(9, "transform")
    assert output["error"].startswith("SchemaError") and "fault" not in output["error_trace_tail"]
    table = dbx.sql("select * from system.lakeflow.job_run_timeline")
    assert table["columns"] == ["run_id"] and table["rows"] == [["9"]]
    assert "job_parameters" in HIDDEN_COLUMNS
    with pytest.raises(ValueError):
        dbx.task_output(9, "nope")


def test_cloudflare_wrapper_sums_metrics_and_surfaces_graphql_errors():
    ok = {"data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": [
        {"sum": {"requests": 40, "errors": 0, "subrequests": 3}, "dimensions": {"status": "success"}},
        {"sum": {"requests": 10, "errors": 10, "subrequests": 0}, "dimensions": {"status": "scriptThrewException"}},
    ]}]}}}
    cf = CloudflareClient("t", "acct", "zone", "weather-edge", "weather.vilnico.com", http_post=lambda _u, _p: ok)
    metrics = cf.worker_metrics(30)
    assert metrics["requests"] == 50 and metrics["errors"] == 10
    bad = CloudflareClient("t", "a", "z", "w", "h", http_post=lambda _u, _p: {"errors": [{"message": "no access"}]})
    with pytest.raises(RuntimeError, match="no access"):
        bad.worker_metrics()
    sent = {}
    purger = CloudflareClient("t", "a", "z", "w", "weather.vilnico.com",
                              http_post=lambda _u, p: sent.update(p) or {"success": True})
    purger.purge()
    assert sent == {"hosts": ["weather.vilnico.com"]}


# ---- tool registry and the loop ---------------------------------------------

def test_registry_grows_with_configuration_and_hides_actions_by_default():
    bare = Config(edge_base_url="https://weather.vilnico.com")
    assert build_registry(bare).names() == ["probe_edge", "submit_report"]
    full = Config(edge_base_url="https://weather.vilnico.com", origin_container_url="https://o/c",
                  origin_read_sas="sig=x", databricks_job_id=1, databricks_warehouse_id="w")
    read_only = build_registry(full, dbx=object(), cf=object()).names()
    assert "query_lakehouse" in read_only and "rerun_pipeline" not in read_only
    with_actions = build_registry(full, dbx=object(), cf=object(), allow_actions=True).names()
    assert {"rerun_pipeline", "purge_edge_cache"} <= set(with_actions)


def block(name, args, id_="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


class ScriptedModel:
    """Stands in for the Anthropic client: returns canned responses in order."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        # Snapshot the conversation: the loop keeps appending to the same list afterwards.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        content = self.turns.pop(0)
        return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=100, output_tokens=20))


REPORT = {"status": "incident", "root_causes": ["schema_drift"], "affected_layer": "pipeline",
          "summary": "s", "evidence": ["e"], "user_impact": "u", "recommended_actions": ["a"],
          "confidence": "high"}


def registry_with(calls: list):
    reg = ToolRegistry()
    reg.add(Tool("look", "read something", {"type": "object", "properties": {}},
                 lambda: calls.append("look") or {"ok": True}))
    reg.add(Tool("explode", "always fails", {"type": "object", "properties": {}},
                 lambda: 1 / 0))
    reg.add(Tool("fix", "change something", {"type": "object", "properties": {}},
                 lambda: calls.append("fix") or {"done": True}, requires_approval=True))
    reg.add(Tool("submit_report", "final", {"type": "object", "properties": {}}))
    return reg


def test_loop_runs_tools_then_stops_on_report():
    calls = []
    model = ScriptedModel([block("look", {})], [block("submit_report", REPORT, "t2")])
    result = run_agent(model, "m", registry_with(calls), [{"check": "job_failed", "severity": "critical", "detail": "d"}])
    assert result.stop == "report" and result.report["root_causes"] == ["schema_drift"]
    assert calls == ["look"] and result.steps == 2 and result.input_tokens == 200
    second_request = model.requests[1]["messages"]
    assert second_request[-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t1",
                                                "content": '{"ok": true}', "is_error": False}


def test_actions_are_denied_unless_a_human_approves():
    calls = []
    model = ScriptedModel([block("fix", {})], [block("submit_report", REPORT, "t2")])
    run_agent(model, "m", registry_with(calls), [])
    assert calls == []
    assert "declined" in model.requests[1]["messages"][-1]["content"][0]["content"]

    model = ScriptedModel([block("fix", {})], [block("submit_report", REPORT, "t2")])
    run_agent(model, "m", registry_with(calls), [], approve=lambda _n, _a: True)
    assert calls == ["fix"]


def test_tool_failures_go_back_to_the_model_as_errors():
    model = ScriptedModel([block("explode", {}), block("nonexistent", {}, "t9")],
                          [block("submit_report", REPORT, "t2")])
    result = run_agent(model, "m", registry_with([]), [])
    results = model.requests[1]["messages"][-1]["content"]
    assert [r["is_error"] for r in results] == [True, True]
    assert "ZeroDivisionError" in results[0]["content"] and result.stop == "report"


def test_loop_stops_at_the_step_limit_and_nudges_once_for_a_report():
    spinning = ScriptedModel(*[[block("look", {})] for _ in range(3)])
    assert run_agent(spinning, "m", registry_with([]), [], max_steps=3).stop == "max_steps"

    prose = SimpleNamespace(type="text", text="I think it is fine.")
    chatty = ScriptedModel([prose], [prose])
    result = run_agent(chatty, "m", registry_with([]), [])
    assert result.stop == "no_report" and result.report is None
    assert "submit_report" in chatty.requests[1]["messages"][-1]["content"]
