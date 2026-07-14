"""Controller main loop tests (plan task 26 wiring).

Covers input assembly (fresh / stale / battery-missing), the RUN/HOLD/SAFE cycle
behaviour end to end with fake reader+publisher, publish-on-change for derates,
the control-measurement projection, and the config-driven builders.
"""

import time

import pytest
from common.points import COMM_FAIL, GOOD, STALE, PointValue
from controller.control_loop import (
    ControlLoop,
    Snapshot,
    assemble_inputs,
    battery_limits_from_config,
    control_measurement_fields,
    derate_limits_from_config,
    params_from_config,
    pcc_params_from_config,
)
from controller.droop import DroopController
from controller.edge_controller import BatteryLimits, ControlParams, DerateLimits, EdgeController
from controller.modes import ModeController


def _pv(value, quality=GOOD, ts=None):
    return PointValue(value, ts if ts is not None else time.time(), quality)


def _snapshot(*, pcc_kw=100.0, soc=50.0, charge=1000.0, discharge=1000.0, quality=GOOD, pv_kw=0.0):
    return Snapshot(
        aggregates={
            "battery": {
                "soc_pct": _pv(soc, quality),
                "active_power_kw": _pv(0.0, quality),
                "available_charge_power_kw": _pv(charge, quality),
                "available_discharge_power_kw": _pv(discharge, quality),
            },
            "pv": {"active_power_kw": _pv(pv_kw, quality)},
        },
        pcc={
            "active_power_kw": _pv(pcc_kw, quality),
            "frequency_hz": _pv(50.0, quality),
            "voltage_v": _pv(230.0, quality),
        },
    )


class FakePublisher:
    def __init__(self):
        self.calls = []  # (asset_class, setpoints)

    def __call__(self, asset_class, setpoints, now):
        self.calls.append((asset_class, dict(setpoints)))
        return True

    def classes(self):
        return [c for c, _ in self.calls]

    def last_for(self, asset_class):
        for c, sp in reversed(self.calls):
            if c == asset_class:
                return sp
        return None


def _loop(snapshot, *, params=None, batt=None, modes=None, droop=None, write_control=None):
    params = params or ControlParams(kp=0.5, ki=0.1, update_period=1.0, slew_limit_kw_s=None)
    batt = batt or BatteryLimits(1000, 1000, 5, 95)
    pub = FakePublisher()
    loop = ControlLoop(
        edge=EdgeController(params, batt, DerateLimits()),
        modes=modes or ModeController(hold_max_s=10.0),
        droop=droop,
        read_snapshot=lambda now: snapshot,
        publish=pub,
        pcc_base_kw=2000.0,
        max_feed_kw=999.0,
        pcc_setpoint_kw=0.0,
        write_control=write_control,
    )
    return loop, pub


# --------------------------------------------------------------- assembly


def test_assemble_fresh_inputs():
    inp, f, v, fresh, batt_ok = assemble_inputs(
        _snapshot(pcc_kw=120.0, soc=42.0), pcc_setpoint_kw=0.0, max_feed_kw=999.0
    )
    assert inp.pcc_meas_kw == 120.0
    assert inp.soc_pct == 42.0
    assert (f, v) == (50.0, 230.0)
    assert fresh and batt_ok


def test_assemble_stale_is_not_fresh_but_battery_ok():
    inp, _, _, fresh, batt_ok = assemble_inputs(
        _snapshot(quality=STALE), pcc_setpoint_kw=0.0, max_feed_kw=None
    )
    assert not fresh  # stale data -> HOLD path
    assert batt_ok  # battery present, just stale


def test_assemble_missing_battery_not_ok():
    snap = _snapshot()
    snap.aggregates["battery"] = {}  # battery aggregate gone
    inp, _, _, fresh, batt_ok = assemble_inputs(snap, pcc_setpoint_kw=0.0, max_feed_kw=None)
    assert not batt_ok and not fresh


def test_assemble_comm_fail_battery_not_ok():
    snap = _snapshot()
    snap.aggregates["battery"]["soc_pct"] = _pv(50.0, COMM_FAIL)
    _, _, _, _, batt_ok = assemble_inputs(snap, pcc_setpoint_kw=0.0, max_feed_kw=None)
    assert not batt_ok


def test_assemble_converter_alarm_forces_battery_not_ok():
    # A tripped converter still reports SoC; an asserted converter_alarm must
    # still drive battery_ok False (the E3 islanding gap).
    snap = _snapshot()
    snap.aggregates["battery"]["converter_alarm"] = _pv(1.0, GOOD)
    _, _, _, fresh, batt_ok = assemble_inputs(snap, pcc_setpoint_kw=0.0, max_feed_kw=None)
    assert not batt_ok and not fresh


