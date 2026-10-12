import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline" / "src"))

import drill  # noqa: E402
import weatherlib  # noqa: E402


def test_scenarios_are_consistent_with_the_code():
    scenarios = drill.load_scenarios()
    assert len(scenarios) == 10
    worker_source = (drill.ROOT / "edge" / "src" / "index.js").read_text()
    for s in scenarios.values():
        for step in s.get("inject", []):
            if "pipeline" in step:
                assert step["pipeline"] in weatherlib.FAULTS, s["id"]
            if "worker" in step:
                for key, value in step["worker"].items():
                    assert key in worker_source, s["id"]
                    if key == "FAULT_MODE":
                        assert f'"{value}"' in worker_source, s["id"]


def test_grading_requires_an_exact_match():
    assert drill.grade({"root_causes": ["schema_drift"]}, ["schema_drift"])
    assert drill.grade({"root_causes": ["edge_code_error", "schema_drift"]}, ["schema_drift", "edge_code_error"])
    assert not drill.grade({"root_causes": ["schema_drift"]}, ["schema_drift", "edge_code_error"])
    assert not drill.grade({"root_causes": ["schema_drift", "unknown"]}, ["schema_drift"])
    assert not drill.grade(None, ["none"])


def test_scorecard_uses_the_latest_run_of_each_drill():
    base = {"expected": ["none"], "got": ["none"], "detected": True, "steps": 3,
            "input_tokens": 10, "output_tokens": 5, "investigation_s": 7}
    text = drill.scorecard([
        {**base, "scenario": "healthy", "passed": False, "got": ["unknown"]},
        {**base, "scenario": "healthy", "passed": True},
        {**base, "scenario": "wrong_path", "passed": False, "got": None},
    ])
    assert "Diagnosed correctly: 1/2" in text and "no report" in text
