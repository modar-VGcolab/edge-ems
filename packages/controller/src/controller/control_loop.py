"""Controller main loop (plan task 26 wiring; ties together tasks 19-23).

One cycle (system design 4): read aggregate+pcc from InfluxDB, judge freshness,
run the mode ladder, and -- only in RUN -- apply droop then the PI/priority-ladder
core, before publishing setpoints over MQTT and writing the `control` measurement.

The loop owns no algorithms; it sequences the pieces:
  data_connector  -> reads
  modes           -> RUN / HOLD / SAFE supervision
  droop           -> PCC setpoint correction (RUN only)
  edge_controller -> battery setpoint + derate/curtail (RUN only)
  publisher       -> MQTT setpoints (battery every cycle; derates on change)

I/O (reader, publisher, control writer) is injected so the whole cycle is
unit-testable with fakes; the live InfluxDB/MQTT paths are exercised in SIL.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from common.config_models import AssetConfigFile, EdgeEmsConfigFile
from common.points import COMM_FAIL, GOOD, PointValue

from controller.droop import DroopController
from controller.pfc import PfcController
from controller.edge_controller import (
    BatteryLimits,
    ControlInputs,
    ControlParams,
    DerateLimits,
    EdgeController,
)
from controller.modes import Mode, ModeController, safe_battery_setpoint

# Setpoint field names (data_model.yaml, direction: output).
BATTERY_P = "active_power_setpoint_kw"
BATTERY_Q = "reactive_power_setpoint_kvar"
DERATE = "derate_factor_setpoint"


@dataclass
class Snapshot:
    """One cycle's raw reads. `aggregates` is class -> field -> PointValue; `pcc`
    is the single PCC asset's points (pcc is never aggregated)."""

    aggregates: dict[str, dict[str, PointValue]] = field(default_factory=dict)
    pcc: dict[str, PointValue] = field(default_factory=dict)


@dataclass(frozen=True)
class CycleResult:
    mode: str
    mode_changed: bool
    mode_reason: str
    battery_setpoint_kw: float
    reactive_setpoint_kvar: float
    derate_factor: float
    curtail_factor: float
    pcc_error_kw: float
    pi_output_kw: float
    loop_duration_ms: float
    data_fresh: bool
    battery_ok: bool
    published: tuple[str, ...]  # asset classes published this cycle


def control_measurement_fields(r: CycleResult) -> dict[str, float | str]:
    """The `control` InfluxDB measurement (system design 3.3) for this cycle."""
    return {
        "pcc_error_kw": r.pcc_error_kw,
        "pi_output_kw": r.pi_output_kw,
        "derate_factor": r.derate_factor,
        "curtail_factor": r.curtail_factor,
        "loop_duration_ms": r.loop_duration_ms,
        "mode": r.mode,
    }


def _val(pv: PointValue | None, default: float = 0.0) -> float:
    return default if pv is None or pv.value is None else float(pv.value)


def _fresh(pv: PointValue | None) -> bool:
    return pv is not None and pv.value is not None and pv.quality == GOOD


# PCC measurement plausibility (grid-loss / islanding guard). On a grid loss the
# PCC meter collapses to ~0 V / 0 Hz (or wild values, incl. negative). Without
# this gate the controller stays in RUN because PCC active power coincidentally
# sits at the 0 kW self-consumption setpoint, so it never notices the island.
# Implausible PCC V/Hz is folded into `data_fresh`, so the mode ladder fails safe
# (HOLD -> SAFE) and auto-recovers when the grid returns. Bounds suit a 50 Hz /
# 230 V nominal site; normal +-10% droop excursions (207-253 V, 49.5-50.5 Hz)
# stay well inside.
PCC_V_MIN_V = 50.0       # below this volts = collapsed / lost grid
PCC_F_MIN_HZ = 45.0
PCC_F_MAX_HZ = 55.0


