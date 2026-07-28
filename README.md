# edge-EMS — Edge Energy Management System for self-consumption microgrids

A small, real-time energy-management controller for a single LV microgrid site.
It steers a battery, curtails PV, and sheds a flexible load so that the **point
of common coupling (PCC)** tracks a power setpoint (typically 0 kW —
self-consumption), while respecting grid import/feed-in limits and battery
state-of-charge limits.

Everything that crosses a service boundary is named, typed, and signed by **one
canonical data model** (`data_model.yaml`). Two services — an **orchestrator**
(`core`) that talks to devices and a **controller** that runs the control law —
are decoupled through InfluxDB (measurements) and MQTT (setpoints). The same
register-map files that the controller reads also generate the device simulator
and the Hardware-in-the-Loop plant, so the controller cannot tell a real
inverter from a simulated one (*parity by construction*).

---

## Documentation map

Each doc below lives next to the code it describes rather than in a shared
`docs/` folder — subsystem docs stay in sync better when they're impossible to
miss while editing that subsystem. This file is the entry point; the others are
read on demand.

| Doc | Covers | Read when |
|---|---|---|
| **`README.md`** (this file) | Architecture, data model, config reference, how to run each fidelity gate | Starting out |
| **`KNOWN_ISSUES.md`** | Five tracked follow-ups (4 resolved: config `${VAR}` expansion, hot config reload, API auth, structured logging); the one still OPEN is the real G3 blocker — SunSpec maps unverified against real firmware | Before relying on hot reload/auth/logging, or before real-device sign-off |
| **`hil/README.md`** | CHIL rig plant side: legacy scenario/action-planner path, component list | Orienting in `hil/` for the first time |
| **`hil/schematic/RIG_CLOSED_LOOP.md`** | Bring-up runbook for the Typhoon HIL606: bridge, containers, getting the loop to RUN | Bringing the rig up from scratch |
| **`hil/schematic/RIG_FEATURE_GUIDE.md`** | Exercising self-consumption and tariff PFC once the loop is already up, plus shared verification/troubleshooting | Loop is up; testing or demoing a specific feature |
| **`hil/reports/chil_report.md`** | *Generated* by `hil.report` — parity, scenario, tuning, loop-budget results. Don't hand-edit. | Checking the latest CHIL validation run |
| **`deploy/ext-ems/README.md`** | Opt-in external-EMS takeover/release gateway (HIL Scenario 1) | Working on or running Scenario 1 |
| **`reconciliation/README.md`** | Reconciling generated register maps against the real inverter firmware | Working the firmware-verification blocker (§9) |
| **`tests/sil/README.md`** | SIL harness: full Docker stack against simulators, no hardware | Running/debugging Gate G2 |

---

## 1. Architecture at a glance

```
                          ┌──────────────────────────────────────────────┐
                          │                  edge-EMS                    │
   Field devices          │                                              │
  (Modbus TCP)            │   ┌─────────┐   InfluxDB        ┌──────────┐ │
 ┌──────────┐             │   │  core   │  measurements     │controller│ │
 │ PCC meter│◀──poll────▶│   │(orchestr│──(pcc, battery,──▶│ control  │ │
 │ BESS     │◀──poll────▶│   │ -ator)  │   pv, load,       │  loop +  │ │
 │ PV inv.  │◀──poll────▶│   │         │   aggregate)      │ PI/droop │ │
 │ flex load│◀──poll────▶│   │ device  │                   │  + modes │ │
 └──────────┘             │   │ adapters│◀──setpoints───────│          │ │
        ▲                 │   │ aggregator MQTT             │ HTTP API │ │
        │ setpoints       │   │ dispatcher│ site/.../setpoints/{class}  │ │
        └────write────────│   └─────────┘                   └────┬─────┘  │
                          │        ▲                             │       │
                          └────────┼─────────────────────────────┼───────┘
                                   │ InfluxDB + MQTT (Docker)    │ :5000 HTTP
                                   ▼                             ▼ (config, health)
                            influxdb 2.x                    operator / tests
                            mosquitto
```

- **`core` (orchestrator, southbound).** Owns one Modbus TCP adapter per asset
  (built from `asset_config.yaml` `comm:` blocks), polls each at 1 Hz, decodes
  via the register map, aggregates per asset class, writes the `pcc`/per-asset/
  `aggregate` measurements to InfluxDB, and disaggregates the controller's MQTT
  setpoints back down to the devices (with a sequence guard and a silence
  watchdog).
- **`controller` (control, northbound).** Reads the `aggregate` + `pcc`
  measurements from InfluxDB, judges freshness, runs the **RUN/HOLD/SAFE** mode
  ladder, then (in RUN) applies droop and the PI/priority-ladder core, publishes
  battery and derate/curtail setpoints over MQTT, and writes the `control`
  measurement. Exposes a config/health/loop HTTP API on port 5000.
