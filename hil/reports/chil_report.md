# CHIL Report -- edge-EMS @ vgcolab-01 (Gate G3)

Generated 2026-06-19 11:15 UTC by `python -m hil.report`.

**Overall: PASS** -- 7/7 scenarios, 3/3 fault
injections, register parity, and the 50-asset loop budget, run as a software
plant-in-the-loop against the real controller core.

> **G3 sign-off is BLOCKED on firmware verification (open).** The register maps
> are a reconstructed DRAFT; addresses/types/scales were inferred to be
> internally consistent but have NOT been checked against the as-built inverter
> firmware (plan task 6). The HIL servers and the controller read the *same* map
> files, so parity holds *by construction* in this report -- but a wrong address
> or scale in the maps is invisible here and silently breaks parity on the real
> device. Reconcile the maps with the firmware owner before the rig sign-off.

## 1. Scenarios (oracle: tests/sil/scenarios.py, asserted on the `control` measurement)

| Scenario | Check | Result | Key control-measurement evidence |
|---|---|---|---|
| tracking | scenarios.py::tracking | PASS | settled |PCC err|=0.00 kW (<5), mode RUN |
| saturation_derate | scenarios.py::saturation_derate | PASS | min derate=0.2 (<1), mode RUN |
| curtailment | scenarios.py::curtailment | PASS | min curtail=0.907 (<1), mode RUN |
| droop | scenarios.py::droop | PASS | PI output pstdev=95.03 kW (>1), mode RUN |
| stale_data | scenarios.py::stale_data | PASS | mode ladder RUN->HOLD->SAFE |
| config_reload | scenarios.py::config_reload | PASS | mode RUN, loop max=0.008 ms (<250) |
| scale_50 | scenarios.py::scale_50 | PASS | loop p95=0.003 ms, max=0.005 ms (<250) |

The plant-in-the-loop wires the genuine `controller.control_loop.ControlLoop`
(+ EdgeController / ModeController / DroopController) to `hil.plant.SiteModel`;
the same ControlLoop runs on the rig with InfluxDB/MQTT I/O and the Typhoon
plant behind the HIL Modbus servers.

## 2. Fault injections (prompt section 7)

| Fault injection | Result | Evidence |
|---|---|---|
| influx_write_stall | PASS | modes RUN->HOLD->SAFE->RUN; recovered_to_RUN=True |
| mqtt_broker_loss | PASS | modes={'RUN'}; dropped_publishes=7; freshest_applied=True |
| asset_dropout_recovery | PASS | pv-dropout kept running=True; battery-dropout->SAFE=True; recovered=True; modes=RUN->SAFE->RUN |

The deep MQTT queue + sequence-guard recovery lives in the core publisher and is
covered by tests/integration; the CHIL check here confirms the control loop
tolerates a publish outage without leaving RUN and applies only the freshest
setpoint on recovery.

## 3. Register parity (mandatory gate)

`python -m hil.parity_check`: **PASS** --
18 canonical points across 4 maps agree
(map file = pymodbus simulator = HIL server), verified by independent byte
placement (encode -> write -> raw read-back -> decode), not just fingerprints.

## 4. 50-asset loop budget (prompt section 8)

50 map-generated HIL Modbus servers polled by the real `ModbusTcpAdapter`,
200 full read cycles, 0 comm failures:

- p50 = 4.3 ms, **p95 = 7.3 ms**,
  worst = 30.3 ms (budget 250 ms of the 1 s period)

```
      <1ms |                                          0
      <2ms |                                          0
      <5ms | ################################         161
     <10ms | #######                                  35
     <25ms |                                          1
     <50ms |                                          3
    <100ms |                                          0
    <250ms |                                          0
   >=250ms |                                          0
```

Note: `control.loop_duration_ms` (the field the scale check asserts on) is the
controller's compute time and is O(1) in asset count; the budget at scale is
dominated by Modbus I/O, measured above.

## 5. PI tuning (prompt section 8)

Tuned against the plant-in-the-loop dynamics (converter lag + setpoint slew +
one-cycle measurement delay), starting from Kp=0.5, Ki=0.1.

| Gains | Settles <5 kW? | Worst tail err | Settling time |
|---|---|---|---|
| baseline Kp=0.5 Ki=0.1 | False | 41.32 kW | 30 s |
| **tuned Kp=0.5 Ki=0.5** | True | 0.13 kW | 9 s |

Anti-windup (conditional integration): deep-saturation probe (2000 kW import vs
1000 kW discharge limit, then release) recovered in **3
cycles**, recovered=True, peak |integral term|=
802 kW. Recovery is prompt, so conditional
integration is kept; revisit back-calculation only if a future plant shows slow
recovery. Tuned gains committed to `configs/edge_ems_config.example.yaml`.

## 6. Findings to resolve before rig sign-off

1. **Firmware-unverified maps (blocker).** Reconcile maps/*.yaml against the
   firmware dump (see scripts/read_real_inverter.py) with the firmware owner.
2. **Feed-limit vs PV capacity.** The PCC feed-in limit (999 kVA) exceeds total
   site generation (PV 600 kVA; a full battery cannot export), so feed-limit PV
   curtailment cannot bind at nameplate. The curtailment scenario models a PV
   plant sized to the stated surplus; confirm the intended site sizing / limit.
3. **Run on the rig.** Re-run this report on the Typhoon HIL606 (real plant +
   InfluxDB/MQTT) via hil.schematic + hil.orchestrate_chil.build_rig_actions to
   convert this software-PIL pass into a hardware G3 pass.
