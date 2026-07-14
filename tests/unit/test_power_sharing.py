import pytest
from core.dispatcher import UnitLimits, share_battery_power

TWO_UNITS = {
    "bess-01": UnitLimits(charge_kw=100.0, discharge_kw=100.0),
    "bess-02": UnitLimits(charge_kw=50.0, discharge_kw=50.0),
}


def test_discharge_split_proportional_to_headroom():
    shares, leftover = share_battery_power(-90.0, TWO_UNITS)
    assert shares["bess-01"] == pytest.approx(-60.0)
    assert shares["bess-02"] == pytest.approx(-30.0)
    assert leftover == 0.0


def test_charge_uses_charge_caps():
    limits = {
        "a": UnitLimits(charge_kw=80.0, discharge_kw=10.0),
        "b": UnitLimits(charge_kw=20.0, discharge_kw=10.0),
    }
    shares, leftover = share_battery_power(50.0, limits)
    assert shares["a"] == pytest.approx(40.0)
    assert shares["b"] == pytest.approx(10.0)
    assert leftover == 0.0


def test_saturation_assigns_caps_and_reports_leftover():
    shares, leftover = share_battery_power(-200.0, TWO_UNITS)
    assert shares["bess-01"] == pytest.approx(-100.0)
    assert shares["bess-02"] == pytest.approx(-50.0)
    assert leftover == pytest.approx(-50.0)


def test_zero_capacity_returns_everything_as_leftover():
    limits = {"a": UnitLimits(0.0, 0.0)}
    shares, leftover = share_battery_power(-75.0, limits)
    assert shares == {"a": 0.0}
    assert leftover == pytest.approx(-75.0)


def test_no_units():
    shares, leftover = share_battery_power(-75.0, {})
    assert shares == {}
    assert leftover == pytest.approx(-75.0)


def test_sum_of_shares_equals_command_when_unsaturated():
    shares, leftover = share_battery_power(-123.4, TWO_UNITS)
    assert sum(shares.values()) + leftover == pytest.approx(-123.4)
