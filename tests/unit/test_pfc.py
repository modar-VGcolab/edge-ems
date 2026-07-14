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