- **Buses.** InfluxDB carries measurements (1 Hz, 30-day retention on the edge);
  MQTT carries setpoints (QoS 1, not retained, monotonic `seq`).
- **The two services never import each other** — only `common`. They communicate
  solely through the buses and the data model.

The full architecture and design rationale live in the repo-root documents:
`microgrid-framework-architecture.md`, `microgrid-framework-system-design.md`, and the implementation plan/roadmap.

---

## 2. The canonical data model (`data_model.yaml`) — the conformity contract

Nothing crosses an interface without a name from here. It defines, per asset
class, every point's **name, unit, type, and direction**, plus the **limit** and
**nominal** schemas. It is the single source of truth for `asset_config.yaml`,
the InfluxDB schema, the MQTT payloads, the HTTP API, and the register maps.

**Sign convention (locked, applies everywhere): positive = consumed by the
asset.**

| Point (per class)                            | Sign / range                          |
|----------------------------------------------|---------------------------------------|
| `pcc.active_power_kw`                        | `+import / −export`                   |
| `battery.active_power_kw` and its setpoint   | `+charge / −discharge`                |
| `pv.active_power_kw`                         | `≤ 0` (generation)                    |
| `flexible_load.active_power_kw`              | `≥ 0` (consumption)                   |
| `derate_factor_setpoint` (pv, flexible_load) | `[0, 1]`, `1` = no derate/curtailment |

Power is kW (active) / kVAr (reactive) / kVA (apparent); voltage is Vrms
phase-neutral; frequency Hz; SoC %. Per-unit (base = PCC max power) is **internal
to the controller only** — every value crossing an interface is SI.

**Asset classes:** `pcc` (single, never aggregated), `battery` (≥1 required,
controllable via power setpoint), `pv` (curtailable via derate), `flexible_load`
(deratable), `meter` (passive — measurement only, no setpoints; used for fixed
loads whose reactive power the controller needs to see but never dispatches).
**Aggregates** (`core` writes, controller reads): per-class sums for
battery/pv/flexible_load/meter. **Quality** flags on every record:
`GOOD` (fresh), `STALE` (older than `timeout_period`), `COMM_FAIL` (adapter
failure / nothing contributed).

---

## 3. The modeled site — `vgcolab-01` (display name VGCOLAB-01)

A 400 V / 50 Hz LV feeder with the PCC at the gr    id coupling point.

| Asset     | Class         | Ratings                                                                                 |
|-----------|---------------|-----------------------------------------------------------------------------------------|
| `pcc-01`  | pcc           | 230 V phase-neutral, 50 Hz; max supply 2000 kVA; max feed-in 999 kVA; max current 100 A |
| `bess-01` | battery       | ±1000 kW; 2000 kWh; SoC 5–95 %, low-warn 20 %, high-warn 90 %                           |
| `pv-01`   | pv            | 500 kW peak (max 600 kVA); curtailable, min derate 0.0                                  |
| `fload-01`| flexible_load | 700 kW; deratable, min derate 0.2                                                       |
| `meter-01`| meter         | 100 kW fixed load; measurement only (no setpoints) — feeds the PFC reactive balance     |

Defined in `configs/asset_config.example.yaml`. On the Typhoon rig the fixed
load is `load-02` (`configs/asset_config.{rig,docker}.yaml`) — same class and
role, different id/comm block per environment.

---

## 4. Repository layout

