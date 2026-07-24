"""Control loop core (plan tasks 19-21).

The heart of the controller: a single pure step that takes a snapshot of the
aggregate inputs plus the controller state and returns the battery setpoint,
derate/curtail factors, and loop telemetry. No I/O lives here -- reads, MQTT
publishes, mode handling (RUN/HOLD/SAFE, task 23) and droop (task 22) wrap this
core. Keeping it pure is what makes the saturation/anti-windup behaviour
unit-testable without a database or broker.

Signs are normative from data_model.yaml and must not be reinterpreted here:

  pcc.active_power_kw        + import   / - export
  battery.active_power_kw    + charge   / - discharge   (so is its setpoint)
  pv.active_power_kw         <= 0 (generation)
  derate_factor_setpoint     in [0, 1], 1 = no derate / no curtailment

Self-consumption tracking: the PI drives the PCC active power to its setpoint
(typically 0 kW) by commanding the battery. Because raising battery charge
raises PCC import (dP_pcc/dP_batt = +1), the same sign works directly: with
e = setpoint - measured, a battery setpoint of Kp*e + Ki*integral pushes the
PCC the right way (e<0, too much import -> negative setpoint -> discharge).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

# Factors that move by less than this are not re-published (anti-chatter,
# system design 4: "published only on change, +-0.01 deadband").
DERATE_DEADBAND = 0.01
CURTAIL_GAIN = 0.5  # integrating feed-limit curtailment loop gain (0 < g <= 1)
# Tolerance for "is the raw PI output past the clamp limit" comparisons (kW).
_SAT_EPS = 1e-9
# Fallback "ceiling" for the t7 curtailment ramp when no site feed limit is
# configured. Large enough to be well above any realistic export so the ramp
# still starts from "no extra constraint" at the warning-band entry and still
# reaches exactly 0 at the hard SoC ceiling.
NO_FEED_LIMIT_KW = 1.0e6


@dataclass(frozen=True)
class ControlParams:
    """Tunables from edge_ems_config controller block."""

    kp: float
    ki: float
    update_period: float  # s; the integral uses this as dt
    slew_limit_kw_s: float | None = None  # battery setpoint slew, kW/s; None disables
    kd: float = 0.0  # derivative gain (on measurement); 0 = pure PI (default, unchanged)
    deriv_filter_tau: float = 0.0  # s; first-order filter on the derivative; 0 = unfiltered
    pv_feedforward_gain: float = 0.0  # [0,1] fraction of PV offset fed forward; 0 = off


@dataclass(frozen=True)
class BatteryLimits:
    """From the battery asset's flexibility.limits. Charge/discharge are
    magnitudes (>= 0); the signed setpoint window is [-max_discharge, +max_charge].
    A warning threshold of 0 disables that side's mitigation (per data model)."""

    max_charge_kw: float
    max_discharge_kw: float
    min_soc_pct: float
    max_soc_pct: float
    min_warning_soc_pct: float = 0.0  # low-SoC derate trigger; 0 disables
    max_warning_soc_pct: float = 0.0  # high-SoC curtail trigger; 0 disables


@dataclass(frozen=True)
class DerateLimits:
    """Floors for the mitigation factors, from pv/flexible_load flexibility.limits."""

    load_min_derate: float = 0.0  # flexible_load floor
    pv_min_derate: float = 0.0  # pv curtailment floor


@dataclass(frozen=True)
class ControlInputs:
    """One cycle's aggregate snapshot (already validated GOOD by the caller)."""

    pcc_meas_kw: float  # measured PCC active power (+import / -export)
    pcc_setpoint_kw: float  # target tracked by the PI (self-consumption -> 0)
    soc_pct: float  # battery aggregate state of charge
    avail_charge_kw: float  # dynamic BMS charge headroom, magnitude >= 0
    avail_discharge_kw: float  # dynamic BMS discharge headroom, magnitude >= 0
    pv_active_power_kw: float = 0.0  # aggregate PV power (<= 0); for curtailment maths
    max_feed_kw: float | None = None  # PCC export limit magnitude; None disables curtail


@dataclass(frozen=True)
class ControlState:
    """Carried between cycles. The integral stores raw done e*dt (Ki applied at use)."""

    integral: float = 0.0
    last_battery_setpoint_kw: float = 0.0
    derate_factor: float = 1.0
    curtail_factor: float = 1.0
    last_pcc_meas_kw: float = 0.0  # for derivative-on-measurement (kept across SAFE)
    deriv_filtered_kw: float = 0.0  # filtered derivative term carried between cycles
    deriv_primed: bool = False  # False until the first measurement seeds the derivative