def test_assemble_converter_alarm_zero_is_ok():
    snap = _snapshot()
    snap.aggregates["battery"]["converter_alarm"] = _pv(0.0, GOOD)
    _, _, _, _, batt_ok = assemble_inputs(snap, pcc_setpoint_kw=0.0, max_feed_kw=None)
    assert batt_ok


def test_assemble_converter_alarm_comm_fail_is_graceful():
    # An un-wired / COMM_FAIL alarm must NOT force SAFE on its own: behaviour is
    # unchanged until a real converter-status signal is mapped through.
    snap = _snapshot()
    snap.aggregates["battery"]["converter_alarm"] = _pv(None, COMM_FAIL)
    _, _, _, _, batt_ok = assemble_inputs(snap, pcc_setpoint_kw=0.0, max_feed_kw=None)
    assert batt_ok


# --------------------------------------------------------------- RUN cycle


def test_run_cycle_discharges_on_import_and_publishes():
    loop, pub = _loop(_snapshot(pcc_kw=100.0))
    r = loop.run_once(now=1.0)
    assert r.mode == "RUN"
    assert r.battery_setpoint_kw < 0  # import -> discharge
    assert r.pcc_error_kw == pytest.approx(-100.0)
    assert "battery" in pub.classes()
    assert pub.last_for("battery")["active_power_setpoint_kw"] == pytest.approx(
        r.battery_setpoint_kw
    )


def test_run_cycle_writes_control_measurement():
    captured = {}
    loop, _ = _loop(
        _snapshot(pcc_kw=50.0), write_control=lambda fields, now: captured.update(fields)
    )
    loop.run_once(now=1.0)
    assert set(captured) == {
        "pcc_error_kw",
        "pi_output_kw",
        "derate_factor",
        "curtail_factor",
        "loop_duration_ms",
        "mode",
    }
    assert captured["mode"] == "RUN"


def test_droop_shifts_setpoint_in_run():
    # Over-frequency raises the PCC setpoint; with zero PCC error baseline the
    # battery is commanded to charge toward the shifted target.
    from common.config_models import DroopConfig, PfDroop, QvDroop

    cfg = DroopConfig(
        enabled=True,
        p_f_droop=PfDroop(
            enabled=True,
            dP_f=[-1.0, 0.0, 0.0, 1.0],
            dQ_f=[0.0, 0.0, 0.0, 0.0],
            f=[49, 49.5, 50.5, 51],
        ),
        q_v_droop=QvDroop(
            enabled=False, dP_V=[0, 0, 0, 0], dQ_V=[-1, 0, 0, 1], V=[210, 225, 235, 250]
        ),
    )
    snap = _snapshot(pcc_kw=0.0)
    snap.pcc["frequency_hz"] = _pv(51.0)  # max -> +1 p.u. * base(2000) = +2000 kW setpoint
    droop = DroopController(cfg, pcc_base_kw=2000.0)
    loop, _ = _loop(snap, droop=droop)
    r = loop.run_once(now=1.0)
    # error = (0 + 2000) - 0 = +2000 -> positive (charge) demand
    assert r.pcc_error_kw == pytest.approx(2000.0)
    assert r.battery_setpoint_kw > 0


# --------------------------------------------------------------- HOLD cycle


def test_hold_freezes_last_setpoint():
    modes = ModeController(hold_max_s=10.0)
    loop, pub = _loop(_snapshot(pcc_kw=100.0), modes=modes)
    r1 = loop.run_once(now=1.0)  # RUN, computes a discharge setpoint
    held_value = r1.battery_setpoint_kw
    # next cycle data goes stale -> HOLD, must republish the same setpoint
    loop.read_snapshot = lambda now: _snapshot(pcc_kw=9999.0, quality=STALE)
    r2 = loop.run_once(now=2.0)
    assert r2.mode == "HOLD"
    assert r2.battery_setpoint_kw == pytest.approx(held_value)
    assert pub.last_for("battery")["active_power_setpoint_kw"] == pytest.approx(held_value)


# --------------------------------------------------------------- SAFE cycle


def test_safe_ramps_battery_to_zero_and_releases_derates():
    params = ControlParams(kp=0.5, ki=0.1, update_period=1.0, slew_limit_kw_s=100.0)
    modes = ModeController(hold_max_s=0.0)  # any stale cycle goes straight to SAFE
    loop, pub = _loop(_snapshot(pcc_kw=100.0), params=params, modes=modes)
    loop.run_once(now=0.0)  # RUN, battery discharging (-60)
    # battery aggregate disappears -> SAFE
    snap = _snapshot()
    snap.aggregates["battery"] = {}
    loop.read_snapshot = lambda now: snap
    r = loop.run_once(now=1.0)
    assert r.mode == "SAFE"
    # last setpoint was -60; a 100 kW/s ramp toward 0 overshoots, so it clamps at 0
    assert r.battery_setpoint_kw == pytest.approx(0.0)
    assert r.derate_factor == 1.0 and r.curtail_factor == 1.0