```
edge-ems/
├── data_model.yaml              Canonical data model (names, units, signs) — normative
├── maps/                        Modbus register maps per device (DRAFT until firmware-verified)
│   ├── grid_meter_v1.yaml         pcc            (SunSpec 701)
│   ├── custom_bess_v1.yaml        battery        (SunSpec 1+701+704+713 + vendor)
│   ├── custom_pv_inverter_v1.yaml pv             (SunSpec 1+701+704)
│   ├── flexible_load_v1.yaml      flexible_load  (SunSpec 1+701+704)
│   ├── meter_v1.yaml              meter / fixed load (SunSpec 701)
│   ├── archive/                   pre-SunSpec map snapshots (*.pre-sunspec.yaml)
│   └── sunspec/                   generate.py + per-asset <id>.image.yaml / .canonical.yaml
├── configs/
│   ├── asset_config.example.yaml  Site composition + comm blocks + limits
│   ├── asset_config.{rig,docker}.yaml   CHIL rig wiring (127.0.0.1 / host.docker.internal)
│   ├── edge_ems_config.example.yaml  Controller gains, droop, aggregation, buses
│   ├── edge_ems_config.{rig,docker}.yaml  CHIL rig endpoints (localhost / service names)
│   └── .env.example               Secrets/IDs for docker compose (copy to .env)
├── packages/                    Source (src-layout, installed editable)
│   ├── common/                    Data-model loader, config validation, register-map
│   │                              loader, Modbus codec, points/quality — the ONLY shared dep
│   ├── core/                      Orchestrator: modbus adapter, aggregator, influx writer,
│   │                              dispatcher, watchdog, orchestrator cycle
│   ├── controller/                Control loop, edge_controller (PI/anti-windup), modes,
│   │                              droop, data_connector, config_manager, http_api, runtime
│   └── simulator/                 pymodbus device simulator built from the same maps
├── hil/                         CHIL rig (Typhoon HIL606) — see hil/README.md
│   ├── plant/models.py            Pure plant physics + signed PCC balance
│   ├── servers.py                 Map-driven Modbus servers (parity by construction)
│   ├── parity_check.py            map = simulator = HIL server (mandatory gate)
│   ├── chil_runner.py             Plant-in-the-loop: real ControlLoop vs SiteModel
│   ├── profiles.py                One stimulus per scenario (+ SCADA CSVs)
│   ├── orchestrate_chil.py        7 scenarios + 3 fault injections (scenarios.py oracle)
│   ├── tuning/tune_pi.py          PI tuning + anti-windup probe
│   ├── scale_bench.py             50-server loop-budget histogram
│   ├── report.py                  Generates hil/reports/chil_report.md
│   └── schematic/                 Typhoon rig: build_schematic, signal_bridge (serves
│                                  Modbus + drives the model, SCADA-safe), verify_signals,
│                                  live_plot, RIG_CLOSED_LOOP.md
├── tests/                       unit · contract · integration · sil
│   └── sil/                       Gate G2 live harness + the seven scenario oracle
└── deploy/                      Docker Compose: docker-compose.yml (influxdb, mosquitto,
                                 core, controller, sims) + docker-compose.rig.yml (CHIL overlay,
                                 incl. the opt-in `ext-ems` profile)
    └── ext-ems/                 External-EMS takeover gateway (HIL Scenario 1): emulator +
                                 watchdog state machine + /status + InfluxDB telemetry (§8.5)
```

---

## 5. Key concepts

- **Register maps & parity by construction.** A map file translates canonical
  point names to Modbus holding registers (address, type, scale, rw). `core`,
  the `simulator`, and the HIL servers all build from the *same* file via the
  *same* codec (`common.modbus_codec`), so they agree by construction.
  `hil/parity_check.py` proves it.
- **Mode ladder (RUN → HOLD → SAFE).** RUN: emit computed setpoints. HOLD:
  aggregate data went stale — freeze the last setpoint for up to `hold_max_s`.
  SAFE: battery unavailable or HOLD outlived its budget — ramp the battery to 0
  (slew-limited) and release all mitigations. Recovery is automatic.
- **PI with conditional-integration anti-windup.** The PI drives PCC power to
  its setpoint by commanding the battery; the integrator is frozen only when the
  battery is saturated *and* the error pushes further into the violated limit.
- **Priority ladder.** Battery first; then low-SoC flexible-load derate; then
  high-SoC PV curtailment when exporting past the feed limit.
- **Droop (optional).** P-f / Q-V piecewise-linear correction added to the PCC
  setpoint before the PI sees the error.
- **Tariff-driven power-factor control (PFC, optional).** A time-of-day tariff
  schedule (`pfc.windows`) maps the wall clock to a PF goal at the PCC; the
  controller turns that into a battery reactive setpoint
  (`controller.pfc.reactive_setpoint_kvar`), sized for the *residual* after
  subtracting what PV/flexible_load/meter are already contributing
  (`other_reactive_kvar`) — so a fixed load's reactive draw doesn't get
  double-counted. Q-V droop keeps priority over PFC whenever it's actively
  commanding (voltage support beats the economic PF target). See
  `hil/schematic/RIG_FEATURE_GUIDE.md` for how to exercise it on the rig.

### Control cycle (target: 250 ms of the 1 s period)

```
t0 read aggregate + pcc from InfluxDB        t5 clamp to battery limits + SoC gates
t1 freshness check → STALE/HOLD              t6 low-SoC → derate flexible load
t2 convert to p.u. (PCC base)                t7 export>feed & high-SoC → curtail PV
t3 droop correction (if enabled)             t8 publish setpoints over MQTT
t4 e = setpoint − measured; PI + anti-windup t9 write the `control` measurement
```

---

## 6. Interface contracts

