"""Control loop core tests (plan tasks 19-21).

Covers PI tracking, saturation entry/exit, conditional-integration anti-windup
(regression), slew limiting, SoC gates, low-SoC derate ramp, high-SoC PV
curtailment, the +-0.01 publish deadband, and a 1000-cycle no-drift run.
"""

import pytest
from controller.edge_controller import (
    BatteryLimits,
    ControlInputs,
    ControlParams,
    ControlState,
    DerateLimits,
    EdgeController,
    compute,
)


def _params(kp=0.5, ki=0.1, period=1.0, slew=None):
    return ControlParams(kp=kp, ki=ki, update_period=period, slew_limit_kw_s=slew)


def _batt(
    charge=1000.0,
    discharge=1000.0,
    min_soc=5.0,
    max_soc=95.0,
    min_warn=0.0,
    max_warn=0.0,
):
    return BatteryLimits(
        max_charge_kw=charge,
        max_discharge_kw=discharge,
        min_soc_pct=min_soc,
        max_soc_pct=max_soc,
        min_warning_soc_pct=min_warn,
        max_warning_soc_pct=max_warn,
    )


def _inputs(pcc, setpoint=0.0, soc=50.0, charge_hl=1000.0, discharge_hl=1000.0, **kw):
    return ControlInputs(
        pcc_meas_kw=pcc,
        pcc_setpoint_kw=setpoint,
        soc_pct=soc,
        avail_charge_kw=charge_hl,
        avail_discharge_kw=discharge_hl,
        **kw,
    )


def _run(out_state_seed=None, **kw):
    """One compute() call with sensible defaults; returns ControlOutput."""
    return compute(
        kw["inputs"],
        kw.get("params", _params()),
        kw.get("battery", _batt()),
        kw.get("derate", DerateLimits()),
        kw.get("state", out_state_seed or ControlState()),
    )


# --------------------------------------------------------------- PI behaviour


def test_zero_error_zero_output_from_rest():
    out = _run(inputs=_inputs(pcc=0.0))
    assert out.pcc_error_kw == 0.0
    assert out.battery_setpoint_kw == pytest.approx(0.0)
    assert not out.battery_saturated


def test_import_drives_battery_to_discharge():
    # measured import 100 kW above setpoint 0 -> e = -100 -> negative (discharge) setpoint
    out = _run(inputs=_inputs(pcc=100.0))
    assert out.pcc_error_kw == pytest.approx(-100.0)
    assert out.battery_setpoint_kw < 0  # discharging to cover the load
    # Kp*e + Ki*(e*dt) = 0.5*-100 + 0.1*-100 = -60
    assert out.battery_setpoint_kw == pytest.approx(-60.0)


def test_export_drives_battery_to_charge():
    out = _run(inputs=_inputs(pcc=-100.0))
    assert out.pcc_error_kw == pytest.approx(100.0)
    assert out.battery_setpoint_kw > 0  # charging to absorb surplus
    assert out.battery_setpoint_kw == pytest.approx(60.0)


def test_integrator_accumulates_toward_steady_state():
    ctrl = EdgeController(_params(kp=0.0, ki=0.1), _batt())  # pure integral
    last = 0.0
    for _ in range(5):
        out = ctrl.step(_inputs(pcc=50.0))
        assert out.battery_setpoint_kw < last  # marching further negative each cycle
        last = out.battery_setpoint_kw
    # 5 cycles of e=-50, dt=1, Ki=0.1 -> 0.1 * (-50*5) = -25
    assert last == pytest.approx(-25.0)


def test_setpoint_tracks_to_zero_error_closed_loop():
    # Simulated plant: PCC = base_load + battery power (dP_pcc/dP_batt = +1).
    ctrl = EdgeController(_params(kp=0.3, ki=0.2), _batt())
    base_load = 200.0
    batt = 0.0
    pcc = base_load + batt
    for _ in range(200):
        out = ctrl.step(_inputs(pcc=pcc))
        batt = out.battery_setpoint_kw
        pcc = base_load + batt
    assert pcc == pytest.approx(0.0, abs=0.5)
    assert batt == pytest.approx(-200.0, abs=0.5)


# --------------------------------------------------------------- saturation