def test_safe_ramp_is_gradual_when_far_from_zero():
    params = ControlParams(kp=0.5, ki=0.1, update_period=1.0, slew_limit_kw_s=50.0)
    modes = ModeController(hold_max_s=0.0)
    # large sustained import drives the battery to a deep discharge first
    loop, _ = _loop(
        _snapshot(pcc_kw=100000.0),
        params=params,
        batt=BatteryLimits(1000, 1000, 5, 95),
        modes=modes,
    )
    loop.run_once(now=0.0)  # RUN -> discharge clamps near -1000 (slew-limited to -50)
    deep = loop.edge.state.last_battery_setpoint_kw
    snap = _snapshot()
    snap.aggregates["battery"] = {}
    loop.read_snapshot = lambda now: snap
    r = loop.run_once(now=1.0)
    assert r.mode == "SAFE"
    assert r.battery_setpoint_kw == pytest.approx(deep + 50.0)  # one slew step toward 0


def test_safe_resets_integrator():
    modes = ModeController(hold_max_s=0.0)
    loop, _ = _loop(_snapshot(pcc_kw=100.0), modes=modes)
    loop.run_once(now=0.0)
    snap = _snapshot()
    snap.aggregates["battery"] = {}
    loop.read_snapshot = lambda now: snap
    loop.run_once(now=1.0)
    assert loop.edge.state.integral == 0.0


# --------------------------------------------------------------- publish-on-change


def test_derate_published_only_on_change():
    # Force a low-SoC derate, then hold SoC constant: derate should publish once.
    batt = BatteryLimits(1000, 1000, 5, 95, min_warning_soc_pct=20.0)
    snap = _snapshot(pcc_kw=100.0, soc=12.5)  # mid warning band -> derate 0.5
    loop, pub = _loop(snap, batt=batt)
    loop.run_once(now=1.0)
    first = [c for c in pub.classes() if c == "flexible_load"]
    loop.run_once(now=2.0)  # same SoC -> same derate -> no second publish
    second = [c for c in pub.classes() if c == "flexible_load"]
    assert len(first) == 1
    assert len(second) == 1  # unchanged


def test_battery_published_every_cycle():
    loop, pub = _loop(_snapshot(pcc_kw=100.0))
    loop.run_once(now=1.0)
    loop.run_once(now=2.0)
    assert pub.classes().count("battery") == 2


# --------------------------------------------------------------- control fields


def test_control_measurement_fields_shape():
    from controller.control_loop import CycleResult

    r = CycleResult(
        mode="RUN",
        mode_changed=False,
        mode_reason="",
        battery_setpoint_kw=-60.0,
        reactive_setpoint_kvar=0.0,
        derate_factor=1.0,
        curtail_factor=1.0,
        pcc_error_kw=-100.0,
        pi_output_kw=-60.0,
        loop_duration_ms=0.1,
        data_fresh=True,
        battery_ok=True,
        published=("battery",),
    )
    fields = control_measurement_fields(r)
    assert fields["mode"] == "RUN"
    assert fields["pcc_error_kw"] == -100.0


# --------------------------------------------------------------- config builders


def test_builders_from_example_config(asset_config_raw, edge_ems_config_raw, dm):
    from common.config_models import validate_asset_config, validate_edge_ems_config

    ac = validate_asset_config(asset_config_raw, dm)
    ec = validate_edge_ems_config(edge_ems_config_raw, dm)

    bl = battery_limits_from_config(ac)
    assert bl.max_charge_kw == 1000.0
    assert bl.min_soc_pct == 5.0
    assert bl.max_warning_soc_pct == 90.0

    dl = derate_limits_from_config(ac)
    assert dl.pv_min_derate == 0.0
    assert dl.load_min_derate == 0.2

    base, max_feed = pcc_params_from_config(ac)
    assert base == 1000.0  # PCC max import = 1000 kVA (per-unit base)
    assert max_feed == 999.0

    p = params_from_config(ec)
    assert p.kp == 0.5 and p.ki == 0.5 and p.slew_limit_kw_s == 200.0


def test_pcc_builder_requires_active_pcc(asset_config_raw, dm):
    from common.config_models import validate_asset_config

    ac = validate_asset_config(asset_config_raw, dm)
    ac.assets[:] = [a for a in ac.assets if a.asset_class != "pcc"]
    with pytest.raises(ValueError):
        pcc_params_from_config(ac)


# --------------------------------------------------- PCC plausibility gate (E3 fix)
# Regression for the islanding finding: on a clean grid loss the PCC meter
# collapses to ~0 V / 0 Hz, which previously read as fresh, so the controller
# stayed in RUN. Implausible PCC V/Hz must now fold into not-fresh and drive the
# mode ladder to HOLD/SAFE, while real droop excursions stay plausible.


