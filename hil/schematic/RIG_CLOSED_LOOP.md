# Rig closed loop — edge-EMS controlling BP09_ext_v1.tse

The **Typhoon model runs in HIL SCADA**, the **signal bridge** (host process,
needs the Typhoon API) exposes each asset as a Modbus server, and **InfluxDB,
MQTT, core, and controller all run as containers**. core reaches the host bridge
over `host.docker.internal`. The controller regulates PCC active power to its
setpoint (0 kW = self-consumption) by dispatching the battery and curtailing
PV / shedding the flexible load.

```
 Typhoon model (HIL SCADA)
        ▲  │  read_analog_signal / set_scada_input_value
        │  ▼
 signal_bridge (host, .venv-rig)
        │  Modbus TCP  0.0.0.0:5020-5024
        ▼  (containers reach it via host.docker.internal)
 ┌─────────────────── docker network ───────────────────┐
 │  core ──► InfluxDB ──► controller ──┐                 │
 │   ▲                                  │ MQTT setpoints  │
 │   └──────────── MQTT ◄───────────────┘                 │
 └────────────────────────────────────────────────────────┘
```

Only the bridge stays on the host — it needs the Windows-only Typhoon API. The
five SIL `sim-*` containers are NOT used; the bridge is the plant.

## 0. Prerequisites

- `BP09_ext_v1.tse` **compiled and loaded, simulation RUNNING in HIL SCADA**
  (the bridge is SCADA-safe: it will NOT start/stop the sim, only drive signals).
- `.venv-rig` active in the bridge terminal.
- Docker Desktop running.
- `configs/.env` exists with `INFLUX_TOKEN=...` (already present in this repo).
  The same token initializes InfluxDB and is expanded into the EMS config.
- All commands run from the repo root: `...\BP09_ext\edge-ems`.

Confirm signal names resolve (model loaded):

```powershell
.\.venv-rig\Scripts\Activate.ps1
python -m hil.schematic.verify_signals      # expect: 0 unresolved binding(s)
```

## 1. Start the bridge (host process)

`.venv-rig` active, model running in SCADA:

```powershell
python -m hil.schematic.signal_bridge --asset-config configs/asset_config.docker.yaml
```

Expect: `bridge: simulation already running (SCADA-driven); leaving start/stop to
SCADA.` then `bridge: serving Modbus -> pcc-01@5020, bess-01@5021, pv-01@5022,
fload-01@5023, load-02@5024`. It serves until Ctrl-C. (The bridge binds 0.0.0.0
and only uses the *ports* from the config, so this same command works for both
the docker and host-process configs.) Add `--scenario tracking` to also inject
grid/irradiance excursions.

> Windows Firewall: the first time a container connects in, allow Python through
> on ports 5020-5024 (private networks) if prompted, or the Modbus reads will
> time out.

## 2. Bring up the stack (containers)

New terminal, repo root:

```powershell
docker compose --env-file configs/.env `
  -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml `
  --profile services up -d --build
```

This starts **influxdb, mosquitto, core, controller** (the `services` profile
excludes the sim-* devices). The overlay points core/controller at the
`*.docker.yaml` configs and mounts host `configs/` read-only, so editing a config
and re-running `up -d` re-applies it without a rebuild.

Watch the logs:

```powershell
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml logs -f core controller
```

Expect in `core`: `core: polling 5 devices at 1.0s for site vgcolab-01`, then
steady reads (no COMM_FAIL). `controller` should report RUN state.

## 3. Verify the loop

- In **HIL SCADA**, watch `bess-01-UI.Pref` and `bess-01-UI.Pmeas_kW` move as the
  controller steers PCC power toward 0 kW; `Power Meter.pcc_01_meter.POWER_P`
  should converge toward 0.
- Change the load/PV in SCADA (or run the bridge with `--scenario tracking`) and
  watch the battery respond within a few cycles.
- controller HTTP API: http://localhost:5000.

If `core` logs Modbus COMM_FAIL on every asset: the containers can't reach the
host bridge. Check (a) the bridge is serving, (b) Windows Firewall allows
5020-5024, (c) `host.docker.internal` resolves (Docker Desktop maps it
automatically).

## 4. Behavioral checks (settle the remaining `# VERIFY` flags in signal_bridge.py)

While the loop runs, confirm conventions and edit the constants if needed:

- **BESS sign** (`BESS_P_SIGN`): command a known battery charge and confirm PCC
  moves the expected direction. Generator convention (inject = +) → `-1` (current).
- **PCC scale/sign** (`PCC_POWER_SCALE`, `PCC_P_SIGN`): `Power Meter` `POWER_P` is
  Watts → `1e-3` (current). Flip `PCC_P_SIGN` if import shows negative.
- **PV curtailment direction** (`_pv_curtailment_from_derate`): set a 50%
  curtailment and watch `pv-01-UI.Pmeas_kW`. If it goes the wrong way, switch the
  return line from `derate` to `1 - derate`.
- **BESS enable bit**: if the battery ignores `Pref`, confirm core asserts the
  SunSpec 704 `WSetEna` (active_power_setpoint_enable, reg 40248), not just `WSet`.

After editing `signal_bridge.py`, restart the bridge (Ctrl-C, re-run). No
container rebuild needed.

## 4b. Interactive load step (watch the BESS react)

To probe the controller live, drive the flexible load from a SCADA slider and
watch the battery hold PCC at setpoint:

