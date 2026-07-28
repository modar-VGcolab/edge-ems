# ext-ems — external-EMS takeover & release gateway (HIL Scenario 1)

Opt-in container that makes the edge-EMS **follow** an external EMS, **take over**
to self-consumption when that EMS goes silent past a watchdog timeout, and
**release** control back when it returns. It is absent from the normal A–E runs
and is started only with the `ext-ems` compose profile.

```
emulator --MQTT(site/<id>/external/pcc_setpoint)--> gateway + watchdog
 (random P*, 5-min cadence,                          (freshness clock;
  simulated outage window)                            FOLLOWING <-> SELF_CONSUMPTION)
                                                        |  +--> GET /status (:5010)
                                                        |  +--> InfluxDB external_ems
                                                        v  HTTP POST /setpoint (live)
                                        controller --MQTT--> core --> bridge --> plant
```

## Modules (`ext_ems/`)

| File | Role |
|---|---|
| `config.py` | Env-driven settings; `TIME_SCALE` compresses all timings for fast runs. |
| `emulator.py` | Random-P* MQTT publisher with an outage/reconnect timeline. |
| `gateway.py` | `Watchdog` (pure state machine) + `Gateway` runtime (forward + telemetry). |
| `forwarder.py` | POSTs the effective PCC setpoint to the controller `/setpoint`. |
| `influx.py` | Writes the `external_ems` measurement (takeover state). |
| `status_api.py` | FastAPI `GET /status`, `GET /health` on `:5010`. |
| `main.py` | Asyncio wiring of emulator + gateway + status API over one MQTT link. |

## State machine

`FOLLOWING_EXTERNAL` (effective P* = last external P*) → on no external message
for `>= EXT_WATCHDOG_TIMEOUT_S` → `SELF_CONSUMPTION` (effective P* = 0) **[TAKEOVER]**;
a later fresh message → `FOLLOWING_EXTERNAL` **[RELEASE]**. Startup with no message
yet → `SELF_CONSUMPTION` (safe default). Freshness boundary: fresh while age `<`
timeout; at age `==` timeout it takes over.

## Run (test mode)

```bash
docker compose --env-file configs/.env \
  -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml \
  --profile services --profile ext-ems up -d --build      # TIME_SCALE defaults to 60
curl -X POST http://localhost:5000/loop/start
curl http://localhost:5010/status        # state, last_external_age_s, takeover_count
```

`TIME_SCALE=60` runs the 5/6/10-minute scenario in ~25 s; set `1` for real timing.
Full run procedure and pass criteria: repo `README.md` §8.5 and
`hil/schematic/RIG_CLOSED_LOOP.md` §4c.

## Configuration (env, defaults; timings divide by `TIME_SCALE`)

| Env var | Default | Meaning |
|---|---|---|
| `SITE_ID` | `vgcolab-01` | topic namespace |
| `MQTT_BROKER` / `MQTT_PORT` | `mosquitto` / `1883` | broker |
| `CONTROLLER_URL` | `http://controller:5000` | live-setpoint target |
| `CONTROLLER_API_TOKEN` | `` (empty = auth off) | sent as `X-API-Key` on every `/setpoint` POST; must match the controller's own `CONTROLLER_API_TOKEN` (KNOWN_ISSUES #4) |
| `INFLUX_URL`/`INFLUX_ORG`/`INFLUX_BUCKET`/`INFLUX_TOKEN` | from `configs/.env` | telemetry (empty token → no-op writer) |
| `EXT_PUBLISH_INTERVAL_S` | `300` | external publish cadence |
| `EXT_WATCHDOG_TIMEOUT_S` | `360` | silence before takeover |
| `EXT_OUTAGE_AT_S` / `EXT_RECONNECT_AT_S` | `600` / `1200` | emulator silence window |
| `EXT_P_MIN_KW` / `EXT_P_MAX_KW` | `-300` / `400` | random P* range (within PCC limits) |
| `EXT_SELF_CONSUMPTION_KW` | `0` | effective PCC target during takeover |
| `TIME_SCALE` | `1` (`60` in compose) | test-mode time compression |
| `STATUS_PORT` | `5010` | gateway `/status` |

## Tests

```bash
python -m pytest deploy/ext-ems/tests        # watchdog state machine + gateway integration
```

The controller-side `POST /setpoint` contract is tested in
`tests/contract/test_http_api.py`.
