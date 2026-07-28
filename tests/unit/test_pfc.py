"""Tariff-driven power-factor control (PFC) tests.

Covers: tariff window selection (incl. midnight wrap and default fallback), the
reactive-setpoint maths and lead/lag signs, the inverter S-limit clamp, config
validation, and the Q-V-droop-over-PFC priority inside the control loop.
"""

import math
import time
from datetime import datetime

import pytest
from common.config_models import (
    DroopConfig,
    PfcConfig,
    PfcTarget,
    PfcWindow,
    PfDroop,
    QvDroop,
    validate_edge_ems_config,
)
from common.points import GOOD, PointValue
from controller.control_loop import ControlLoop, Snapshot
from controller.droop import DroopController
from controller.edge_controller import BatteryLimits, ControlParams, DerateLimits, EdgeController
from controller.modes import ModeController
from controller.pfc import PfcController


def _epoch_at(hour, minute=0):
    """Epoch for a local wall-clock time (PFC reads local time via fromtimestamp)."""
    return datetime(2026, 7, 14, hour, minute, 0).timestamp()


def _cfg(**kw):
    base = dict(enabled=True)
    base.update(kw)
    return PfcConfig(**base)


# --------------------------------------------------------------- window selection


def test_window_selected_by_time_of_day():
    pfc = PfcController(
        _cfg(
            windows=[
                PfcWindow(start="08:00", end="20:00", pf_target=0.95, mode="lagging"),
                PfcWindow(start="20:00", end="08:00", pf_target=0.98, mode="leading"),
            ]
        )
    )
    assert pfc.active_target(_epoch_at(12)) == (0.95, "lagging")   # inside peak
    assert pfc.active_target(_epoch_at(2)) == (0.98, "leading")    # inside wrap window
    assert pfc.active_target(_epoch_at(23)) == (0.98, "leading")   # wrap, evening side


def test_default_when_no_window_matches():
    pfc = PfcController(
        _cfg(
            default=PfcTarget(pf_target=1.0, mode="unity"),
            windows=[PfcWindow(start="08:00", end="20:00", pf_target=0.95, mode="lagging")],
        )
    )
    assert pfc.active_target(_epoch_at(3)) == (1.0, "unity")  # outside the only window


def test_first_matching_window_wins():
    pfc = PfcController(
        _cfg(
            windows=[
                PfcWindow(start="08:00", end="18:00", pf_target=0.95, mode="lagging"),
                PfcWindow(start="10:00", end="12:00", pf_target=0.90, mode="leading"),
            ]
        )
    )
    assert pfc.active_target(_epoch_at(11)) == (0.95, "lagging")  # first wins on overlap


# --------------------------------------------------------------- reactive maths


def test_unity_commands_zero_reactive():
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=1.0, mode="unity")))
    assert pfc.reactive_setpoint_kvar(time.time(), pcc_active_kw=150.0, battery_active_kw=0.0) == 0.0


def test_lagging_positive_leading_negative():
    pcc = 100.0
    pf = 0.95
    expected_mag = pcc * math.tan(math.acos(pf))
    lag = PfcController(_cfg(default=PfcTarget(pf_target=pf, mode="lagging")))
    lead = PfcController(_cfg(default=PfcTarget(pf_target=pf, mode="leading")))
    q_lag = lag.reactive_setpoint_kvar(time.time(), pcc, 0.0)
    q_lead = lead.reactive_setpoint_kvar(time.time(), pcc, 0.0)
    assert q_lag == pytest.approx(expected_mag)       # +inductive
    assert q_lead == pytest.approx(-expected_mag)     # -capacitive


def test_magnitude_scales_with_pcc_active_power():
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=0.9, mode="lagging")))
    q1 = pfc.reactive_setpoint_kvar(time.time(), 100.0, 0.0)
    q2 = pfc.reactive_setpoint_kvar(time.time(), 200.0, 0.0)
    assert q2 == pytest.approx(2 * q1)


def test_sign_convention_flip():
    pfc = PfcController(_cfg(q_sign_convention=-1.0, default=PfcTarget(pf_target=0.9, mode="lagging")))
    q = pfc.reactive_setpoint_kvar(time.time(), 100.0, 0.0)
    assert q < 0  # convention flip turns a lagging (nominally +) command negative


# --------------------------------------------------------------- capability clamp


def test_s_limit_clamps_reactive():
    # S=100, P=90 -> reactive headroom sqrt(100^2 - 90^2) = 43.588...
    pfc = PfcController(
        _cfg(s_rated_kva=100.0, default=PfcTarget(pf_target=0.7, mode="lagging"))
    )
    # pcc=200, pf=0.7 -> raw q_mag = 200*tan(acos(0.7)) ~= 204 kVAr, must clamp
    q = pfc.reactive_setpoint_kvar(time.time(), pcc_active_kw=200.0, battery_active_kw=90.0)
    assert q == pytest.approx(math.sqrt(100.0**2 - 90.0**2))


