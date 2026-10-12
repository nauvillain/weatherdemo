"""Failure drill harness: break the system on purpose and score the agent.

    python evals/drill.py list
    python evals/drill.py run wrong_path        # inject -> wait -> detect -> investigate -> grade -> restore
    python evals/drill.py run all
    python evals/drill.py restore               # put everything back to a clean state
    python evals/drill.py scorecard             # write evals/SCORECARD.md from results.jsonl

Run from the repository root, with agent/.env filled in. Pipeline drills need
the Databricks job; Worker drills need `npx wrangler` logged in.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from weather_agent import cli, probes, report as reporting  # noqa: E402
from weather_agent.config import Config, load_dotenv  # noqa: E402
from weather_agent.prompts import CATEGORIES  # noqa: E402

RESULTS = ROOT / "evals" / "results.jsonl"
SCORECARD = ROOT / "evals" / "SCORECARD.md"
EDGE_PATHS = ["/v1/index.json", cli.SAMPLE_LOCATION]


def load_scenarios(path: Path = ROOT / "evals" / "scenarios.yaml") -> dict[str, dict]:
    scenarios = yaml.safe_load(path.read_text())
    by_id = {s["id"]: s for s in scenarios}
    if len(by_id) != len(scenarios):
        raise ValueError("duplicate scenario ids")
    for s in scenarios:
        unknown = set(s["expected"]) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"{s['id']}: unknown categories {unknown}")
    return by_id


def grade(report: dict | None, expected: list[str]) -> bool:
    """Pass only if the agent named exactly the true causes: no misses, no extras."""
    if not report:
        return False
    return set(report.get("root_causes", [])) == set(expected)


def wrangler_deploy(overrides: dict | None = None) -> None:
    cmd = ["npx", "wrangler", "deploy"]
    for key, value in (overrides or {}).items():
        cmd += ["--var", f"{key}:{value}"]
    subprocess.run(cmd, cwd=ROOT / "edge", check=True)
    time.sleep(10)  # give the new version a moment to roll out


def inject(scenario: dict, cfg: Config, dbx) -> None:
    for step in scenario.get("inject", []):
        if "pipeline" in step:
            if dbx is None:
                raise SystemExit("this drill needs Databricks (DATABRICKS_JOB_ID is not set)")
            print(f"  running pipeline (variant: {step['pipeline']})")
            run = dbx.run_now(step["pipeline"])
            print(f"  run {run['run_id']} ended {run['result_state']}")
        elif "worker" in step:
            print(f"  redeploying Worker with {step['worker']}")
            wrangler_deploy(step["worker"])
        elif step.get("warm_edge"):
            for path in EDGE_PATHS:
                probes.probe_edge(cfg, path)


def settle(scenario: dict, cfg: Config, dbx, cf) -> list[dict]:
    """Poll the monitor until the symptom this drill should cause is visible."""
    wanted = set(scenario.get("settle", []))
    deadline = time.time() + scenario.get("settle_timeout_s", 300)
    while True:
        findings = cli.run_check(cfg, dbx, cf)
        seen = {f["check"] for f in findings}
        if not wanted or wanted & seen:
            return findings
        if time.time() > deadline:
            print(f"  symptom never appeared (wanted one of {sorted(wanted)}, saw {sorted(seen)})")
            return findings
        print(f"  waiting for {sorted(wanted)} (currently: {sorted(seen) or 'healthy'})")
        time.sleep(30)


def restore(cfg: Config, dbx, cf) -> None:
    print("restoring: clean Worker deploy, clean pipeline run, cache purge")
    wrangler_deploy()
    if dbx is not None:
        dbx.run_now("none")
    if cf is not None:
        try:
            cf.purge()
        except Exception as exc:
            print(f"  purge failed ({exc}); cached copies will age out on their own")
    time.sleep(5)
    for path in EDGE_PATHS:
        probes.probe_edge(cfg, path)
    left = cli.run_check(cfg, dbx, cf)
    print("  clean" if not left else f"  still reporting: {[f['check'] for f in left]}")


def run_scenario(scenario: dict, base_cfg: Config, dbx, cf, keep: bool) -> dict:
    print(f"\n== {scenario['id']}: {scenario['title']}")
    cfg = replace(base_cfg, **scenario.get("monitor", {}))
    started = time.time()
    inject(scenario, cfg, dbx)
    findings = settle(scenario, cfg, dbx, cf)
    print(f"  monitor: {[f['check'] for f in findings] or 'no findings'}")

    investigation_started = time.time()
    result = cli.run_investigation(cfg, findings, dbx=dbx, cf=cf, forced=True, allow_actions=False)
    report_path = reporting.save(reporting.render(findings, result, cfg.model),
                                 ROOT / "evals" / "reports" / scenario["id"])
    passed = grade(result.report, scenario["expected"])
    record = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scenario": scenario["id"],
        "model": cfg.model,
        "expected": scenario["expected"],
        "got": (result.report or {}).get("root_causes"),
        "confidence": (result.report or {}).get("confidence"),
        "passed": passed,
        "monitor_checks": sorted({f["check"] for f in findings}),
        "detected": bool(findings) == (scenario["expected"] != ["none"]),
        "steps": result.steps,
        "stop": result.stop,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "investigation_s": round(time.time() - investigation_started),
        "total_s": round(time.time() - started),
        "report": str(report_path.relative_to(ROOT)),
    }
    with RESULTS.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    print(f"  expected {record['expected']}, got {record['got']} -> {'PASS' if passed else 'FAIL'}")
    if not keep and scenario.get("inject"):
        restore(base_cfg, dbx, cf)
    return record


def scorecard(records: list[dict]) -> str:
    latest: dict[str, dict] = {}
    for r in records:
        latest[r["scenario"]] = r  # keep the most recent run of each scenario
    rows = list(latest.values())
    passed = sum(r["passed"] for r in rows)
    detected = sum(r["detected"] for r in rows)
    lines = [
        "# Agent scorecard", "",
        f"Latest run of each drill. **Diagnosed correctly: {passed}/{len(rows)}.** "
        f"Monitor detected correctly: {detected}/{len(rows)}.", "",
        "| Drill | Expected | Agent said | Result | Steps | Tokens (in/out) | Time (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['scenario']} | {', '.join(r['expected'])} | {', '.join(r['got'] or ['no report'])} | "
            f"{'pass' if r['passed'] else 'FAIL'} | {r['steps']} | "
            f"{r['input_tokens']}/{r['output_tokens']} | {r['investigation_s']} |")
    lines += ["", "Read the failures first. Each one is either a missing tool, a vague tool "
              "description, a gap in the system prompt, or a real limit of the model, and "
              "knowing which is the point of the exercise."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["list", "run", "restore", "scorecard"])
    parser.add_argument("scenario", nargs="?")
    parser.add_argument("--keep", action="store_true", help="leave the fault in place afterwards")
    args = parser.parse_args(argv)
    scenarios = load_scenarios()

    if args.command == "list":
        for s in scenarios.values():
            print(f"{s['id']:<16} {s['title']}")
        return 0
    if args.command == "scorecard":
        records = [json.loads(line) for line in RESULTS.read_text().splitlines() if line.strip()]
        SCORECARD.write_text(scorecard(records))
        print(SCORECARD.read_text())
        return 0

    load_dotenv(ROOT / "agent" / ".env")
    cfg = Config.from_env()
    dbx, cf = cli.build_clients(cfg)
    if args.command == "restore":
        restore(cfg, dbx, cf)
        return 0
    if not args.scenario:
        parser.error("run needs a scenario id or 'all'")
    chosen = list(scenarios.values()) if args.scenario == "all" else [scenarios[args.scenario]]
    records = [run_scenario(s, cfg, dbx, cf, args.keep) for s in chosen]
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