def test_output_clamped_to_charge_limit():
    out = _run(inputs=_inputs(pcc=-100000.0), battery=_batt(charge=500.0))
    assert out.battery_setpoint_kw == pytest.approx(500.0)
    assert out.battery_saturated


def test_output_clamped_to_discharge_limit():
    out = _run(inputs=_inputs(pcc=100000.0), battery=_batt(discharge=400.0))
    assert out.battery_setpoint_kw == pytest.approx(-400.0)
    assert out.battery_saturated


def test_dynamic_bms_headroom_overrides_config_limit():
    # config allows 1000 charge but BMS only offers 150 right now
    out = _run(inputs=_inputs(pcc=-100000.0, charge_hl=150.0), battery=_batt(charge=1000.0))
    assert out.battery_setpoint_kw == pytest.approx(150.0)
    assert out.battery_saturated


def test_not_saturated_when_within_limits():
    out = _run(inputs=_inputs(pcc=10.0), battery=_batt())
    assert not out.battery_saturated


# ------------------------------------------------- anti-windup (regression)


def test_integrator_freezes_while_saturated_into_limit():
    # Persistent huge import keeps the battery pinned at the discharge limit.
    ctrl = EdgeController(_params(kp=0.5, ki=0.1), _batt(discharge=300.0))
    for _ in range(50):
        out = ctrl.step(_inputs(pcc=100000.0))
        assert out.integrator_frozen
        assert out.battery_setpoint_kw == pytest.approx(-300.0)
    # Integrator must NOT have wound up far past the limit.
    assert abs(ctrl.state.integral) < 1e6


def test_no_windup_means_immediate_recovery_on_reversal():
    # Without anti-windup the integrator would bury itself during saturation and
    # the output would stay pinned for many cycles after the load clears.
    ctrl = EdgeController(_params(kp=0.5, ki=0.1), _batt(discharge=300.0))
    for _ in range(100):
        ctrl.step(_inputs(pcc=100000.0))  # deep, sustained saturation
    # Load clears: error reverses. A wound-up integrator would keep discharging.
    out = ctrl.step(_inputs(pcc=0.0))
    assert out.battery_setpoint_kw > -300.0  # released, not stuck at the limit
    assert not out.integrator_frozen


def test_integrator_runs_when_saturated_but_error_reverses():
    # Saturated at discharge limit, but error now positive (wants to charge):
    # integrator should be allowed to move (not frozen) so it can recover.
    state = ControlState(integral=-5000.0, last_battery_setpoint_kw=-300.0)
    out = compute(
        _inputs(pcc=-50.0),  # e = +50, pushing away from the discharge limit
        _params(),
        _batt(discharge=300.0),
        DerateLimits(),
        state,
    )
    assert not out.integrator_frozen


# --------------------------------------------------------------- slew limiter


def test_slew_limits_step_up():
    state = ControlState(last_battery_setpoint_kw=0.0)
    out = compute(
        _inputs(pcc=-100000.0),
        _params(slew=50.0, period=1.0),  # max 50 kW/cycle
        _batt(),
        DerateLimits(),
        state,
    )
    assert out.battery_setpoint_kw == pytest.approx(50.0)


def test_slew_respects_period():
    state = ControlState(last_battery_setpoint_kw=0.0)
    out = compute(
        _inputs(pcc=-100000.0),
        _params(slew=50.0, period=0.5),  # 50 kW/s * 0.5 s = 25 kW/cycle
        _batt(),
        DerateLimits(),
        state,
    )
    assert out.battery_setpoint_kw == pytest.approx(25.0)


def test_slew_ramps_over_multiple_cycles():
    ctrl = EdgeController(_params(slew=50.0), _batt())
    setpoints = [ctrl.step(_inputs(pcc=-100000.0)).battery_setpoint_kw for _ in range(4)]
    assert setpoints == pytest.approx([50.0, 100.0, 150.0, 200.0])


def test_no_slew_when_disabled():
    out = _run(inputs=_inputs(pcc=-100000.0), params=_params(slew=None), battery=_batt(charge=500))
    assert out.battery_setpoint_kw == pytest.approx(500.0)  # jumps straight to limit


# --------------------------------------------------------------- SoC gates


def test_min_soc_forbids_discharge():
    out = _run(inputs=_inputs(pcc=100000.0, soc=4.0), battery=_batt(min_soc=5.0))
    assert out.battery_setpoint_kw == pytest.approx(0.0)  # cannot discharge below min


