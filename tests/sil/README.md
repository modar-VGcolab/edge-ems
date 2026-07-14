# SIL harness (Gate G2)

Software-in-the-loop: the full Docker stack (InfluxDB + Mosquitto + core +
controller + simulators) against the device simulator — no HIL hardware. Runs
the seven validation scenarios and asserts on the `control` measurement.

## Run

```bash
# 1. bring up the full stack incl. simulators
docker compose -f deploy/docker-compose.yml --profile sil up -d --build

# 2. start the control loop
curl -X POST http://localhost:5000/loop/start

# 3. run the scenarios against the live stack
EDGE_EMS_SIL=1 INFLUX_TOKEN=change-me pytest tests/sil/test_sil.py -v
```

Without `EDGE_EMS_SIL=1` the live tests skip; `tests/sil/test_sil_assertions.py`
(the oracle's own tests) always run as part of the normal suite.

## The seven scenarios

tracking · saturation→derate · curtailment · droop · stale data · config
reload · 50-asset scale. Each is defined in `scenarios.py` with a pure `check`
over the control series; the live runner only fetches the series and calls it.