@dataclass(frozen=True)
class ControlOutput:
    """Result of one cycle. Mirrors the `control` InfluxDB measurement fields."""

    battery_setpoint_kw: float
    derate_factor: float
    curtail_factor: float
    pcc_error_kw: float
    pi_output_kw: float  # PI result before clamp/slew (the raw demand)
    battery_saturated: bool
    integrator_frozen: bool
    loop_duration_ms: float
    state: ControlState  # the new state to feed into the next step


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class EdgeController:
    """Stateful wrapper around the pure `compute`. Holds tunables/limits and the
    rolling control state; `step` times the computation and advances the state."""

    def __init__(
        self,
        params: ControlParams,
        battery: BatteryLimits,
        derate: DerateLimits | None = None,
        state: ControlState | None = None,
    ):
        self.params = params
        self.battery = battery
        self.derate = derate or DerateLimits()
        self.state = state or ControlState()

    def step(self, inputs: ControlInputs, now: float | None = None) -> ControlOutput:
        t0 = time.perf_counter()
        out = compute(inputs, self.params, self.battery, self.derate, self.state)
        dur_ms = (time.perf_counter() - t0) * 1000.0
        out = replace(out, loop_duration_ms=dur_ms)
        self.state = out.state
        return out

    def reset(self, state: ControlState | None = None) -> None:
        self.state = state or ControlState()