**HTTP API (controller, :5000)** — `GET /health`, `GET /status`,
`GET|PUT /config/assets`, `POST /config/assets/validate`,
`GET|PUT /config/ems`, `POST /config/ems/validate`, `POST /loop/start|/loop/stop`,
`POST /setpoint`, `GET /loop/state`. Config PUTs are applied to the running
loop immediately (`LoopRunner.apply_config`, KNOWN_ISSUES #2) — non-structural
changes (`Kp`/`Ki`, limits, droop, PFC) take effect from the next cycle with PI
state carried across; structural changes (asset added/removed) still require
the loop stopped (else 409). `POST /setpoint` (`{"pcc_setpoint_kw": <float>}`)
sets the **live** PCC target on the running loop without a restart — used by
the external-EMS gateway (§8.5); the applied value is echoed back in
`GET /status.last_cycle.pcc_setpoint_kw`. **Auth:** every route except
`GET /health` and `GET /loop/state` is gated behind an `X-API-Key` header when
`CONTROLLER_API_TOKEN` is set (empty/unset = auth off, KNOWN_ISSUES #4); the
ext-ems gateway sends the same token via its own `CONTROLLER_API_TOKEN`.

**Gateway HTTP API (ext-ems, :5010)** — `GET /health`, `GET /status`
(`state`, `active_source`, `last_external_age_s`, `watchdog_timeout_s`,
`last_pcc_setpoint_kw`, `takeover_count`); see §8.5.

**MQTT** — controller→core setpoints on `site/{site_id}/setpoints/{asset_class}`,
JSON payload with `data_model_version`, `site_id`, `ts`, monotonic `seq`, and
`setpoints`; `core` discards out-of-order/stale messages. The external EMS →
gateway path publishes PCC setpoints on `site/{site_id}/external/pcc_setpoint`
(`{"pcc_setpoint_kw", "seq", "ts"}`), consumed by the ext-ems gateway (§8.5).

**InfluxDB (bucket `edge_ems`)** — measurements `pcc`, `battery`, `pv`,
`flexible_load`, `meter` (tags `site_id`, `asset_id`, `quality`); `aggregate`
(tags `site_id`, `asset_class`); and `control` (`pcc_error_kw`, `pi_output_kw`,
`derate_factor`, `curtail_factor`, `loop_duration_ms`, `mode`). The `control`
measurement is the **test oracle** for the SIL/CHIL scenario assertions. When the
`ext-ems` profile runs, the gateway also writes `external_ems` (tag `site_id`;
fields `active_source` 1=following/0=self-consumption, `pcc_setpoint_kw`,
`last_external_age_s`) — the S1 takeover/release oracle (§8.5).

---

## 7. Configuration

- **`configs/asset_config.example.yaml`** — site id, and per-asset class, state,
  `nominal`, `limits`, `flexibility`, and the Modbus `comm` block (host, port,
  unit_id, register_map). Copy → edit → validate via
  `POST /config/assets/validate`.
- **`configs/edge_ems_config.example.yaml`** — InfluxDB/MQTT endpoints,
  `controller` block (`update_period`, `Kp`, `Ki`, `timeout_period`,
  `hold_max_s`, `slew_limit_kw_s`), `droop` curves, `pfc` (tariff windows +
  PF target, optional), `asset_aggregation`, and `logging` (`level`, optional
  `file`/`max_file_size`/`backup_count` for log rotation — wired up via
  `common.logging_setup.configure_logging`, called from both `core.main` and
  `controller.main` at startup). Tuned PI defaults committed here:
  **`Kp=0.5, Ki=0.5`** (see §9) — this is the SIL/software-plant tuning; the
  Typhoon-rig configs (`edge_ems_config.{rig,docker}.yaml`) run **`Kp=0.3,
  Ki=0.2`** instead, detuned for the rig's real comms round-trip (bridge +
  Modbus + InfluxDB + MQTT), which the zero-latency software plant doesn't
  have. **`edge_ems_config.rig.yaml`** (host-process) and
  **`edge_ems_config.docker.yaml`** (containers) must be kept in sync by hand —
  there's no shared inheritance between them, and a stale copy silently runs
  the wrong gains/PFC state with no error (see §9). Non-structural changes to
  this file take effect on a running loop via `PUT /config/ems` with no
  restart (§6, KNOWN_ISSUES #2).
- **`configs/.env.example`** — `SITE_ID`, `INFLUX_TOKEN/ORG/BUCKET`, MQTT creds,
  and `CONTROLLER_API_TOKEN` (controller HTTP API auth, empty = off, §6,
  KNOWN_ISSUES #4). Copy to `configs/.env` (git-ignored); loaded by Docker
  Compose.

All three are cross-validated against `data_model.yaml` at load (unknown
names/limits, version mismatches, and bad register placements are rejected).

### 7.1 Configuration authority — Core (ADR-0001)

`core` is the **configuration authority**: it serves the config API on an
**internal-only** port (`:5100`, not host-published) for `GET|PUT` of
`assets`/`ems` plus their `/validate` dry-runs, and `GET /config/data_model`
read-only (the ontology is a versioned artifact, not runtime-uploadable).
Per-site config persists on the `core-config` volume, seeded from image defaults
on first run.

The Controller's config source is selected by **`CONFIG_SOURCE`** (default
`file`, legacy local-file behavior). With `CONFIG_SOURCE=core` it pulls config
from Core into a **last-known-good cache** (`CORE_CONFIG_URL`, `CONFIG_CACHE_DIR`)
and keeps running on the cache if Core is briefly unreachable. A structural asset
change is refused while the control loop runs (Core consults the Controller's
`GET /loop/state`), and Core emits a retained `site/{id}/config/changed` MQTT
signal on every committed change so the Controller re-pulls (with a slow
backstop). See `docs/adr/ADR-0001-core-config-authority.md`.

---

## 8. How to run

There are four ways to run the system, in increasing order of fidelity: the pure
test suite (8.1, no infra), the SIL stack (8.2, Docker but no hardware), the CHIL
plant-in-the-loop (8.3, real controller code against a software plant), and the
CHIL rig (8.4, real plant on the Typhoon HIL606). Start at 8.1 and work down —
each layer assumes the one above it is green.

### 8.1 Dev environment & tests

This is the entry point: a local virtualenv with the four packages installed
editable, plus the test/runtime dependencies, then the offline test suite. No
Docker, no broker, no database — everything here runs in-process.

```bash
python -m venv .venv                                          # Python 3.10+ (repo pins 3.13)
source .venv/bin/activate                                     # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -e packages/common -e packages/simulator -e packages/controller -e packages/core       # editable installs
pip install pytest pyyaml "pymodbus>=3.12,<3.13" httpx requests   # dev/test deps
python -m pytest tests -q                                  # unit + contract + integration
```

What each step does:

- **`python -m venv .venv` then activate** creates and activates an isolated
  environment so the editable installs and pinned dependencies don't touch your
  system Python. Use Python 3.10 or newer; `.python-version` pins **3.13** for the
  canonical dev environment. Activate with `source .venv/bin/activate` on Unix, or
  `.venv\Scripts\Activate.ps1` (PowerShell) / `.venv\Scripts\activate.bat` (cmd) on
  Windows.
- **The editable installs (`pip install -e …`)** put all four packages
  (`common`, `simulator`, `controller`, `core`) on the path as live source, so
  edits take effect without reinstalling. `common` is the shared dependency the
  other three import; installing all four lets the tests import across package
  boundaries. Order doesn't matter — pip resolves them together.
- **The dev/test deps** are the runtime libraries the tests exercise: `pytest`
  (the runner), `pyyaml` (loads `data_model.yaml`, the configs, and the register
  maps), `pymodbus` in the **3.12.x** line (the Modbus stack used by the adapter,
  the simulator, and the HIL servers — the pin matters because the codec depends
  on its API), and `httpx` (drives the controller's HTTP API in the contract
  tests).
- **`python -m pytest tests -q`** runs the offline suite. Invoke it through
  `python -m pytest`, **not** a bare `pytest`: the `python -m` form uses the
  virtualenv's interpreter — where the editable packages are installed — whereas a
  bare `pytest` can resolve to a different Python on your `PATH` and fail at import
  with `ModuleNotFoundError: No module named 'common'`. The `-q` flag is quiet
  output; drop it for a verbose per-test listing, or target a layer directly, e.g.
  `python -m pytest tests/unit -q`.

The `tests/` tree has four layers: **`unit`** (pure logic — control loop, PI,
modes, droop, codec, aggregator), **`contract`** (config + register-map +
data-model conformity, and the HTTP API), **`integration`** (orchestrator,
dispatcher, Modbus adapter, simulator, watchdog), and **`sil`** (the live Gate-G2
harness plus the seven-scenario oracle and its assertion unit tests). The plain
`pytest tests -q` invocation runs unit + contract + integration; the live SIL
harness in `tests/sil/` is driven separately by 8.2 (it needs the Docker stack
up and is gated behind the `EDGE_EMS_SIL=1` environment flag).

### 8.2 SIL — software-in-the-loop (Gate G2, no hardware)

SIL is the first full-system test: it runs the **real** `core` and `controller`
services against four pymodbus device simulators, wired through the real
InfluxDB and MQTT buses — all in Docker, no physical hardware. The simulators
serve Modbus from the same register maps `core` reads, so the controller cannot
tell them from real devices (parity by construction).

```bash
bash tests/sil/run_sil.sh
# or: docker compose -f deploy/docker-compose.yml --profile sil up -d --build
```

`run_sil.sh` is the one-command path and does the full sequence for you: it
exports a default `INFLUX_TOKEN` if you haven't set one, builds and starts the
`sil` profile (InfluxDB + Mosquitto + `core` + `controller` + the four
simulators `sim-grid`/`sim-bess`/`sim-pv`/`sim-load`), then runs a **health
gate** — after a short settle it inspects every simulator container and aborts
(with the last lines of that container's logs) if any isn't `running`, so a crash
shows up immediately instead of as a confusing test failure. It then polls
`http://localhost:5000/health` for up to ~60 s until the controller is live, runs
the seven scenarios via `EDGE_EMS_SIL=1 pytest tests/sil/test_sil.py -v` (which
assert on the `control` measurement — the test oracle from §6), and finally tears
the whole stack back down with `docker compose … --profile sil down`, returning
pytest's exit code. **Prerequisite:** Docker with Compose v2, and
`configs/.env` must exist (copy it from `configs/.env.example`) because `core`
and `controller` load it via `env_file`.

The raw `docker compose … --profile sil up -d --build` line is the same stack
without the health gate, the `/health` wait, the scenario run, or the automatic
teardown — useful when you want the stack to stay up so you can poke at it by
hand. Remember to `docker compose -f deploy/docker-compose.yml --profile sil
down` when you're done.

Infra only (for local development against the buses) — start just InfluxDB
(`:8086`) and Mosquitto (`:1883`) and run `core`/`controller` from your venv
against them, instead of in containers:

```bash
docker compose -f deploy/docker-compose.yml up -d         # influxdb + mosquitto
```

With no `--profile`, only the always-on services (`influxdb`, `mosquitto`) start;
`core`, `controller`, and the simulators all sit behind profiles and stay down.

### 8.3 CHIL plant-in-the-loop (runnable without the rig)

CHIL ("controller-hardware-in-the-loop") normally means the real controller
driving a real-time plant on the Typhoon rig. This subsection is the **software**
plant-in-the-loop equivalent: it wires the **real** controller core to a software
plant model (`hil/plant/models.py`) and validates the same seven scenarios — no
Typhoon and no Docker required, so it runs anywhere the venv from 8.1 does. Run
the commands in order; parity must be green before the rest is meaningful.

```bash
python -m hil.parity_check        # register parity — must be green first (the gate)
python -m hil.orchestrate_chil    # 7 scenarios + 3 fault injections (plant-in-the-loop)
python -m hil.tuning.tune_pi      # sweep gains + anti-windup probe (--write to commit)
python -m hil.scale_bench         # 50-asset loop-budget histogram (real Modbus)
python -m hil.report              # full CHIL report -> hil/reports/chil_report.md
python -m pytest hil/tests -q                # repeatable CHIL regression suite
```

- **`hil.parity_check`** proves that the register map, the simulator, and the HIL
  Modbus servers all encode/decode identically. It's the **mandatory gate**: if
  parity isn't green, every result below it is suspect, so run it first.
- **`hil.orchestrate_chil`** runs the seven control scenarios plus three fault
  injections (InfluxDB write stall, MQTT broker loss, asset/battery dropout)
  against the software plant, checking each against the `scenarios.py` oracle.
- **`hil.tuning.tune_pi`** sweeps PI gains and probes anti-windup recovery to find
  good `Kp`/`Ki`. It reports by default; pass **`--write`** to commit the tuned
  values into `configs/edge_ems_config.example.yaml` (this is how the current
  `Kp=0.5, Ki=0.5` defaults in §9 were set).
- **`hil.scale_bench`** spins up ~50 Modbus servers and histograms the control
  loop's per-cycle duration to confirm it stays under the 250 ms budget at scale.
- **`hil.report`** runs the above and writes the consolidated CHIL report to
  `hil/reports/chil_report.md`.
- **`pytest hil/tests -q`** is the repeatable regression suite over the CHIL
  logic, for CI and quick re-checks.

### 8.4 CHIL on the Typhoon HIL606 (Gate G3, real plant)

The highest-fidelity gate: the real `core`/`controller` driving a real-time plant
on the **Typhoon HIL606** (model `BP09_ext_v1.tse`). The plant exposes each asset
(PCC meter, BESS, PV, flexible load) as model signals; the **signal bridge**
(`hil/schematic/signal_bridge.py`) translates those to/from the map-generated
Modbus servers, so the controller talks the same Modbus it would to field
devices. The bridge runs on the rig host (it needs the Windows-only Typhoon API)
and is **SCADA-safe**: when HIL SCADA owns the running simulation, the bridge only
drives signals — it never calls `load_model`/`start_simulation`.

Closed-loop bring-up — full runbook in **`hil/schematic/RIG_CLOSED_LOOP.md`**:

```powershell
# 0. open + compile BP09_ext_v1.tse and START the simulation in HIL SCADA, then:
python -m hil.schematic.verify_signals          # every binding resolves (0 unresolved)
python -m hil.schematic.signal_bridge --asset-config configs/asset_config.docker.yaml
#      ^ serves one Modbus server per asset (ports 5020-5024, incl. the load-02
#        fixed-load meter used for PFC reactive-balance closure) and drives the model

# core + controller + InfluxDB + MQTT in containers, reaching the host bridge
# over host.docker.internal:
docker compose --env-file configs/.env `
  -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml `
  --profile services up -d --build

python -m hil.schematic.live_plot               # host-side live InfluxDB plot
```

Supporting tools and configs added for the rig:

- **`hil/schematic/verify_signals.py`** — diffs the bridge's bindings against the
  live model's signal list (`--dump` lists all readable/writable names).
- **`hil/schematic/live_plot.py`** — host-side rolling plot (no Docker) of PCC
  power, battery power/command, and SOC, read straight from InfluxDB.
- **Interactive load slider** — add a SCADA input `load_demand_kW` (0–500) plus a
  `load_demand_probe` (the HIL API can't read a SCADA input back, so the bridge
  reads the probe); the bridge then drives the flexible-load base demand live, so
  you can step the load and watch the BESS hold PCC at setpoint. See `4b` in
  `hil/schematic/RIG_CLOSED_LOOP.md`.
- **`configs/asset_config.{rig,docker}.yaml`** / **`edge_ems_config.{rig,docker}.yaml`**
  — rig wiring for the host-process (`127.0.0.1`, localhost endpoints) and
  container (`host.docker.internal`, service-name endpoints) topologies.
- **`deploy/docker-compose.rig.yml`** — overlay that runs `core`/`controller` as
  containers against the host bridge (excludes the SIL `sim-*` devices).

Once the loop is up, **`hil/schematic/RIG_FEATURE_GUIDE.md`** is the step-by-step
guide for exercising each controller feature on the rig — self-consumption
(the default active-power loop) and tariff PFC (§5) — with exact config edits,
restart/rebuild steps, and how to verify each one from InfluxDB/SCADA.

The legacy scenario/action-planner rig path and the component list are in
**`hil/README.md`**.

---

### 8.5 External-EMS takeover & release (HIL Scenario 1)

Scenario 1 makes the edge-EMS follow an **external EMS** that publishes PCC active-power
setpoints, **take over** to self-consumption if that EMS goes silent past a watchdog
timeout, and **release** control back when it returns. All of it lives in a new opt-in
container (`deploy/ext-ems/`, compose profile `ext-ems`); the only existing-code change is
a controller `POST /setpoint` that sets the **live** PCC target without a restart.

```
  emulator --MQTT(site/<id>/external/pcc_setpoint)--> gateway + watchdog
   (random P*, 5-min cadence,                          (freshness clock;
    simulated outage window)                            FOLLOWING <-> SELF_CONSUMPTION)
                                                          |  +--> GET /status (:5010)
                                                          |  +--> InfluxDB external_ems
                                                          v  HTTP POST /setpoint (live)
                                          controller --MQTT--> core --> bridge --> plant
```

State machine: an external message is *fresh* while its age is `< EXT_WATCHDOG_TIMEOUT_S`;
at the timeout the gateway takes over (`SELF_CONSUMPTION`, PCC target → 0); a later fresh
message releases it back to `FOLLOWING_EXTERNAL`. Startup with no message yet → self-consumption
(safe default). Transitions are logged once and written to InfluxDB.

Run it in **test mode** (`TIME_SCALE=60` compresses the 5/6/10-min scenario into ~25 s):

```powershell
docker compose --env-file configs/.env `
  -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml `
  --profile services --profile ext-ems up -d --build      # TIME_SCALE defaults to 60
curl.exe -X POST http://localhost:5000/loop/start

# watch all three:
curl.exe http://localhost:5000/status          # controller: pcc_setpoint_kw, pcc_error_kw
curl.exe http://localhost:5010/status          # gateway: state, last_external_age_s, takeover_count
python -m hil.schematic.live_plot               # active_source trace (ext/self) vs PCC power + SOC
```

Phases to confirm: **A (following)** PCC tracks the random external P\* each cadence; **B
(takeover)** during the outage, after the watchdog timeout, gateway → `SELF_CONSUMPTION`,
controller setpoint → 0, PCC → 0; **C (release)** on reconnect, gateway → `FOLLOWING_EXTERNAL`
and PCC resumes tracking. Set `TIME_SCALE=1` for real 5/6/10-min timing. The profile is
**absent** from the normal A–E runs, so it never overrides their setpoints.

Configuration (env, with defaults; all timings divide by `TIME_SCALE`):

| Env var | Default | Meaning |
|---|---|---|
| `EXT_PUBLISH_INTERVAL_S` | `300` | external publish cadence |
| `EXT_WATCHDOG_TIMEOUT_S` | `360` | silence before takeover |
| `EXT_OUTAGE_AT_S` / `EXT_RECONNECT_AT_S` | `600` / `1200` | emulator silence window |
| `EXT_P_MIN_KW` / `EXT_P_MAX_KW` | `-300` / `400` | random P\* range (within PCC limits) |
| `TIME_SCALE` | `60` (compose) | test-mode time compression (`1` = real timing) |
| `STATUS_PORT` | `5010` | gateway `/status` |

Tests: `python -m pytest deploy/ext-ems/tests` (watchdog state machine + gateway integration
with fakes) and the controller `POST /setpoint` contract tests in
`tests/contract/test_http_api.py`.

---

## 9. Validation gates & current status

- **Gate G2 (SIL):** all seven scenarios pass against the full Dockerized
  pymodbus simulator stack (`bash tests/sil/run_sil.sh`, or `run_sil.ps1` on
  Windows). Two follow-ups surfaced while getting here (config `${VAR}`
  expansion, hot reload not applied to the running loop) were controller
  changes left out per rule #3 at the time — both are now resolved; see
  `KNOWN_ISSUES.md`.
- **Gate G3 (CHIL):** the seven scenarios + three fault injections (InfluxDB
  write stall, MQTT broker loss, asset/battery dropout) pass on the Typhoon
  HIL606, with the 50-asset loop staying under the 250 ms budget.
- **External-EMS takeover & release (S1):** implemented as the opt-in `ext-ems`
  gateway (§8.5) driving the controller's live `POST /setpoint`. The watchdog
  state machine and gateway are unit/integration tested
  (`deploy/ext-ems/tests`, `tests/contract/test_http_api.py`); on-rig execution
  uses the procedure in §8.5 and `hil/schematic/RIG_CLOSED_LOOP.md`.

The CHIL logic, register parity, PI tuning, and loop budget are **green in the
software plant-in-the-loop today** (see `hil/reports/chil_report.md`). PI tuning
moved the defaults from `Kp=0.5, Ki=0.1` (settled ~41 kW, missed the 5 kW band)
to **`Kp=0.5, Ki=0.5`** (settles <0.2 kW in ~9 s; anti-windup recovery in 3
cycles), committed to `configs/edge_ems_config.example.yaml`. On the real rig,
comms latency (bridge + Modbus + InfluxDB + MQTT) makes those same gains
underdamped; `hil/tuning/tune_pi_latency.py` recommends **`Kp=0.3, Ki=0.2`**
for that path (zero first-swing overshoot, ~12-15 s settle at 1-2 cycles of
delay), committed to `configs/edge_ems_config.{rig,docker}.yaml`.

- **Tariff PFC — confirmed on the Typhoon rig (2026-07-28):** with `pfc.enabled`
  and the `load-02`/`meter-01` fixed-load meter wired into the `meter`
  aggregate, enabling PFC dropped measured PCC reactive power from ~47-50 kVAr
  to ~2.4-2.6 kVAr — the battery absorbing essentially all of the fixed load's
  reactive draw. `q_sign_convention: 1` is confirmed correct (Q moved toward
  the target, not away from it). One caveat worth remembering: PF reads near 0
  whenever PCC active power is near 0 (self-consumption's normal operating
  point) — `PF = P/√(P²+Q²)` is degenerate at `P≈0` regardless of how well Q is
  controlled, so don't read a low PF display as PFC failing to work. See
  `hil/schematic/RIG_FEATURE_GUIDE.md` for the full walkthrough and how to
  verify it.
- **Config-drift gotcha found during that verification:** `edge_ems_config.docker.yaml`
  (the container path) had silently drifted from `edge_ems_config.rig.yaml`
  (the host-process path) — old `Kp=0.5, Ki=0.5` and no `pfc:` block at all, so
  the containerized controller was running neither the tuned gains nor PFC
  despite both being "configured" in the repo. There's no validation that
  catches this; the two files must be checked for parity by hand whenever
  either changes.

> **⚠ Firmware verification is an OPEN BLOCKER for the G3 hardware sign-off.**
> The register maps in `maps/` are a reconstructed **DRAFT**: they load and pass
> the contract tests, but their addresses/types/scale factors were inferred to
> be internally consistent and have **not** been checked against the as-built
> inverter firmware (plan task 6). Because the controller and the HIL servers
> read the *same* map files, parity holds *by construction* even if a map is
> wrong — a bad address or scale is invisible off-device and silently breaks
> parity on the real inverter. Reconcile `maps/*.yaml` against the firmware dump
> (`scripts/read_real_inverter.py`) with the firmware owner before sign-off.

---

## 10. Project rules

1. Nothing crosses an interface without a name from `data_model.yaml`; never
   redefine names, units, or signs.
2. Services never 