"""Run the pipeline's fetch -> parse -> publish path from a laptop, no Databricks needed.

Use it to seed the origin on day one (so the CDN can be built before the trial
starts) and as a fallback demo if the trial has ended by interview day.

    export ORIGIN_CONTAINER_URL=$(terraform -chdir=infra/01-origin output -raw container_url)
    export ORIGIN_WRITE_SAS=$(terraform -chdir=infra/01-origin output -raw write_sas)
    python pipeline/local_publish.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
import weatherlib as wl  # noqa: E402


def main() -> int:
    container_url = os.environ["ORIGIN_CONTAINER_URL"]
    write_sas = os.environ["ORIGIN_WRITE_SAS"]
    fault = os.environ.get("FAULT", "none")
    rows = [wl.parse_observation(loc["id"], wl.fetch_current(loc, fault=fault))
            for loc in wl.LOCATIONS]
    rows = wl.apply_transform_fault(rows, fault)
    if fault != "null_values":
        wl.check_quality(rows)
    result = wl.upload_documents(wl.build_documents(rows, datetime.now(timezone.utc)),
                                 container_url, write_sas, fault=fault)
    print(f"published {result['files_written']} files under {result['target_prefix']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
