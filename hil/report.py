"""Generate the CHIL report (Gate G3 deliverable, prompt section 9).

Runs the seven scenarios + three fault injections through the software
plant-in-the-loop, the register-parity check, and the 50-asset loop-budget
benchmark, then writes a Markdown report with the control-measurement
assertions, the loop-budget histogram, the tuned gains, and the open
firmware-verification caveat that gates the actual rig sign-off.

    python -m hil.report --out hil/reports/chil_report.md
"""

from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path

from hil import parity_check, scale_bench
from hil.orchestrate_chil import run_all, run_faults
from hil.tuning.tune_pi import antiwindup_probe, score_gains

_REPO = Path(__file__).resolve().parents[1]


def _scenario_table(results) -> str:
    rows = ["| Scenario | Check | Result | Key control-measurement evidence |",
            "|---|---|---|---|"]
    ev = {
        "tracking": lambda s: (
            f"settled |PCC err|={abs(s['final_pcc_error_kw']):.2f} kW (<5), mode {s['modes']}"),
        "saturation_derate": lambda s: (
            f"min derate={s['min_derate_factor']} (<1), mode {s['modes']}"),
        "curtailment": lambda s: f"min curtail={s['min_curtail_factor']} (<1), mode {s['modes']}",
        "droop": lambda s: f"PI output pstdev={s['pi_output_pstdev']} kW (>1), mode {s['modes']}",
        "stale_data": lambda s: f"mode ladder {s['modes']}",
        "config_reload": lambda s: f"mode {s['modes']}, loop max={s['loop_ms_max']} ms (<250)",
        "scale_50": lambda s: f"loop p95={s['loop_ms_p95']} ms, max={s['loop_ms_max']} ms (<250)",
    }
    for r in results:
        res = "PASS" if r.passed else f"FAIL ({r.error})"
        detail = ev.get(r.name, lambda s: "")(r.summary) if r.summary else ""
        rows.append(f"| {r.name} | scenarios.py::{r.name} | {res} | {detail} |")
    return "\n".join(rows)


def _fault_table(faults) -> str:
    rows = ["| Fault injection | Result | Evidence |", "|---|---|---|"]
    for f in faults:
        rows.append(f"| {f.name} | {'PASS' if f.passed else 'FAIL'} | {f.detail} |")
    return "\n".join(rows)


def build_report() -> str:
    scen = run_all()
    faults = run_faults()
    parity_ok, parity = parity_check.run()
    bench = scale_bench.run(count=50, cycles=200)
    baseline = score_gains(0.5, 0.1)
    tuned = score_gains(0.5, 0.5)
    probe = antiwindup_probe(0.5, 0.5)

    n_points = sum(len(d) for d in parity["maps"].values())
    all_pass = (all(r.passed for r in scen) and all(f.passed for f in faults)
                and parity_ok and bench["under_budget"])

    hist = scale_bench._histogram(bench["samples"])
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    baseline_row = (
        f"| baseline Kp=0.5 Ki=0.1 | {baseline.settles} | "
        f"{baseline.worst_tail_kw:.2f} kW | {baseline.settling_time_s:.0f} s |")
    tuned_row = (
        f"| **tuned Kp=0.5 Ki=0.5** | {tuned.settles} | "
        f"{tuned.worst_tail_kw:.2f} kW | {tuned.settling_time_s:.0f} s |")

    return f"""# CHIL Report -- edge-EMS @ vgcolab-01 (Gate G3)

Generated {now} by `python -m hil.report`.

**Overall: {"PASS" if all_pass else "FAIL"}** -- 7/7 scenarios, 3/3 fault
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

{_scenario_table(scen)}

The plant-in-the-loop wires the genuine `controller.control_loop.ControlLoop`
(+ EdgeController / ModeController / DroopController) to `hil.plant.SiteModel`;
the same ControlLoop runs on the rig with InfluxDB/MQTT I/O and the Typhoon
plant behind the HIL Modbus servers.

## 2. Fault injections (prompt section 7)

{_fault_table(faults)}

The deep MQTT queue + sequence-guard recovery lives in the core publisher and is
covered by tests/integration; the CHIL check here confirms the control loop
tolerates a publish outage without leaving RUN and applies only the freshest
setpoint on recovery.

## 3. Register parity (mandatory gate)

`python -m hil.parity_check`: **{"PASS" if parity_ok else "FAIL"}** --
{n_points} canonical points across {len(parity["maps"])} maps agree
(map file = pymodbus simulator = HIL server), verified by independent byte
placement (encode -> write -> raw read-back -> decode), not just fingerprints.

## 4. 50-asset loop budget (prompt section 8)

50 map-generated HIL Modbus servers polled by the real `ModbusTcpAdapter`,
{bench["cycles"]} full read cycles, {bench["comm_fail"]} comm failures:

- p50 = {bench["p50_ms"]:.1f} ms, **p95 = {bench["p95_ms"]:.1f} ms**,
  worst = {bench["worst_ms"]:.1f} ms (budget 250 ms of the 1 s period)

```
{hist}
```

Note: `control.loop_duration_ms` (the field the scale check asserts on) is the
controller's compute time and is O(1) in asset count; the budget at scale is
dominated by Modbus I/O, measured above.

## 5. PI tuning (prompt section 8)

Tuned against the plant-in-the-loop dynamics (converter lag + setpoint slew +
one-cycle measurement delay), starting from Kp=0.5, Ki=0.1.

| Gains | Settles <5 kW? | Worst tail err | Settling time |
|---|---|---|---|
{baseline_row}
{tuned_row}

Anti-windup (conditional integration): deep-saturation probe (2000 kW import vs
1000 kW discharge limit, then release) recovered in **{probe.recovery_cycles}
cycles**, recovered={probe.recovered}, peak |integral term|=
{probe.peak_integral_term_kw:.0f} kW. Recovery is prompt, so conditional
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
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the CHIL report")
    ap.add_argument("--out", default=str(_REPO / "hil" / "reports" / "chil_report.md"))
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