def _pcc_plausible(volt_pt: PointValue | None, freq_pt: PointValue | None) -> bool:
    """True only when PCC voltage & frequency are fresh AND physically plausible
    (a live grid). A collapsed/islanded grid (~0 V / 0 Hz) returns False."""
    return (
        _fresh(volt_pt) and _val(volt_pt) >= PCC_V_MIN_V
        and _fresh(freq_pt) and PCC_F_MIN_HZ <= _val(freq_pt) <= PCC_F_MAX_HZ
    )


def _battery_ok(batt: dict[str, PointValue]) -> bool:
    """The battery aggregate is controllable only when its SoC is present and not
    COMM_FAIL, AND no converter alarm is asserted.

    A tripped converter keeps publishing SoC (a BMS reading), so SoC-presence
    alone reads a faulted battery as available -- the gap behind the E3 islanding
    finding, where the BESS tripped on grid-voltage-out-of-range yet `battery_ok`
    stayed True. An explicit `converter_alarm` (non-zero = fault/trip) closes it.

    The alarm is honoured only when it arrives GOOD, so an absent/un-wired or
    COMM_FAIL alarm never forces SAFE on its own: behaviour is unchanged until a
    real converter-status signal is mapped through to the aggregate."""
    soc = batt.get("soc_pct")
    if soc is None or soc.value is None or soc.quality == COMM_FAIL:
        return False
    alarm = batt.get("converter_alarm")
    if (
        alarm is not None
        and alarm.quality == GOOD
        and alarm.value is not None
        and float(alarm.value) != 0.0
    ):
        return False
    return True


def assemble_inputs(
    snap: Snapshot, *, pcc_setpoint_kw: float, max_feed_kw: float | None
) -> tuple[ControlInputs, float, float, bool, bool]:
    """Build ControlInputs plus droop f/V and the (data_fresh, battery_ok) flags.

    battery_ok is False when the battery aggregate is absent/COMM_FAIL or its
    converter reports an alarm (an unrecoverable loss -> SAFE; see `_battery_ok`).
    data_fresh requires every controlled input to be GOOD; a present-but-STALE
    input yields not-fresh, which drives HOLD.
    """
    batt = snap.aggregates.get("battery", {})
    pv = snap.aggregates.get("pv", {})
    pcc = snap.pcc

    soc = batt.get("soc_pct")
    battery_ok = _battery_ok(batt)
    pcc_p = pcc.get("active_power_kw")
    charge_hl = batt.get("available_charge_power_kw")
    discharge_hl = batt.get("available_discharge_power_kw")
    volt_pt = pcc.get("voltage_v")
    freq_pt = pcc.get("frequency_hz")
    # Grid-loss / islanding guard: implausible PCC V/Hz (collapse to ~0) counts as
    # not-fresh, so the loop fails safe instead of regulating against a dead grid.
    grid_ok = _pcc_plausible(volt_pt, freq_pt)
    data_fresh = (
        grid_ok
        and battery_ok
        and all(_fresh(x) for x in (pcc_p, soc, charge_hl, discharge_hl))
    )

    inputs = ControlInputs(
        pcc_meas_kw=_val(pcc_p),
        pcc_setpoint_kw=pcc_setpoint_kw,
        soc_pct=_val(soc),
        avail_charge_kw=_val(charge_hl),
        avail_discharge_kw=_val(discharge_hl),
        pv_active_power_kw=_val(pv.get("active_power_kw")),
        max_feed_kw=max_feed_kw,
    )
    freq = _val(freq_pt, 50.0)
    volt = _val(volt_pt, 230.0)
    return inputs, freq, volt, data_fresh, battery_ok