def test_s_limit_zero_headroom_when_active_at_rating():
    pfc = PfcController(_cfg(s_rated_kva=100.0, default=PfcTarget(pf_target=0.8, mode="lagging")))
    q = pfc.reactive_setpoint_kvar(time.time(), 100.0, battery_active_kw=100.0)
    assert q == 0.0  # no apparent-power headroom left for reactive


def test_no_clamp_when_s_rating_absent():
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=0.7, mode="lagging")))
    q = pfc.reactive_setpoint_kvar(time.time(), 200.0, battery_active_kw=1000.0)
    assert q == pytest.approx(200.0 * math.tan(math.acos(0.7)))  # unclamped


# ------------------------------------------------- other_reactive_kvar subtraction


def test_other_reactive_kvar_reduces_battery_residual():
    # A large fixed load already contributing lagging (+) reactive at the PCC
    # means the battery only needs to make up what's left of the target.
    pcc = 100.0
    pf = 0.9
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=pf, mode="lagging")))
    q_mag = pcc * math.tan(math.acos(pf))
    q_alone = pfc.reactive_setpoint_kvar(time.time(), pcc, 0.0)
    q_with_other = pfc.reactive_setpoint_kvar(time.time(), pcc, 0.0, other_reactive_kvar=30.0)
    assert q_alone == pytest.approx(q_mag)
    assert q_with_other == pytest.approx(q_mag - 30.0)


def test_other_reactive_kvar_can_exceed_target_and_go_negative():
    # If the other assets already over-supply the target (a large fixed load
    # bigger than what's actually needed), the battery must absorb reactive
    # in the opposite direction, not just clamp at 0.
    pcc = 50.0
    pf = 0.95
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=pf, mode="lagging")))
    q_mag = pcc * math.tan(math.acos(pf))
    q = pfc.reactive_setpoint_kvar(time.time(), pcc, 0.0, other_reactive_kvar=q_mag * 3)
    assert q == pytest.approx(q_mag - q_mag * 3)
    assert q < 0


def test_other_reactive_kvar_applied_before_sign_convention():
    # q_sign_convention calibrates the battery's own VarSet write direction; the
    # subtraction must happen in the natural PCC sign convention *first*, then
    # get flipped along with everything else -- not applied post-flip (which
    # would subtract in the wrong direction for a flipped site).
    pcc, pf, other = 100.0, 0.9, 20.0
    q_mag = pcc * math.tan(math.acos(pf))
    normal = PfcController(_cfg(default=PfcTarget(pf_target=pf, mode="lagging")))
    flipped = PfcController(
        _cfg(q_sign_convention=-1.0, default=PfcTarget(pf_target=pf, mode="lagging"))
    )
    q_normal = normal.reactive_setpoint_kvar(time.time(), pcc, 0.0, other)
    q_flipped = flipped.reactive_setpoint_kvar(time.time(), pcc, 0.0, other)
    assert q_normal == pytest.approx(q_mag - other)
    assert q_flipped == pytest.approx(-(q_mag - other))


def test_other_reactive_kvar_still_respects_s_limit_clamp():
    # The residual after subtraction is what gets clamped to inverter capability,
    # not the raw pre-subtraction target.
    pfc = PfcController(_cfg(s_rated_kva=50.0, default=PfcTarget(pf_target=0.7, mode="lagging")))
    q = pfc.reactive_setpoint_kvar(
        time.time(), pcc_active_kw=200.0, battery_active_kw=0.0, other_reactive_kvar=-500.0
    )
    assert q == pytest.approx(50.0)  # driven positive and past S_rated -> clamped at +q_lim


def test_other_reactive_kvar_defaults_to_zero():
    # Backward compatible: omitting the argument matches the pre-existing
    # single-source behaviour exactly.
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=0.9, mode="lagging")))
    q_default = pfc.reactive_setpoint_kvar(time.time(), 100.0, 0.0)
    q_explicit = pfc.reactive_setpoint_kvar(time.time(), 100.0, 0.0, other_reactive_kvar=0.0)
    assert q_default == q_explicit


# --------------------------------------------------------------- config validation


def test_bad_sign_convention_rejected():
    with pytest.raises(ValueError):
        PfcConfig(enabled=True, q_sign_convention=2.0)


def test_bad_time_format_rejected():
    with pytest.raises(ValueError):
        PfcWindow(start="25:00", end="08:00", pf_target=0.9, mode="lagging")


def test_pf_target_out_of_range_rejected():
    with pytest.raises(ValueError):
        PfcTarget(pf_target=1.5, mode="lagging")
    with pytest.raises(ValueError):
        PfcTarget(pf_target=0.0, mode="lagging")


def test_edge_ems_config_defaults_pfc_disabled(dm):
    # A config file omitting `pfc` should still validate, with PFC off.
    raw = {
        "data_model_version": dm.version,
        "influxdb": {"url": "u", "org": "o", "bucket": "b", "token": "t"},
        "mqtt": {"broker_address": "a", "client_id": "c"},
        "controller": {},
        "droop": {
            "p_f_droop": {"dP_f": [-1, 0, 0, 1], "dQ_f": [0, 0, 0, 0], "f": [1, 2, 3, 4]},
            "q_v_droop": {"dP_V": [0, 0, 0, 0], "dQ_V": [-1, 0, 0, 1], "V": [1, 2, 3, 4]},
        },
        "asset_aggregation": {},
    }
    cfg = validate_edge_ems_config(raw, dm)
    assert cfg.pfc.enabled is False