def test_assemble_implausible_pcc_voltage_not_fresh():
    snap = _snapshot()
    snap.pcc["voltage_v"] = _pv(0.0)  # collapsed grid
    _, _, _, fresh, batt_ok = assemble_inputs(snap, pcc_setpoint_kw=0.0, max_feed_kw=None)
    assert not fresh and batt_ok  # battery fine; only the grid is implausible


def test_assemble_implausible_pcc_frequency_not_fresh():
    for bad_hz in (0.0, 60.0):  # collapse and out-of-band
        snap = _snapshot()
        snap.pcc["frequency_hz"] = _pv(bad_hz)
        _, _, _, fresh, _ = assemble_inputs(snap, pcc_setpoint_kw=0.0, max_feed_kw=None)
        assert not fresh


def test_assemble_droop_excursions_stay_plausible():
    # +-10% droop excursions (207-253 V, 49.5-50.5 Hz) must NOT trip the gate.
    for v, hz in ((207.0, 49.5), (253.0, 50.5)):
        snap = _snapshot()
        snap.pcc["voltage_v"] = _pv(v)
        snap.pcc["frequency_hz"] = _pv(hz)
        _, _, _, fresh, _ = assemble_inputs(snap, pcc_setpoint_kw=0.0, max_feed_kw=999.0)
        assert fresh


def _dead_grid(pcc_kw=0.0):
    snap = _snapshot(pcc_kw=pcc_kw)
    snap.pcc["voltage_v"] = _pv(0.0)
    snap.pcc["frequency_hz"] = _pv(0.0)
    return snap


def test_grid_loss_drives_safe_and_recovers():
    # A collapsed grid is "data not fresh", so the ladder runs RUN -> HOLD -> SAFE
    # (battery is still present, so this is not the direct battery-loss SAFE path).
    # With hold_max_s=0 the SAFE transition lands on the next not-fresh cycle.
    params = ControlParams(kp=0.5, ki=0.1, update_period=1.0, slew_limit_kw_s=100.0)
    modes = ModeController(hold_max_s=0.0)
    loop, _ = _loop(_snapshot(pcc_kw=100.0), params=params, modes=modes)
    assert loop.run_once(now=0.0).mode == "RUN"
    loop.read_snapshot = lambda now: _dead_grid()
    assert loop.run_once(now=1.0).mode == "HOLD"  # first not-fresh cycle freezes
    assert loop.run_once(now=2.0).mode == "SAFE"  # island confirmed -> safe state
    loop.read_snapshot = lambda now: _snapshot(pcc_kw=100.0)
    assert loop.run_once(now=3.0).mode == "RUN"  # auto-recovers when the grid returns


def test_grid_loss_holds_before_safe_within_hold_window():
    modes = ModeController(hold_max_s=10.0)
    loop, _ = _loop(_snapshot(pcc_kw=100.0), modes=modes)
    loop.run_once(now=0.0)  # RUN
    loop.read_snapshot = lambda now: _dead_grid()
    assert loop.run_once(now=1.0).mode == "HOLD"  # frozen within the hold window
    assert loop.run_once(now=20.0).mode == "SAFE"  # auto-transition past hold_max_s


# ----------------------------------------------- battery-loss detection (D2 / E3)
# A tripped converter still publishes SoC, so a converter_alarm (or a COMM_FAIL on
# the battery aggregate) must take the controller to SAFE end-to-end, with prompt
# recovery once the battery is healthy again.


def test_converter_alarm_drives_run_to_safe_and_recovers():
    params = ControlParams(kp=0.5, ki=0.1, update_period=1.0, slew_limit_kw_s=100.0)
    loop, _ = _loop(_snapshot(pcc_kw=100.0), params=params)
    assert loop.run_once(now=0.0).mode == "RUN"
    tripped = _snapshot(pcc_kw=100.0)
    tripped.aggregates["battery"]["converter_alarm"] = _pv(1.0, GOOD)
    loop.read_snapshot = lambda now: tripped
    assert loop.run_once(now=1.0).mode == "SAFE"  # battery unavailable -> SAFE
    loop.read_snapshot = lambda now: _snapshot(pcc_kw=100.0)
    assert loop.run_once(now=2.0).mode == "RUN"  # alarm cleared -> recover


def test_battery_comm_fail_drives_safe():
    loop, _ = _loop(_snapshot(pcc_kw=100.0))
    loop.run_once(now=0.0)
    lost = _snapshot(pcc_kw=100.0)
    lost.aggregates["battery"]["soc_pct"] = _pv(50.0, COMM_FAIL)
    loop.read_snapshot = lambda now: lost
    assert loop.run_once(now=1.0).mode == "SAFE"