class ControlLoop:
    def __init__(
        self,
        *,
        edge: EdgeController,
        modes: ModeController,
        droop: DroopController | None,
        read_snapshot: Callable[[float], Snapshot],
        publish: Callable[[str, dict[str, float], float], bool],
        pcc_base_kw: float,
        max_feed_kw: float | None,
        pcc_setpoint_kw: float = 0.0,
        write_control: Callable[[dict[str, float | str], float], None] | None = None,
        pfc: PfcController | None = None,
    ):
        self.edge = edge
        self.modes = modes
        self.droop = droop
        self.pfc = pfc
        self.read_snapshot = read_snapshot
        self.publish = publish
        self.pcc_base_kw = pcc_base_kw
        self.max_feed_kw = max_feed_kw
        self.pcc_setpoint_kw = pcc_setpoint_kw
        self.write_control = write_control
        self._last_reactive = 0.0
        self._last_pub: dict[str, float] = {}  # asset_class -> last published derate

    def run_once(self, now: float | None = None) -> CycleResult:
        now = time.time() if now is None else now
        snap = self.read_snapshot(now)
        inputs, freq, volt, data_fresh, battery_ok = assemble_inputs(
            snap, pcc_setpoint_kw=self.pcc_setpoint_kw, max_feed_kw=self.max_feed_kw
        )
        decision = self.modes.update(data_fresh=data_fresh, battery_ok=battery_ok, now=now)

        if decision.mode == Mode.RUN:
            dp = dq = 0.0
            if self.droop is not None and self.droop.enabled:
                corr = self.droop.correction(freq, volt)
                dp, dq = corr.dp_kw, corr.dq_kvar
            out = self.edge.step(replace(inputs, pcc_setpoint_kw=self.pcc_setpoint_kw + dp), now)
            battery_sp = out.battery_setpoint_kw
            # Reactive priority: Q-V droop (voltage support) wins whenever it is
            # commanding; the tariff PFC only fills in the reactive when droop's Q
            # is idle (voltage inside the deadband, or droop disabled). This keeps
            # grid-voltage safety ahead of the economic PF target.
            reactive_sp = dq
            if self.pfc is not None and self.pfc.enabled and abs(dq) < 1e-9:
                # Sum whatever every other asset is already contributing at the
                # PCC (PV, flexible load, fixed load/meter) so the battery is
                # only sized for the residual -- see pfc.reactive_setpoint_kvar's
                # other_reactive_kvar. Missing/COMM_FAIL aggregates default to 0
                # via _val(), so this degrades gracefully on sites without one
                # of these classes wired.
                other_reactive_kvar = (
                    _val(snap.aggregates.get("pv", {}).get("reactive_power_kvar"))
                    + _val(snap.aggregates.get("flexible_load", {}).get("reactive_power_kvar"))
                    + _val(snap.aggregates.get("meter", {}).get("reactive_power_kvar"))
                )
                reactive_sp = self.pfc.reactive_setpoint_kvar(
                    now, inputs.pcc_meas_kw, battery_sp, other_reactive_kvar
                )
            derate, curtail = out.derate_factor, out.curtail_factor
            pcc_error, pi_output, dur = out.pcc_error_kw, out.pi_output_kw, out.loop_duration_ms

        elif decision.mode == Mode.HOLD:
            st = self.edge.state  # freeze on the last good command
            battery_sp, reactive_sp = st.last_battery_setpoint_kw, self._last_reactive
            derate, curtail = st.derate_factor, st.curtail_factor
            pcc_error = inputs.pcc_setpoint_kw - inputs.pcc_meas_kw
            pi_output, dur = 0.0, 0.0
            # No compute() this cycle -> the derivative's last measurement goes
            # stale; unprime so RUN re-entry seeds it afresh (no derivative kick).
            self.edge.state = replace(st, deriv_primed=False)

        else:  # SAFE: ramp battery to 0, release all mitigations, reset integrator
            battery_sp = safe_battery_setpoint(
                self.edge.state.last_battery_setpoint_kw,
                self.edge.params.slew_limit_kw_s,
                self.edge.params.update_period,
            )
            reactive_sp, derate, curtail = 0.0, 1.0, 1.0
            self.edge.state = replace(
                self.edge.state,
                integral=0.0,
                last_battery_setpoint_kw=battery_sp,
                derate_factor=1.0,
                curtail_factor=1.0,
                deriv_filtered_kw=0.0,
                deriv_primed=False,
            )
            pcc_error = inputs.pcc_setpoint_kw - inputs.pcc_meas_kw
            pi_output, dur = 0.0, 0.0

        published: list[str] = []
        # Battery setpoint goes out every cycle (keeps core's silence watchdog fed).
        self.publish("battery", {BATTERY_P: battery_sp, BATTERY_Q: reactive_sp}, now)
        published.append("battery")
        self._last_reactive = reactive_sp
        # Derate/curtail published only on change (+-deadband handled upstream).
        if self._publish_if_changed("pv", curtail, now):
            published.append("pv")
        if self._publish_if_changed("flexible_load", derate, now):
            published.append("flexible_load")

        result = CycleResult(
            mode=decision.mode.value,
            mode_changed=decision.changed,
            mode_reason=decision.reason,
            battery_setpoint_kw=battery_sp,
            reactive_setpoint_kvar=reactive_sp,
            derate_factor=derate,
            curtail_factor=curtail,
            pcc_error_kw=pcc_error,
            pi_output_kw=pi_output,
            loop_duration_ms=dur,
            data_fresh=data_fresh,
            battery_ok=battery_ok,
            published=tuple(published),
        )
        if self.write_control is not None:
            self.write_control(control_measurement_fields(result), now)
        return result

    def _publish_if_changed(self, asset_class: str, factor: float, now: float) -> bool:
        last = self._last_pub.get(asset_class)
        if last is not None and last == factor:
            return False
        self.publish(asset_class, {DERATE: factor}, now)
        self._last_pub[asset_class] = factor
        return True


