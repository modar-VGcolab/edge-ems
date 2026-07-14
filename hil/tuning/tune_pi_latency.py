"""Latency-aware PI overshoot analysis (companion to tune_pi.py).

`tune_pi.py` scores gains against the nominal plant-in-the-loop, where the only
dynamics are the converter first-order lag and the setpoint slew limit. On that
model Kp=0.5/Ki=0.5 settles fast with ~no overshoot. On the RIG, however, the
self-consumption transient is underdamped (a large first swing and several
decaying overshoot cycles) -- the Results document attributes this to the extra
loop latency of the bridge + Modbus + InfluxDB + MQTT path, which the nominal
model omits. That latency is dead time: it erodes phase margin, so the gains that
look critically damped on the nominal model ring on the rig.

This tool adds an explicit measurement transport delay (D cycles of dead time) to
a self-contained closed loop built around the REAL EdgeController, then sweeps
Kp/Ki and measures the first-swing overshoot, settling time and tail ripple for a
load-step disturbance (the A1 scenario). It recommends the most damped gain set
that still settles, so the rig transient has margin against that latency.

    python -m hil.tuning.tune_pi_latency            # table + recommendation
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from controller.edge_controller import (
    BatteryLimits,
    ControlInputs,
    ControlParams,
    EdgeController,
)

DT = 1.0                 # control/update period [s]
TAU_S = 0.2              # battery converter first-order tracking lag [s]
SLEW_KW_S = 200.0        # battery setpoint slew limit [kW/s]
LOAD_STEP_KW = 150.0     # A1 disturbance: flexible-load step [kW]
SETTLE_BAND_KW = 5.0     # |PCC| deadband for "settled"
HORIZON_S = 60


@dataclass
class Score:
    kp: float
    ki: float
    delay_cycles: int
    overshoot_kw: float      # worst export swing past the 0 kW target (first swing)
    settle_s: float          # time to enter & stay in the band (inf if never)
    tail_ripple_kw: float    # peak-to-peak |PCC| over the last 10 s
    settles: bool

    @property
    def cost(self) -> float:
        base = 0.0 if self.settles else 1e6
        return base + self.overshoot_kw + 0.5 * self.settle_s + 2.0 * self.tail_ripple_kw


def _simulate(kp: float, ki: float, delay_cycles: int) -> Score:
    """Closed loop: a +LOAD_STEP load disturbance at t=0; the PI commands the
    battery (slew-limited inside the controller); the converter tracks the command
    with a first-order lag; PCC = load + battery_actual; the controller sees PCC
    delayed by `delay_cycles` (the comms dead time)."""
    params = ControlParams(kp=kp, ki=ki, update_period=DT, slew_limit_kw_s=SLEW_KW_S)
    # Big SoC window + headroom so the gates/clamps never interfere with the tuning.
    batt = BatteryLimits(max_charge_kw=1000.0, max_discharge_kw=1000.0,
                         min_soc_pct=5.0, max_soc_pct=95.0)
    ctrl = EdgeController(params, batt)

    alpha = 1.0 - math.exp(-DT / TAU_S)
    batt_actual = 0.0
    pcc_now = LOAD_STEP_KW             # PCC = load + battery_actual(=0)
    meas_buf = [pcc_now] * (delay_cycles + 1)

    n = int(HORIZON_S / DT)
    pcc_series: list[float] = []
    for _ in range(n):
        pcc_seen = meas_buf[0]         # delayed measurement the controller acts on
        out = ctrl.step(ControlInputs(
            pcc_meas_kw=pcc_seen, pcc_setpoint_kw=0.0, soc_pct=50.0,
            avail_charge_kw=1000.0, avail_discharge_kw=1000.0,
        ))
        # converter tracks the commanded setpoint with the first-order lag
        batt_actual += alpha * (out.battery_setpoint_kw - batt_actual)
        pcc_now = LOAD_STEP_KW + batt_actual
        meas_buf = meas_buf[1:] + [pcc_now]
        pcc_series.append(pcc_now)

    overshoot = max(0.0, -min(pcc_series))                 # export swing past 0
    tail = pcc_series[-int(10 / DT):]
    tail_ripple = max(tail) - min(tail)
    settle_s = float("inf")
    for i in range(len(pcc_series)):
        if all(abs(p) <= SETTLE_BAND_KW for p in pcc_series[i:]):
            settle_s = i * DT
            break
    return Score(kp, ki, delay_cycles, overshoot, settle_s, tail_ripple,
                 settles=math.isfinite(settle_s))


def main() -> int:
    kps = [0.3, 0.5, 0.8]
    kis = [0.1, 0.2, 0.3, 0.5, 0.8]
    for delay in (1, 2):
        print(f"\n=== measurement transport delay D = {delay} cycle(s) "
              f"({delay * DT:.0f} s dead time) ===")
        scores = [_simulate(kp, ki, delay) for kp in kps for ki in kis]
        scores.sort(key=lambda s: s.cost)
        print(f"{'Kp':>4} {'Ki':>4} {'overshoot':>10} {'settle':>7} "
              f"{'tail_pp':>8} {'settles':>8}")
        for s in scores:
            st = f"{s.settle_s:.0f}s" if s.settles else "no"
            print(f"{s.kp:>4} {s.ki:>4} {s.overshoot_kw:>9.1f}kW {st:>7} "
                  f"{s.tail_ripple_kw:>7.1f} {str(s.settles):>8}")
        best = scores[0]
        print(f"recommend @ D={delay}: Kp={best.kp} Ki={best.ki}  "
              f"overshoot={best.overshoot_kw:.1f} kW  settle={best.settle_s:.0f}s  "
              f"tail_pp={best.tail_ripple_kw:.1f} kW")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
