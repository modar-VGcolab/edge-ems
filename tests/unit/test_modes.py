"""Mode ladder tests (plan task 23).

Walks every transition via stale-injection, the HOLD timeout to SAFE, the
battery-loss shortcut to SAFE, automatic recovery to RUN, and the SAFE
ramp-to-zero helper.
"""

import pytest
from controller.modes import Mode, ModeController, safe_battery_setpoint


def _mc(hold_max_s=10.0):
    return ModeController(hold_max_s=hold_max_s)


# --------------------------------------------------------------- transitions


def test_starts_in_run():
    assert _mc().mode == Mode.RUN


def test_run_stays_run_when_healthy():
    mc = _mc()
    d = mc.update(data_fresh=True, battery_ok=True, now=0.0)
    assert d.mode == Mode.RUN
    assert not d.changed


def test_run_to_hold_on_stale():
    mc = _mc()
    d = mc.update(data_fresh=False, battery_ok=True, now=100.0)
    assert d.mode == Mode.HOLD
    assert d.changed


def test_hold_persists_within_window():
    mc = _mc(hold_max_s=10.0)
    mc.update(data_fresh=False, battery_ok=True, now=0.0)  # -> HOLD
    d = mc.update(data_fresh=False, battery_ok=True, now=9.0)  # still within window
    assert d.mode == Mode.HOLD
    assert not d.changed


def test_hold_to_safe_after_timeout():
    mc = _mc(hold_max_s=10.0)
    mc.update(data_fresh=False, battery_ok=True, now=0.0)  # -> HOLD at t=0
    d = mc.update(data_fresh=False, battery_ok=True, now=10.0)  # window elapsed
    assert d.mode == Mode.SAFE
    assert d.changed


def test_hold_recovers_to_run_when_data_returns():
    mc = _mc()
    mc.update(data_fresh=False, battery_ok=True, now=0.0)  # -> HOLD
    d = mc.update(data_fresh=True, battery_ok=True, now=2.0)  # data back before timeout
    assert d.mode == Mode.RUN
    assert d.changed


def test_battery_loss_forces_safe_from_run():
    mc = _mc()
    d = mc.update(data_fresh=True, battery_ok=False, now=0.0)
    assert d.mode == Mode.SAFE
    assert d.changed


def test_battery_loss_forces_safe_even_with_fresh_data():
    mc = _mc()
    d = mc.update(data_fresh=True, battery_ok=False, now=0.0)
    assert d.mode == Mode.SAFE


def test_safe_recovers_to_run_when_fully_healthy():
    mc = _mc()
    mc.update(data_fresh=True, battery_ok=False, now=0.0)  # -> SAFE
    d = mc.update(data_fresh=True, battery_ok=True, now=1.0)
    assert d.mode == Mode.RUN
    assert d.changed


def test_safe_stays_while_stale():
    mc = _mc()
    mc.update(data_fresh=True, battery_ok=False, now=0.0)  # -> SAFE
    d = mc.update(data_fresh=False, battery_ok=True, now=1.0)  # data still stale
    assert d.mode == Mode.SAFE
    assert not d.changed


def test_full_ladder_walk():
    mc = _mc(hold_max_s=5.0)
    seq = [
        (True, True, 0.0, Mode.RUN),
        (False, True, 1.0, Mode.HOLD),  # stale -> hold
        (False, True, 3.0, Mode.HOLD),  # holding
        (False, True, 6.0, Mode.SAFE),  # timeout -> safe
        (True, True, 7.0, Mode.RUN),  # recover
    ]
    for fresh, batt, t, expected in seq:
        assert mc.update(data_fresh=fresh, battery_ok=batt, now=t).mode == expected


def test_hold_timer_resets_after_recovery():
    # A second stale episode must get its own full hold window, not inherit the first.
    mc = _mc(hold_max_s=10.0)
    mc.update(data_fresh=False, battery_ok=True, now=0.0)  # HOLD at t=0
    mc.update(data_fresh=True, battery_ok=True, now=5.0)  # back to RUN, timer cleared
    mc.update(data_fresh=False, battery_ok=True, now=100.0)  # HOLD at t=100
    d = mc.update(data_fresh=False, battery_ok=True, now=105.0)  # only 5s in
    assert d.mode == Mode.HOLD


# --------------------------------------------------------------- safe ramp


def test_safe_setpoint_jumps_to_zero_without_slew():
    assert safe_battery_setpoint(-500.0, slew_limit_kw_s=None, period_s=1.0) == 0.0


def test_safe_setpoint_ramps_discharge_toward_zero():
    # last = -500 (discharging), slew 100 kW/s, 1 s -> step toward 0 by 100
    assert safe_battery_setpoint(-500.0, slew_limit_kw_s=100.0, period_s=1.0) == pytest.approx(
        -400.0
    )


def test_safe_setpoint_ramps_charge_toward_zero():
    assert safe_battery_setpoint(500.0, slew_limit_kw_s=100.0, period_s=1.0) == pytest.approx(400.0)


def test_safe_setpoint_does_not_overshoot_zero():
    assert safe_battery_setpoint(-50.0, slew_limit_kw_s=100.0, period_s=1.0) == 0.0
    assert safe_battery_setpoint(50.0, slew_limit_kw_s=100.0, period_s=1.0) == 0.0


def test_safe_setpoint_already_zero_stays_zero():
    assert safe_battery_setpoint(0.0, slew_limit_kw_s=100.0, period_s=1.0) == 0.0