# --------------------------------------------------------------- loop priority


def _pv(value, quality=GOOD):
    return PointValue(value, time.time(), quality)


def _snapshot(*, pcc_kw=100.0, voltage=230.0):
    return Snapshot(
        aggregates={
            "battery": {
                "soc_pct": _pv(50.0),
                "active_power_kw": _pv(0.0),
                "available_charge_power_kw": _pv(1000.0),
                "available_discharge_power_kw": _pv(1000.0),
            },
            "pv": {"active_power_kw": _pv(0.0)},
        },
        pcc={
            "active_power_kw": _pv(pcc_kw),
            "frequency_hz": _pv(50.0),
            "voltage_v": _pv(voltage),
        },
    )


def _loop_with(pfc, droop, snapshot):
    params = ControlParams(kp=0.5, ki=0.1, update_period=1.0, slew_limit_kw_s=None)
    return ControlLoop(
        edge=EdgeController(params, BatteryLimits(1000, 1000, 5, 95), DerateLimits()),
        modes=ModeController(hold_max_s=10.0),
        droop=droop,
        read_snapshot=lambda now: snapshot,
        publish=lambda *a: True,
        pcc_base_kw=2000.0,
        max_feed_kw=999.0,
        pcc_setpoint_kw=0.0,
        pfc=pfc,
    )


def _q_v_droop():
    cfg = DroopConfig(
        enabled=True,
        p_f_droop=PfDroop(enabled=False, dP_f=[0, 0, 0, 0], dQ_f=[0, 0, 0, 0], f=[49, 50, 51, 52]),
        q_v_droop=QvDroop(
            enabled=True, dP_V=[0, 0, 0, 0], dQ_V=[-0.1, -0.05, 0.05, 0.1], V=[207, 219, 241, 253]
        ),
    )
    return DroopController(cfg, pcc_base_kw=2000.0)


def test_pfc_supplies_reactive_when_droop_idle():
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=0.9, mode="lagging")))
    loop = _loop_with(pfc, _q_v_droop(), _snapshot(pcc_kw=100.0, voltage=230.0))
    r = loop.run_once(time.time())  # V nominal -> droop Q = 0 -> PFC fills in
    assert r.reactive_setpoint_kvar == pytest.approx(100.0 * math.tan(math.acos(0.9)))


def test_qv_droop_overrides_pfc_on_voltage_excursion():
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=0.9, mode="lagging")))
    loop = _loop_with(pfc, _q_v_droop(), _snapshot(pcc_kw=100.0, voltage=253.0))
    r = loop.run_once(time.time())  # V high -> droop commands Q, must win over PFC
    assert r.reactive_setpoint_kvar == pytest.approx(0.1 * 2000.0)  # droop dQ, not PFC


def test_pfc_active_without_droop_configured():
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=0.95, mode="leading")))
    loop = _loop_with(pfc, None, _snapshot(pcc_kw=80.0))
    r = loop.run_once(time.time())
    assert r.reactive_setpoint_kvar == pytest.approx(-80.0 * math.tan(math.acos(0.95)))


def test_pfc_loop_sums_other_reactive_sources_before_sizing_battery():
    # PV, flexible_load, and meter (fixed load) aggregates each contribute
    # reactive at the PCC; run_once must sum all three and hand the residual
    # to pfc.reactive_setpoint_kvar, not size the battery for the whole target.
    snap = _snapshot(pcc_kw=100.0)
    snap.aggregates["pv"]["reactive_power_kvar"] = _pv(5.0)
    snap.aggregates["flexible_load"] = {"reactive_power_kvar": _pv(2.0)}
    snap.aggregates["meter"] = {"reactive_power_kvar": _pv(47.0)}  # load-02 fixed load
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=0.9, mode="lagging")))
    loop = _loop_with(pfc, None, snap)
    r = loop.run_once(time.time())
    q_target = 100.0 * math.tan(math.acos(0.9))
    assert r.reactive_setpoint_kvar == pytest.approx(q_target - (5.0 + 2.0 + 47.0))


def test_pfc_loop_missing_reactive_aggregates_default_to_zero():
    # Sites without flexible_load/meter wired (or a COMM_FAIL aggregate) must not
    # crash or silently drop PFC -- other_reactive_kvar just degrades to 0, so
    # the battery is sized for the full target as before this feature existed.
    snap = _snapshot(pcc_kw=100.0)  # no flexible_load/meter keys at all; pv has no Q field
    pfc = PfcController(_cfg(default=PfcTarget(pf_target=0.9, mode="lagging")))
    loop = _loop_with(pfc, None, snap)
    r = loop.run_once(time.time())
    assert r.reactive_setpoint_kvar == pytest.approx(100.0 * math.tan(math.acos(0.9)))
