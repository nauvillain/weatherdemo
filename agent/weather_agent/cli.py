"""Command line entry point.

    python -m weather_agent check                  # rules only, no model, exit 2 if unhealthy
    python -m weather_agent investigate            # rules, then the agent if anything fired
    python -m weather_agent investigate --force    # run the agent even when healthy
    python -m weather_agent investigate --allow-actions   # offer the two approval-gated actions
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

from . import probes, report as reporting
from .clients import CloudflareClient, DatabricksClient
from .config import Config, load_dotenv
from .detect import evaluate
from .loop import AgentResult, deny_all, run_agent
from .tools import build_registry

SAMPLE_LOCATION = "/v1/current/state-college.json"


def build_clients(cfg: Config):
    dbx = DatabricksClient(cfg.databricks_job_id, cfg.databricks_warehouse_id) if cfg.has_databricks else None
    cf = (CloudflareClient(cfg.cf_api_token, cfg.cf_account_id, cfg.cf_zone_id, cfg.worker_name, cfg.edge_host)
          if cfg.has_cloudflare else None)
    return dbx, cf


def run_check(cfg: Config, dbx=None, cf=None) -> list[dict]:
    """Collect signals and apply the rules. Signal sources that fail are reported, not fatal."""
    edge = [probes.probe_edge(cfg, "/v1/index.json"), probes.probe_edge(cfg, SAMPLE_LOCATION)]
    origin = probes.probe_origin(cfg, "/v1/index.json") if cfg.has_origin else None
    runs = metrics = None
    notes = []
    if dbx is not None:
        try:
            runs = dbx.list_runs(5)
        except Exception as exc:
            notes.append({"check": "monitor_blind_spot", "severity": "warning",
                          "detail": f"could not read Databricks job runs: {exc}"})
    if cf is not None:
        try:
            metrics = cf.worker_metrics(30)
        except Exception as exc:
            notes.append({"check": "monitor_blind_spot", "severity": "warning",
                          "detail": f"could not read Cloudflare metrics: {exc}"})
    findings = evaluate(edge, origin, runs, metrics, max_age_min=cfg.max_data_age_min,
                        behind_slack_min=cfg.behind_slack_min)
    return [f.to_dict() for f in findings] + notes


def ask_operator(name: str, args: dict) -> bool:
    """The human approval gate. No terminal means no approval."""
    if not sys.stdin.isatty():
        return False
    answer = input(f"\nThe agent wants to run {name}({json.dumps(args)}). Allow? [y/N] ")
    return answer.strip().lower() == "y"


def run_investigation(cfg: Config, findings: list[dict], *, dbx=None, cf=None, forced: bool = False,
                      allow_actions: bool = False, client=None, verbose: bool = True) -> AgentResult:
    if client is None:
        import anthropic

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    registry = build_registry(cfg, dbx, cf, allow_actions=allow_actions)
    return run_agent(client, cfg.model, registry, findings, forced=forced,
                     approve=ask_operator if allow_actions else deny_all,
                     on_event=(lambda msg: print(f"  {msg}", file=sys.stderr)) if verbose else (lambda _m: None))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weather_agent")
    parser.add_argument("command", choices=["check", "investigate"])
    parser.add_argument("--force", action="store_true", help="investigate even with no findings")
    parser.add_argument("--allow-actions", action="store_true",
                        help="offer rerun/purge tools; each call still needs your approval")
    parser.add_argument("--max-age-min", type=float, help="override the data freshness limit")
    args = parser.parse_args(argv)

    load_dotenv()
    cfg = Config.from_env()
    if args.max_age_min is not None:
        cfg = replace(cfg, max_data_age_min=args.max_age_min)
    dbx, cf = build_clients(cfg)

    findings = run_check(cfg, dbx, cf)
    if not findings:
        print("healthy: no findings")
    for f in findings:
        print(f"[{f['severity']}] {f['check']}: {f['detail']}")
    if args.command == "check":
        return 2 if findings else 0
    if not findings and not args.force:
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\nThe monitor ran, but the agent needs an Anthropic API key.\n"
              "Create one at https://console.anthropic.com/settings/keys and add it to "
              "agent/.env as ANTHROPIC_API_KEY=...")
        return 1

    result = run_investigation(cfg, findings, dbx=dbx, cf=cf, forced=args.force,
                               allow_actions=args.allow_actions)
    path = reporting.save(reporting.render(findings, result, cfg.model))
    print(f"\nreport: {path}")
    if result.report:
        print(f"{result.report['status']}: {', '.join(result.report['root_causes'])} "
              f"({result.report['confidence']} confidence)\n{result.report['summary']}")
        if cfg.slack_webhook_url:
            reporting.post_to_slack(cfg.slack_webhook_url, result)
    return 2 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