def test_max_soc_forbids_charge():
    out = _run(inputs=_inputs(pcc=-100000.0, soc=96.0), battery=_batt(max_soc=95.0))
    assert out.battery_setpoint_kw == pytest.approx(0.0)  # cannot charge above max


def test_charge_allowed_at_low_soc():
    out = _run(inputs=_inputs(pcc=-100.0, soc=4.0), battery=_batt(min_soc=5.0))
    assert out.battery_setpoint_kw > 0  # low SoC blocks discharge, not charge


# ------------------------------------------------- low-SoC derate (ladder t6)


@pytest.mark.parametrize(
    "soc,expected",
    [
        (25.0, 1.0),  # above warning band -> no derate
        (20.0, 1.0),  # at warning edge -> full
        (12.5, 0.5),  # halfway between min(5) and warn(20) -> 0.5
        (5.0, 0.2),  # at min -> floored at load_min_derate
        (3.0, 0.2),  # below min -> still floored
    ],
)
def test_low_soc_derate_ramp(soc, expected):
    out = compute(
        _inputs(pcc=100.0, soc=soc),
        _params(),
        _batt(min_soc=5.0, min_warn=20.0),
        DerateLimits(load_min_derate=0.2),
        ControlState(derate_factor=expected),  # seed past deadband so value passes through
    )
    assert out.derate_factor == pytest.approx(expected)


def test_derate_disabled_when_warning_zero():
    out = _run(inputs=_inputs(pcc=100.0, soc=3.0), battery=_batt(min_soc=5.0, min_warn=0.0))
    assert out.derate_factor == 1.0


# --------------------------------------------- high-SoC PV curtail (ladder t7)


def test_curtail_ramped_target_past_feed_limit_and_full():
    # Ramped target (fixes the ~12s bang-bang oscillation found on the rig):
    # at soc=92 the warning band (90->95) is 40% traversed, so the target is
    # tapered from the 500 kW feed limit down to 500*(1-0.4) = 300 kW, not the
    # raw feed limit itself. Exporting 600 kW is a 300 kW overshoot vs that
    # target. From curtail 0.75 it steps DOWN by
    # g*feed_error*prev/pv_gen = 0.5*300*0.75/400 = 0.28125 -> 0.46875.
    out = compute(
        _inputs(pcc=-600.0, soc=92.0, pv_active_power_kw=-400.0, max_feed_kw=500.0),
        _params(),
        _batt(max_soc=95.0, max_warn=90.0),
        DerateLimits(),
        ControlState(curtail_factor=0.75),
    )
    assert out.curtail_factor == pytest.approx(0.46875)


def test_curtail_engages_before_raw_feed_limit_partway_through_band():
    # Same 400 kW export that used to be "below the feed limit" (500) and get
    # no curtailment now DOES curtail a little: at soc=92 the ramped target is
    # only 300 kW (see above), so 400 kW export is a 100 kW overshoot vs that
    # tighter target, not the raw 500 kW cap. This is the ramp working as
    # intended -- proactive, partial curtailment before the hard ceiling,
    # instead of a single on/off snap right at it.
    out = _run(
        inputs=_inputs(pcc=-400.0, soc=92.0, pv_active_power_kw=-400.0, max_feed_kw=500.0),
        battery=_batt(max_soc=95.0, max_warn=90.0),
    )
    assert out.curtail_factor == pytest.approx(0.875)


def test_no_curtail_at_or_below_warning_threshold():
    # Right at the warning-band entry (soc == max_warning_soc_pct), the ramp
    # hasn't started yet -- strict ">" (mirrors t6's strict "<" for the
    # low-SoC band), so no curtailment.
    out = _run(
        inputs=_inputs(pcc=-400.0, soc=90.0, pv_active_power_kw=-400.0, max_feed_kw=500.0),
        battery=_batt(max_soc=95.0, max_warn=90.0),
    )
    assert out.curtail_factor == 1.0