# --------------------------------------------------------------- config builders


def battery_limits_from_config(ac: AssetConfigFile) -> BatteryLimits:
    """Aggregate battery limits: power limits summed across active batteries; SoC
    thresholds from the first (v1 single-battery-aggregate assumption)."""
    batts = [a for a in ac.assets if a.asset_class == "battery" and a.state == "active"]
    if not batts:
        raise ValueError("no active battery in asset config")
    lim = batts[0].flexibility.limits
    return BatteryLimits(
        max_charge_kw=sum(b.flexibility.limits.get("max_charge_kw", 0.0) for b in batts),
        max_discharge_kw=sum(b.flexibility.limits.get("max_discharge_kw", 0.0) for b in batts),
        min_soc_pct=lim.get("min_soc_pct", 0.0),
        max_soc_pct=lim.get("max_soc_pct", 100.0),
        min_warning_soc_pct=lim.get("min_warning_soc_pct", 0.0),
        max_warning_soc_pct=lim.get("max_warning_soc_pct", 0.0),
    )


def derate_limits_from_config(ac: AssetConfigFile) -> DerateLimits:
    pv = next((a for a in ac.assets if a.asset_class == "pv" and a.state == "active"), None)
    load = next(
        (a for a in ac.assets if a.asset_class == "flexible_load" and a.state == "active"), None
    )
    return DerateLimits(
        load_min_derate=load.flexibility.limits.get("min_derate_factor", 0.0) if load else 0.0,
        pv_min_derate=pv.flexibility.limits.get("min_derate_factor", 0.0) if pv else 0.0,
    )


def pcc_params_from_config(ac: AssetConfigFile) -> tuple[float, float | None]:
    """Returns (pcc_base_kw, max_feed_kw). Base = PCC max supply power (data
    model: per-unit base = PCC max power)."""
    pcc = next((a for a in ac.assets if a.asset_class == "pcc" and a.state == "active"), None)
    if pcc is None:
        raise ValueError("no active pcc in asset config")
    base = pcc.limits.get("max_supply_power_kva")
    if not base or base <= 0:
        raise ValueError("pcc max_supply_power_kva required as the per-unit base")
    return float(base), pcc.limits.get("max_feed_power_kva")


def params_from_config(ec: EdgeEmsConfigFile) -> ControlParams:
    c = ec.controller
    return ControlParams(
        kp=c.Kp,
        ki=c.Ki,
        update_period=c.update_period,
        slew_limit_kw_s=c.slew_limit_kw_s,
        kd=c.Kd,
        deriv_filter_tau=c.deriv_filter_tau,
        pv_feedforward_gain=c.pv_feedforward_gain,
    )
