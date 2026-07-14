"""Controller-performance additions: PV feedforward + optional derivative (PID).

These exercise the new terms in edge_controller.compute in isolation (sign,
magnitude, priming) and confirm the defaults leave the pure-PI behaviour
untouched. The closed-loop settling improvement itself is a rig/HIL measurement;
here we lock the maths and the backward compatibility.
"""

import time

import pytest
from common.points import GOOD, STALE, PointValue
from controller.control_loop import ControlLoop, Snapshot
from controller.edge_controller import (
    BatteryLimits,
    ControlInputs,
    ControlParams,
    ControlState,
    DerateLimits,
    EdgeController,
    compute,
)
from controller.modes import ModeController

_BATT = BatteryLimits(1000, 1000, 5, 95)
_DERATE = DerateLimits()


def _inputs(pcc, pv=0.0, setpoint=0.0):
    return ControlInputs(
        pcc_meas_kw=pcc,
        pcc_setpoint_kw=setpoint,
        soc_pct=50.0,
        avail_charge_kw=1000.0,
        avail_discharge_kw=1000.0,
        pv_active_power_kw=pv,
    )


# --------------------------------------------------------------- PV feedforward


def test_feedforward_offsets_pv_before_error_appears():
    # PCC already balanced (error = 0) but PV is generating 100 kW. With FF the
    # battery pre-charges +100 to absorb it; without FF it does nothing.
    params_ff = ControlParams(kp=0.5, ki=0.1, update_period=1.0, pv_feedforward_gain=1.0)
    params_no = ControlParams(kp=0.5, ki=0.1, update_period=1.0, pv_feedforward_gain=0.0)
    ff = compute(_inputs(pcc=0.0, pv=-100.0), params_ff, _BATT, _DERATE, ControlState())
    no = compute(_inputs(pcc=0.0, pv=-100.0), params_no, _BATT, _DERATE, ControlState())
    assert ff.battery_setpoint_kw == pytest.approx(100.0)
    assert no.battery_setpoint_kw == pytest.approx(0.0)


def test_feedforward_gain_scales_linearly():
    p = ControlParams(kp=0.0, ki=0.0, update_period=1.0, pv_feedforward_gain=0.5)
    out = compute(_inputs(pcc=0.0, pv=-200.0), p, _BATT, _DERATE, ControlState())
    assert out.battery_setpoint_kw == pytest.approx(0.5 * 200.0)  # -gain*pv


# --------------------------------------------------------------- derivative term


def test_derivative_suppressed_on_first_cycle():
    # deriv_primed defaults False -> no derivative kick on a cold start even with
    # a non-zero measurement jump; output matches pure PI.
    p_d = ControlParams(kp=0.5, ki=0.1, update_period=1.0, kd=2.0)
    p_0 = ControlParams(kp=0.5, ki=0.1, update_period=1.0, kd=0.0)
    d = compute(_inputs(pcc=100.0), p_d, _BATT, _DERATE, ControlState())
    z = compute(_inputs(pcc=100.0), p_0, _BATT, _DERATE, ControlState())
    assert d.pi_output_kw == pytest.approx(z.pi_output_kw)
    assert d.state.deriv_primed is True  # now primed for next cycle


def test_derivative_applies_on_measurement_change():
    kd, dt = 2.0, 1.0
    primed = ControlState(last_pcc_meas_kw=0.0, deriv_primed=True)
    p_d = ControlParams(kp=0.5, ki=0.1, update_period=dt, kd=kd)
    p_0 = ControlParams(kp=0.5, ki=0.1, update_period=dt, kd=0.0)
    d = compute(_inputs(pcc=50.0), p_d, _BATT, _DERATE, primed)
    z = compute(_inputs(pcc=50.0), p_0, _BATT, _DERATE, primed)
    # derivative-on-measurement term = -kd * (dmeas)/dt = -2 * 50 = -100
    assert (d.pi_output_kw - z.pi_output_kw) == pytest.approx(-kd * 50.0 / dt)


def test_derivative_filter_attenuates():
    kd, dt, tau = 2.0, 1.0, 3.0
    primed = ControlState(last_pcc_meas_kw=0.0, deriv_primed=True)
    raw = ControlParams(kp=0.0, ki=0.0, update_period=dt, kd=kd)  # unfiltered
    filt = ControlParams(kp=0.0, ki=0.0, update_period=dt, kd=kd, deriv_filter_tau=tau)
    r = compute(_inputs(pcc=50.0), raw, _BATT, _DERATE, primed)
    f = compute(_inputs(pcc=50.0), filt, _BATT, _DERATE, primed)
    assert abs(f.pi_output_kw) < abs(r.pi_output_kw)  # low-pass shrinks first step
    alpha = dt / (tau + dt)
    assert f.pi_output_kw == pytest.approx(alpha * r.pi_output_kw)


# --------------------------------------------------------------- backward compat


def test_defaults_are_pure_pi():
    # No kd/ff configured -> output is exactly Kp*e + Ki*integral.
    p = ControlParams(kp=0.4, ki=0.2, update_period=1.0)
    out = compute(_inputs(pcc=80.0), p, _BATT, _DERATE, ControlState())
    expected = 0.4 * (-80.0) + 0.2 * (-80.0 * 1.0)
    assert out.pi_output_kw == pytest.approx(expected)


# --------------------------------------------------------------- priming reset in loop


def _pv(value, quality=GOOD):
    return PointValue(value, time.time(), quality)


def _snapshot(quality=GOOD):
    return Snapshot(
        aggregates={
            "battery": {
                "soc_pct": _pv(50.0, quality),
                "active_power_kw": _pv(0.0, quality),
                "available_charge_power_kw": _pv(1000.0, quality),
                "available_discharge_power_kw": _pv(1000.0, quality),
            },
            "pv": {"active_power_kw": _pv(0.0, quality)},
        },
        pcc={
            "active_power_kw": _pv(100.0, quality),
            "frequency_hz": _pv(50.0, quality),
            "voltage_v": _pv(230.0, quality),
        },
    )


def _loop(snapshot):
    params = ControlParams(kp=0.5, ki=0.1, update_period=1.0, kd=2.0)
    return ControlLoop(
        edge=EdgeController(params, _BATT, _DERATE),
        modes=ModeController(hold_max_s=10.0),
        droop=None,
        read_snapshot=lambda now: snapshot,
        publish=lambda *a: True,
        pcc_base_kw=2000.0,
        max_feed_kw=999.0,
    )


def test_hold_unprimes_derivative():
    loop = _loop(_snapshot(GOOD))
    loop.run_once(time.time())  # RUN -> primes
    assert loop.edge.state.deriv_primed is True
    loop.read_snapshot = lambda now: _snapshot(STALE)  # stale -> HOLD
    loop.run_once(time.time())
    assert loop.edge.state.deriv_primed is False  # re-primes on RUN re-entry