def test_curtail_ramps_monotonically_through_warning_band():
    # The whole point of the ramp: at a FIXED export level, curtailment
    # tightens progressively as SoC climbs from the warning threshold to the
    # hard ceiling, instead of staying off then snapping hard at one point.
    # Single-cycle response at each soc (fresh ControlState) to isolate the
    # ramp's shape from integrator dynamics.
    factors = []
    for soc in (90.0, 91.0, 92.0, 93.0, 94.0, 95.0):
        out = compute(
            _inputs(pcc=-490.0, soc=soc, pv_active_power_kw=-490.0, max_feed_kw=500.0),
            _params(),
            _batt(max_soc=95.0, max_warn=90.0),
            DerateLimits(),
            ControlState(),
        )
        factors.append(out.curtail_factor)
    assert factors == sorted(factors, reverse=True)  # monotonically decreasing
    assert factors[0] == 1.0  # no curtailment right at the threshold
    assert factors[-1] == pytest.approx(0.5)  # fully ramped down at the ceiling
    assert all(f < 1.0 for f in factors[1:])  # every step past it curtails some


def test_no_curtail_when_battery_not_full():
    out = _run(
        inputs=_inputs(pcc=-600.0, soc=80.0, pv_active_power_kw=-400.0, max_feed_kw=500.0),
        battery=_batt(max_warn=90.0),
    )
    assert out.curtail_factor == 1.0


def test_curtail_floored_at_pv_min_derate():
    # Massive overshoot would drive curtail negative; floor holds it.
    out = compute(
        _inputs(pcc=-5000.0, soc=92.0, pv_active_power_kw=-400.0, max_feed_kw=100.0),
        _params(),
        _batt(max_warn=90.0),
        DerateLimits(pv_min_derate=0.1),
        ControlState(curtail_factor=0.1),
    )
    assert out.curtail_factor == pytest.approx(0.1)


def test_curtail_targets_zero_export_when_no_charge_headroom():
    # Regression: battery saturated full (avail_charge_kw <= 0) with no SoC
    # warning band configured at all (max_warn=0, disabling the feed-limit
    # branch) must still curtail PV toward 0 export -- self-consumption is
    # driven directly by real BMS headroom, not by a configured SoC band.
    out = compute(
        _inputs(pcc=-150.0, soc=95.0, charge_hl=0.0, pv_active_power_kw=-400.0),
        _params(),
        _batt(max_soc=95.0, max_warn=0.0),  # feed-limit branch disabled
        DerateLimits(),
        ControlState(curtail_factor=1.0),
    )
    assert out.curtail_factor < 1.0  # curtailing, not stuck exporting freely


def test_no_curtail_target_when_headroom_available_even_at_max_soc():
    # If the BMS still reports headroom (avail_charge_kw > 0), curtailment
    # stays off even at soc >= max_soc_pct -- the battery absorbs the surplus.
    out = compute(
        _inputs(pcc=-150.0, soc=95.0, charge_hl=50.0, pv_active_power_kw=-400.0),
        _params(),
        _batt(max_soc=95.0, max_warn=0.0),
        DerateLimits(),
        ControlState(curtail_factor=1.0),
    )
    assert out.curtail_factor == 1.0


# --------------------------------------------------------------- deadband


def test_derate_deadband_holds_small_changes():
    # New computed derate differs from last by < 0.01 -> hold last.
    out = compute(
        _inputs(pcc=100.0, soc=12.4),  # ramp ~0.4933
        _params(),
        _batt(min_soc=5.0, min_warn=20.0),
        DerateLimits(),
        ControlState(derate_factor=0.49),  # within 0.01 of computed
    )
    assert out.derate_factor == 0.49  # held


def test_derate_deadband_passes_large_changes():
    out = compute(
        _inputs(pcc=100.0, soc=12.5),  # ramp 0.5
        _params(),
        _batt(min_soc=5.0, min_warn=20.0),
        DerateLimits(),
        ControlState(derate_factor=0.40),  # > 0.01 away -> update
    )
    assert out.derate_factor == pytest.approx(0.5)


# --------------------------------------------------------------- loop health


def test_step_reports_loop_duration():
    ctrl = EdgeController(_params(), _batt())
    out = ctrl.step(_inputs(pcc=10.0))
    assert out.loop_duration_ms >= 0.0
    assert out.loop_duration_ms < 250.0  # well inside the budget for the pure core


