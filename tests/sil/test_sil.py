"""Live SIL run (Gate G2). Skipped unless EDGE_EMS_SIL=1 and the compose stack
(`docker compose --profile sil up`) is running.

For each scenario it applies the setup via the orchestrator (config/droop/fault),
lets the loop run for the scenario duration, fetches the `control` measurement
series from InfluxDB, and calls the scenario's pure check. The checks themselves
are validated in test_sil_assertions.py and the action planning in
test_sil_orchestrate.py, so a green run here means the *system* behaves.
"""

import pytest
from orchestrate import LiveExecutor, run_scenario
from scenarios import SCENARIOS

pytestmark = pytest.mark.sil


def _fetch_control_series(env, lookback_s):  # pragma: no cover - needs infra
    from influxdb_client import InfluxDBClient

    flux = f'''
    from(bucket: "{env["influx_bucket"]}")
      |> range(start: -{lookback_s}s)
      |> filter(fn: (r) => r._measurement == "control" and r.site_id == "{env["site_id"]}")
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> sort(columns:["_time"])
    '''
    rows = []
    with InfluxDBClient(
        url=env["influx_url"], token=env["influx_token"], org=env["influx_org"]
    ) as client:
        for table in client.query_api().query(flux):
            for rec in table.records:
                v = rec.values
                rows.append(
                    {
                        "pcc_error_kw": v.get("pcc_error_kw", 0.0),
                        "pi_output_kw": v.get("pi_output_kw", 0.0),
                        "derate_factor": v.get("derate_factor", 1.0),
                        "curtail_factor": v.get("curtail_factor", 1.0),
                        "loop_duration_ms": v.get("loop_duration_ms", 0.0),
                        "mode": v.get("mode", "RUN"),
                    }
                )
    return rows


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario(scenario, sil_env):  # pragma: no cover - needs infra
    executor = LiveExecutor(sil_env["controller_url"])
    run_scenario(scenario, executor)  # applies setup + runs the window
    series = _fetch_control_series(sil_env, scenario.duration_s + 10)
    scenario.check(series)
