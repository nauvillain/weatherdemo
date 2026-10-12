import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import weatherlib as wl  # noqa: E402

LOC = wl.LOCATIONS[0]
PAYLOAD = {"latitude": 40.79, "longitude": -77.86, "timezone": "GMT",
           "current": {"time": "2026-10-09T16:45", "interval": 900, "temperature_2m": 14.2,
                       "relative_humidity_2m": 61, "wind_speed_10m": 9.4, "weather_code": 3}}


def ok_get(_url, _params):
    return 200, json.dumps(PAYLOAD)


def test_fetch_parse_roundtrip():
    row = wl.parse_observation(LOC["id"], wl.fetch_current(LOC, http_get=ok_get))
    assert row == {"location_id": "state-college", "observed_at": "2026-10-09T16:45:00Z",
                   "temperature_c": 14.2, "humidity_pct": 61.0, "wind_kmh": 9.4, "weather_code": 3}


def test_fetch_retries_then_raises():
    calls = []

    def flaky(_url, _params):
        calls.append(1)
        return (503, "busy") if len(calls) < 3 else (200, json.dumps(PAYLOAD))

    assert wl.fetch_current(LOC, http_get=flaky, backoff_s=0)["current"]["weather_code"] == 3
    with pytest.raises(wl.UpstreamError, match="HTTP 500"):
        wl.fetch_current(LOC, http_get=lambda _u, _p: (500, ""), backoff_s=0)


def test_faults_fail_the_way_real_incidents_do():
    with pytest.raises(wl.UpstreamError, match="503"):
        wl.fetch_current(LOC, http_get=ok_get, fault="upstream_down")
    drifted = wl.fetch_current(LOC, http_get=ok_get, fault="schema_drift")
    with pytest.raises(wl.SchemaError, match="temperature_2m"):
        wl.parse_observation(LOC["id"], drifted)
    rows = wl.apply_transform_fault([wl.parse_observation(LOC["id"], PAYLOAD)], "null_values")
    with pytest.raises(wl.QualityError, match="null"):
        wl.check_quality(rows)


def test_quality_gate():
    good = wl.parse_observation(LOC["id"], PAYLOAD)
    wl.check_quality([good])
    with pytest.raises(wl.QualityError, match="out of range"):
        wl.check_quality([{**good, "temperature_c": 140.0}])
    with pytest.raises(wl.QualityError):
        wl.check_quality([])


def test_documents_and_upload():
    rows = [wl.parse_observation(loc["id"], PAYLOAD) for loc in wl.LOCATIONS]
    docs = wl.build_documents(rows, datetime(2026, 10, 9, 17, 0, tzinfo=timezone.utc))
    assert len(docs) == 7
    index = docs["v1/index.json"]
    assert index["generated_at"] == "2026-10-09T17:00:00Z" and index["count"] == 6
    assert docs["v1/current/morzine.json"]["location"]["name"] == "Morzine, France"

    puts = []

    def put(url, data, headers):
        puts.append((url, json.loads(data), headers))
        return 201, ""

    result = wl.upload_documents(docs, "https://acct.blob.core.windows.net/weather/", "?sv=1&sig=REAL", http_put=put)
    assert result["files_written"] == 7 and result["target_prefix"] == "v1/"
    assert puts[0][0].startswith("https://acct.blob.core.windows.net/weather/v1/") and puts[0][0].endswith("?sv=1&sig=REAL")
    assert puts[0][2]["x-ms-blob-type"] == "BlockBlob"

    puts.clear()
    misrouted = wl.upload_documents(docs, "https://acct.blob.core.windows.net/weather", "sig=REAL", http_put=put, fault="wrong_path")
    assert misrouted["target_prefix"] == "staging/v1/" and "/weather/staging/v1/" in puts[0][0]

    puts.clear()
    wl.upload_documents(docs, "https://a/c", "sv=1&sig=REAL&se=2", http_put=put, fault="publish_denied")
    assert "sig=REAL" not in puts[0][0] and "sv=1" in puts[0][0] and "se=2" in puts[0][0]

    with pytest.raises(wl.PublishError, match="HTTP 403") as err:
        wl.upload_documents(docs, "https://a/c", "sig=SECRET", http_put=lambda *_a: (403, "AuthenticationFailed"))
    assert "SECRET" not in str(err.value)