1. In the model (inside `load-01-UI`) add a **SCADA input `load_demand_kW`**
   (range 0–500) and bind a panel slider to it. This HIL API cannot *read* a
   SCADA input back (it is write-only via `set_scada_input_value`), so also wire
   that input into a **Probe named `load_demand_probe`** — the bridge reads the
   probe (`read_analog_signal`), not the input. **Recompile + reload** the model
   so the probe signal exists. `LOAD_DEMAND_SCADA` in `signal_bridge.py` must
   point at the probe (`load-01-UI.load_demand_probe`); confirm with
   `verify_signals --dump`. On a good start the bridge prints
   `load slider read via read_analog_signal('load-01-UI.load_demand_probe')`.
   Do **not** put the slider on `load-01-UI.Pref` — the bridge owns Pref and
   overwrites it each tick.
2. Precondition the BESS to **grid-following** and seed SoC to **~50%** (mid-band,
   so the derate stays 1.0 and only the battery responds, not the load-shed ladder).
3. Leave the PCC setpoint at its default 0 kW. Start the loop, let it settle.
4. Drag the slider. Expect: **load up → BESS discharges** to cover it (PCC returns
   to ~0); **load down → BESS charges**. Watch `bess-01-UI.Pmeas_kW`, or
   `bess-01-UI.Pref × 500 × −1` for the kW command.

Caveats: while discharging, SoC falls — if it crosses 20% the load-derate ladder
engages and the controller starts trimming your demand (expected, but it changes
the test; keep runs short or start at high SoC). The slider is optional: if the
`load_demand_kW` input is absent, the bridge falls back to the `load_kw` profile
channel, so scripted scenarios are unaffected.

## 4c. External-EMS takeover & release (Scenario S1)

Validate that the edge-EMS follows an external EMS, takes over to self-consumption
when it goes silent, and releases on reconnection. This adds the opt-in `ext-ems`
container (emulator + watchdog + gateway); it is **absent** from the normal runs, so
turn it on only for this scenario.

1. Bring up the stack **with the profile**, in test mode (`TIME_SCALE=60` runs the
   5/6/10-minute timeline in ~25 s; set `1` for real timing):

   ```powershell
   docker compose --env-file configs/.env `
     -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml `
     --profile services --profile ext-ems up -d --build
   curl.exe -X POST http://localhost:5000/loop/start
   ```

2. Watch three things together:
   - controller `GET http://localhost:5000/status` — `last_cycle.pcc_setpoint_kw`, `pcc_error_kw`
   - gateway `GET http://localhost:5010/status` — `state`, `last_external_age_s`, `takeover_count`
   - the live plot — `python -m hil.schematic.live_plot` shows the `external_ems.active_source`
     trace (ext/self) against PCC power and SoC.

3. Phases to confirm:
   - **A — following:** PCC tracks the random external P* each cadence (gateway `FOLLOWING_EXTERNAL`).
   - **B — takeover:** during the simulated outage, after the watchdog timeout, the gateway goes
     `SELF_CONSUMPTION`, the controller setpoint → 0, PCC → 0, `takeover_count` increments.
   - **C — release:** when the emulator resumes, the gateway returns to `FOLLOWING_EXTERNAL`
     and PCC resumes tracking.

4. Capture `/status` snapshots at each transition plus a live-plot image for the report
   (Results doc, section 1). Pass criteria: external P* followed while fresh; takeover at the
   watchdog timeout; clean release on reconnection; controller stays in RUN throughout.

Tuning env (in the `ext-ems` service / `configs/.env`): `EXT_PUBLISH_INTERVAL_S`,
`EXT_WATCHDOG_TIMEOUT_S`, `EXT_OUTAGE_AT_S`/`EXT_RECONNECT_AT_S`, `EXT_P_MIN_KW`/`EXT_P_MAX_KW`,
`TIME_SCALE`. See README §8.5 for the full table.

## 5. Teardown

```powershell
# stop the containers (include --profile ext-ems if you started S1)
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml `
  --profile services --profile ext-ems down   # add -v to also wipe InfluxDB/MQTT volumes
# Ctrl-C the bridge terminal
```

## Appendix — host-process variant (no core/controller containers)

If you ever want core/controller on the host instead of in containers, use
`configs/asset_config.rig.yaml` (host `127.0.0.1`) + `configs/edge_ems_config.rig.yaml`
(localhost endpoints), bring up only `influxdb mosquitto` in Docker, and run
`python -m core.main` / `python -m controller.main` in `.venv-rig` with
`ASSET_CONFIG_PATH`/`EMS_CONFIG_PATH`/`INFLUX_TOKEN` set. Same loop, different
process boundary.

## Notes

- `load-02` (class `meter`, 100 kW, port 5024) is declared and served: the
  bridge reads it every tick (`hil/schematic/signal_bridge.py`'s `ASSET_MAPS` +
  `SIGNALS["load-02"]`) and it flows through as the `meter` aggregate. The
  control law does not dispatch it directly (no setpoints), but `pfc.py`'s
  `reactive_setpoint_kvar` reads its `reactive_power_kvar` as part of
  `other_reactive_kvar` when sizing the battery's PFC reactive residual —
  it's telemetry-only, not a controlled asset.
- Config files are bind-mounted **read-write** (`docker-compose.rig.yml`,
  changed from `:ro` on 2026-07-29): non-structural EMS config changes now
  apply to the running loop without a restart (`PUT /config/ems`,
  `LoopRunner.apply_config`, KNOWN_ISSUES #2), and that PUT persists back to
  the same host file the bridge/core/controller all read. If you'd rather the
  containers never write host files, revert both `core`'s and `controller`'s
  mounts to `:ro` and stick to file-edit + restart instead of PUT on this path.
