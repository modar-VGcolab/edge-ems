# CHIL rig — edge-EMS plant for Typhoon HIL606 (Gate G3)

The **plant side** of the edge-EMS controller: a Typhoon HIL606 schematic, the
Modbus TCP servers it exposes, the profile-injection scripts, and the
orchestration that reuses the SIL scenario oracle. The controller/orchestrator
already exist and are SIL-validated; nothing here changes controller code.

## Guiding principle — parity by construction

Every HIL Modbus server is generated from the **same register-map file**
(`maps/*.yaml`) that `core.py` reads, via the **same codec**
(`common.modbus_codec`) the pymodbus simulator uses. We never hand-place a
register. So the controller cannot tell the simulator from the schematic, and
`hil/parity_check.py` *proves* map = simulator = HIL server for every canonical
point before any scenario runs.

## ⚠️ Firmware verification is an OPEN BLOCKER for G3 sign-off

The register maps are a **reconstructed DRAFT**. They load and pass the contract
tests, but addresses, types and scale factors were *inferred to be internally
consistent* — they have **not** been checked against the as-built inverter
firmware (plan task 6). Because the HIL servers and the controller both read
these files, parity holds *by construction* in every run here even if a map is
wrong; a bad address or scale is **invisible off the device** and silently
breaks parity on the real inverter. **Before the rig sign-off, reconcile each
point's address/type/scale against the firmware dump
(`scripts/read_real_inverter.py`) with the firmware owner and correct the maps.**

**Status (2026-06-22): tooling delivered, blocker still OPEN.** The
reconciliation tooling now exists and is tested (`scripts/sunspec_discovery.py`,
`scripts/diff_dump_vs_map.py`, `scripts/read_real_inverter.py discover`, and
`tests/tools/`): it walks a live device or a saved raw register dump, records the
device's real `sunssf` values and model placement, and diffs that against each
generated map. What is still missing is the **input**: no real device or saved
firmware dump is available yet, so no map has been checked against real
hardware. The blocker stays OPEN until a real per-asset dump has been diffed,
any discrepancies reconciled in `maps/sunspec/generate.py` (then regenerated),
and a real read/write round-trip recorded. See `reconciliation/README.md` for
how to run it and the (synthetic) sample artifacts.

## Layout

```
hil/
  plant/models.py        Pure plant physics + signed PCC balance (Typhoon-free)
  servers.py             Map-driven Modbus TCP servers (parity by construction)
  parity_check.py        Mandatory gate: map = simulator = HIL server
  chil_runner.py         Plant-in-the-loop: real ControlLoop vs SiteModel
  profiles.py            One parametrized stimulus per scenario (+ SCADA CSVs)
  orchestrate_chil.py    7 scenarios (scenarios.py oracle) + 3 fault injections
  tuning/tune_pi.py      PI tuning + anti-windup probe against plant dynamics
  scale_bench.py         50 real Modbus servers, loop-budget histogram
  report.py              Generates reports/chil_report.md
  schematic/             Typhoon API: build_schematic.py + signal_bridge.py (rig)
  tests/                 Repeatable CHIL checks (run in plain pytest)
```

## Run it (off-rig, here)

```bash
python -m hil.parity_check          # register parity (gate) — must be green first
python -m hil.orchestrate_chil      # 7 scenarios + 3 fault injections (plant-in-loop)
python -m hil.tuning.tune_pi        # sweep gains + anti-windup probe (--write to commit)
python -m hil.scale_bench           # 50-asset loop-budget histogram
python -m hil.report                # full CHIL report -> hil/reports/chil_report.md
pytest hil/tests -q                 # repeatable regression suite
```

The off-rig path uses `hil.chil_runner.PlantInTheLoop`, which closes the loop
with the **genuine** `controller.control_loop.ControlLoop` (and the real
EdgeController / ModeController / DroopController) against `hil.plant.SiteModel`.
A green run is strong evidence the rig run satisfies the same `scenarios.py`
checks, since the controller code is identical.

## Run it (on the Typhoon HIL606)

```bash
# 1. Build & compile the plant model (Typhoon HIL Control Center required)
python -m hil.schematic.build_schematic --out hil/schematic/lux_moura_01.tse --compile
# 2. Bring up the controller stack (InfluxDB/MQTT/core/controller), then bridge
#    model signals to the map-generated Modbus servers and play a scenario:
python -m hil.schematic.signal_bridge --scenario tracking
# 3. Drive scenarios via the existing SIL action planner (HTTP API + docker faults)
#    reused through hil.orchestrate_chil.build_rig_actions(<name>).
```

The schematic exposes each asset's measurements/setpoints as model signals;
`signal_bridge.py` copies signal⇄register by canonical name so the register
layout always comes from the maps. The seven scenarios map to plant stimuli in
`profiles.py`; their timelines are pushed onto the model's SCADA inputs.

The external-EMS takeover/release scenario (S1) runs differently — at the MQTT
interface, not via a plant stimulus — using the opt-in `ext-ems` container
(emulator + watchdog + gateway driving the controller's live `POST /setpoint`).
See `hil/schematic/RIG_CLOSED_LOOP.md` §4c and README §8.5.

## Tuned defaults

PI tuning against the plant dynamics moved the defaults from `Kp=0.5, Ki=0.1`
(settles ~41 kW, misses the 5 kW band) to **`Kp=0.5, Ki=0.5`** (settles <0.2 kW
in ~9 s, no overshoot, anti-windup recovery in 3 cycles), committed to
`configs/edge_ems_config.example.yaml`.

## Constraints honored

- `data_model.yaml` is normative; names/units/signs are never redefined.
- No controller code changes to make CHIL pass. If a control bug surfaces, fix
  it in the controller with a regression test at the lowest level that
  reproduces it, then re-run.
- The schematic and scripts live here in the repo so CHIL is repeatable before
  every release.