def compute(
    inputs: ControlInputs,
    params: ControlParams,
    battery: BatteryLimits,
    derate: DerateLimits,
    state: ControlState,
) -> ControlOutput:
    """Pure single-cycle control law. Order follows system design 4 (t4-t7):
    PI with conditional-integration anti-windup, battery clamp incl. SoC gates,
    slew limiting, then the mitigation ladder (low-SoC derate, high-SoC curtail).
    """
    dt = params.update_period

    # t4 -- error and PI (tentative integration, revisited by anti-windup)
    error = inputs.pcc_setpoint_kw - inputs.pcc_meas_kw
    cand_integral = state.integral + error * dt

    # Derivative-on-measurement (not error) so a setpoint step causes no kick.
    # For a constant setpoint de/dt = -d(meas)/dt, so the battery derivative term
    # is kd*de/dt = -kd*d(meas)/dt, optionally low-pass filtered. The term is only
    # active once "primed" by a first measurement, so a cold start or a SAFE
    # re-entry never injects a one-cycle spike. kd = 0 (default) -> pure PI.
    deriv_filtered = state.deriv_filtered_kw
    deriv = 0.0
    if params.kd > 0.0 and dt > 0.0 and state.deriv_primed:
        raw_deriv = -params.kd * (inputs.pcc_meas_kw - state.last_pcc_meas_kw) / dt
        alpha = dt / (params.deriv_filter_tau + dt) if params.deriv_filter_tau > 0 else 1.0
        deriv_filtered = alpha * raw_deriv + (1.0 - alpha) * state.deriv_filtered_kw
        deriv = deriv_filtered

    # Feedforward: cancel PV's direct effect on the PCC so the loop does not have
    # to chase generation ramps through the integrator. pv is <= 0 (generation);
    # holding the PCC needs the battery to charge by -pv, scaled by the gain.
    feedforward = -params.pv_feedforward_gain * inputs.pv_active_power_kw

    pi_output = params.kp * error + params.ki * cand_integral + deriv + feedforward

    # t5 -- signed battery setpoint window: dynamic BMS headroom AND config limits,
    # then SoC hard gates (<=min: no discharge; >=max: no charge).
    upper = min(battery.max_charge_kw, inputs.avail_charge_kw)  # charge is positive
    lower = -min(battery.max_discharge_kw, inputs.avail_discharge_kw)  # discharge negative
    if inputs.soc_pct <= battery.min_soc_pct:
        lower = max(lower, 0.0)  # forbid discharge
    if inputs.soc_pct >= battery.max_soc_pct:
        upper = min(upper, 0.0)  # forbid charge
    if lower > upper:  # degenerate (both gates / zero headroom): hold at zero
        lower = upper = 0.0

    # conditional-integration anti-windup: only freeze the integrator when the
    # battery is saturated AND the error pushes further into the violated limit.
    integrator_frozen = False
    if pi_output > upper + _SAT_EPS and error > 0:
        integrator_frozen = True
    elif pi_output < lower - _SAT_EPS and error < 0:
        integrator_frozen = True

    if integrator_frozen:
        integral = state.integral  # do not accumulate this cycle
        pi_output = params.kp * error + params.ki * integral + deriv + feedforward
    else:
        integral = cand_integral

    clamped = _clamp(pi_output, lower, upper)
    battery_saturated = abs(clamped - pi_output) > _SAT_EPS

    # t5 (cont.) -- slew limiter protects the battery from step abuse
    if params.slew_limit_kw_s is not None:
        max_step = params.slew_limit_kw_s * dt
        prev = state.last_battery_setpoint_kw
        battery_setpoint = _clamp(clamped, prev - max_step, prev + max_step)
    else:
        battery_setpoint = clamped

    # t6 -- low-SoC derate of flexible load. Warning band is the trigger; ramp the
    # factor from 1 (at the warning SoC) down to the floor (at min SoC).
    derate_factor = 1.0
    if battery.min_warning_soc_pct > 0 and inputs.soc_pct < battery.min_warning_soc_pct:
        span = battery.min_warning_soc_pct - battery.min_soc_pct
        ramp = (inputs.soc_pct - battery.min_soc_pct) / span if span > 0 else 0.0
        derate_factor = _clamp(ramp, derate.load_min_derate, 1.0)
    derate_factor = _deadband(derate_factor, state.derate_factor)

    # t7 -- PV curtailment: an INTEGRATING regulator (curtail_factor is the
    # integrator state, carried in ControlState, clamped to [pv_min_derate, 1.0]
    # for anti-windup). Each cycle it is nudged by the export overshoot vs a
    # *target*, normalised by the *available* PV -- estimated as pv_meas /
    # curtail_prev, since the plant applies Pcurtailment as a ceiling on
    # available capacity (controller only sees curtailed pv_meas). This avoids
    # the steady-state offset a one-shot proportional cut would leave; at
    # equilibrium feed_error = 0 -> export sits exactly on the target. Releasing
    # happens gradually (the integrator climbs back toward 1.0) so there is no
    # boundary hunting.
    #
    # The export TARGET ramps across the high-SoC warning band
    # (max_warning_soc_pct -> max_soc_pct): from the site's feed limit (i.e.
    # effectively no extra constraint under normal export levels) down to 0
    # (full self-consumption) exactly at the hard ceiling. Priority order is
    # PV -> BESS -> load-shed; grid export is the last resort. This -- not a
    # single on/off switch fired by avail_charge_kw hitting 0 -- is what avoids
    # a bang-bang relay: gating curtailment on a step function created a ~12s
    # limit cycle on the rig (charge to the ceiling -> full curtail -> SoC dips
    # -> full release -> repeat). Ramping the target lets PV ease off smoothly
    # as the battery approaches full, well before it actually saturates.
    #
    # Belt-and-braces: if the BMS ever reports zero charge headroom outright
    # (avail_charge_kw <= 0 -- the same signal that clamps `upper` above), the
    # target is forced to 0 regardless of where SoC nominally sits relative to
    # the configured band, in case headroom is lost for a reason other than
    # the SoC thresholds tracked here.
    span = battery.max_soc_pct - battery.max_warning_soc_pct
    in_band = battery.max_warning_soc_pct > 0 and inputs.soc_pct > battery.max_warning_soc_pct
    no_headroom = inputs.avail_charge_kw <= 0.0
    if in_band or no_headroom:
        if span > 0:
            frac = _clamp((inputs.soc_pct - battery.max_warning_soc_pct) / span, 0.0, 1.0)
        else:
            frac = 1.0
        ceiling_kw = inputs.max_feed_kw if inputs.max_feed_kw is not None else NO_FEED_LIMIT_KW
        target_kw = 0.0 if no_headroom else ceiling_kw * (1.0 - frac)
        export_kw = -inputs.pcc_meas_kw                  # +ve when exporting
        feed_error = export_kw - target_kw               # +ve => over target
        pv_gen = abs(inputs.pv_active_power_kw)
        prev = state.curtail_factor if state.curtail_factor > 0.0 else 1.0
        if pv_gen > 0.0:
            # available ~ pv_gen / prev; step the factor by the normalised overshoot
            step = CURTAIL_GAIN * feed_error * prev / pv_gen
            curtail_factor = _clamp(prev - step, derate.pv_min_derate, 1.0)
        else:
            curtail_factor = _clamp(prev, derate.pv_min_derate, 1.0)
    else:
        # below the warning band and headroom available: no curtailment
        # needed, the battery absorbs the surplus instead.
        curtail_factor = 1.0

    new_state = ControlState(
        integral=integral,
        last_battery_setpoint_kw=battery_setpoint,
        derate_factor=derate_factor,
        curtail_factor=curtail_factor,
        last_pcc_meas_kw=inputs.pcc_meas_kw,
        deriv_filtered_kw=deriv_filtered,
        deriv_primed=True,
    )
    return ControlOutput(
        battery_setpoint_kw=battery_setpoint,
        derate_factor=derate_factor,
        curtail_factor=curtail_factor,
        pcc_error_kw=error,
        pi_output_kw=pi_output,
        battery_saturated=battery_saturated,
        integrator_frozen=integrator_frozen,
        loop_duration_ms=0.0,
        state=new_state,
    )


def _deadband(new: float, last: float) -> float:
    """Hold the previous factor unless the new one moved beyond the deadband."""
    return last if abs(new - last) < DERATE_DEADBAND else new