def test_thousand_cycles_no_drift_or_nan():
    ctrl = EdgeController(_params(kp=0.3, ki=0.2, slew=200.0), _batt())
    base_load = 150.0
    batt = 0.0
    for _ in range(1000):
        pcc = base_load + batt
        out = ctrl.step(_inputs(pcc=pcc))
        batt = out.battery_setpoint_kw
        assert out.battery_setpoint_kw == out.battery_setpoint_kw  # not NaN
    assert (base_load + batt) == pytest.approx(0.0, abs=0.5)  # converged, stable


# ----------------------------------- self-consumption PV curtailment, CLOSED LOOP
# Once the battery is full (no charge headroom), curtailment targets 0 export
# (self-consumption) rather than the site feed limit -- priority order is
# PV -> BESS -> load-shed, and grid export is the last resort. An INTEGRATING
# regulator normalised by the *available* PV avoids the steady-state offset a
# one-shot proportional cut would leave; export must converge exactly onto the
# target and hold there without hunting.


def _curtail_plant_step(
    ctrl, curtail, *, pv_available, site_load, feed_limit, soc=92.0, charge_hl=0.0
):
    """One closed-loop cycle: the plant delivers `curtail * pv_available`, the PCC
    exports that minus the site load, and the controller returns a new curtail
    factor. `charge_hl=0.0` (default) models a full battery -- no charge
    headroom -- so export is regulated by PV curtailment alone, targeting 0."""
    pv_delivered = curtail * pv_available          # >= 0 magnitude
    export_kw = pv_delivered - site_load           # +ve when exporting
    out = ctrl.step(
        _inputs(
            pcc=-export_kw,                        # data model: export is negative
            soc=soc,
            charge_hl=charge_hl,
            discharge_hl=1000.0,
            pv_active_power_kw=-pv_delivered,      # curtailed PV, <= 0
            max_feed_kw=feed_limit,
        )
    )
    return out.curtail_factor, export_kw


def test_curtailment_closed_loop_holds_export_at_zero_when_battery_full():
    feed_limit, pv_available, site_load = 300.0, 600.0, 100.0
    ctrl = EdgeController(_params(kp=0.5, ki=0.5), _batt(max_warn=90.0))
    curtail, export = 1.0, None
    for _ in range(60):
        curtail, export = _curtail_plant_step(
            ctrl, curtail, pv_available=pv_available, site_load=site_load, feed_limit=feed_limit
        )
    assert export == pytest.approx(0.0, abs=1.0)                 # holds at 0, not the feed limit
    assert curtail == pytest.approx(site_load / pv_available, abs=0.02)


def test_curtailment_closed_loop_quiescent_at_equilibrium():
    feed_limit, pv_available, site_load = 300.0, 600.0, 100.0
    ctrl = EdgeController(_params(kp=0.5, ki=0.5), _batt(max_warn=90.0))
    curtail = 1.0
    for _ in range(60):
        curtail, _ = _curtail_plant_step(
            ctrl, curtail, pv_available=pv_available, site_load=site_load, feed_limit=feed_limit
        )
    settled = []
    for _ in range(10):
        curtail, _ = _curtail_plant_step(
            ctrl, curtail, pv_available=pv_available, site_load=site_load, feed_limit=feed_limit
        )
        settled.append(curtail)
    assert max(settled) - min(settled) < 0.01                   # no boundary hunting


def test_curtailment_releases_when_battery_regains_headroom():
    # Converged curtailment must climb back toward 1.0 once the battery regains
    # charge headroom (gradual release, no latch) -- PV level is unchanged, only
    # the battery's ability to absorb the surplus changes.
    feed_limit, pv_available, site_load = 300.0, 600.0, 100.0
    ctrl = EdgeController(_params(kp=0.5, ki=0.5), _batt(max_warn=90.0))
    curtail = 1.0
    for _ in range(60):  # converge while battery is full
        curtail, _ = _curtail_plant_step(
            ctrl, curtail, pv_available=pv_available, site_load=site_load, feed_limit=feed_limit
        )
    assert curtail < 0.8
    for _ in range(60):  # battery regains headroom and drops out of the high-SoC
        curtail, _ = _curtail_plant_step(  # warning band: it absorbs the surplus instead
            ctrl,
            curtail,
            pv_available=pv_available,
            site_load=site_load,
            feed_limit=feed_limit,
            charge_hl=1000.0,
            soc=70.0,
        )
    assert curtail == pytest.approx(1.0, abs=0.02)              # released back toward no curtail
