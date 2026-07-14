import time

import pytest
from core.adapters.base import COMM_FAIL, GOOD, PointValue
from core.aggregator import aggregate_class


def _pv(value, quality=GOOD):
    return PointValue(value, time.time(), quality)


def test_battery_powers_are_summed(dm):
    readings = {
        "bess-01": {"active_power_kw": _pv(-100.0), "soc_pct": _pv(60.0)},
        "bess-02": {"active_power_kw": _pv(-50.0), "soc_pct": _pv(80.0)},
    }
    agg = aggregate_class("battery", dm, readings)
    assert agg["active_power_kw"].value == pytest.approx(-150.0)
    assert agg["active_power_kw"].quality == GOOD


def test_soc_is_capacity_weighted(dm):
    readings = {
        "bess-01": {"soc_pct": _pv(60.0)},
        "bess-02": {"soc_pct": _pv(80.0)},
    }
    # bess-01 has 3x the capacity: weighted soc = (60*3 + 80*1)/4 = 65
    agg = aggregate_class("battery", dm, readings, weights={"bess-01": 3.0, "bess-02": 1.0})
    assert agg["soc_pct"].value == pytest.approx(65.0)


def test_soc_unweighted_is_plain_mean(dm):
    readings = {"a": {"soc_pct": _pv(60.0)}, "b": {"soc_pct": _pv(80.0)}}
    agg = aggregate_class("battery", dm, readings)
    assert agg["soc_pct"].value == pytest.approx(70.0)


def test_failed_asset_excluded_from_aggregate(dm):
    readings = {
        "bess-01": {"active_power_kw": _pv(-100.0)},
        "bess-02": {"active_power_kw": _pv(None, COMM_FAIL)},
    }
    agg = aggregate_class("battery", dm, readings)
    assert agg["active_power_kw"].value == pytest.approx(-100.0)
    assert agg["active_power_kw"].quality == GOOD


def test_all_assets_failed_yields_comm_fail(dm):
    readings = {
        "bess-01": {"soc_pct": _pv(None, COMM_FAIL)},
        "bess-02": {"soc_pct": _pv(None, COMM_FAIL)},
    }
    agg = aggregate_class("battery", dm, readings)
    assert agg["soc_pct"].quality == COMM_FAIL
    assert agg["soc_pct"].value is None


def test_unaggregated_class_raises(dm):
    with pytest.raises(ValueError, match="not aggregated"):
        aggregate_class("pcc", dm, {})


def test_aggregate_covers_all_data_model_points(dm):
    readings = {
        "bess-01": {
            "soc_pct": _pv(50.0),
            "active_power_kw": _pv(-10.0),
            "reactive_power_kvar": _pv(0.0),
            "available_charge_power_kw": _pv(100.0),
            "available_discharge_power_kw": _pv(100.0),
        }
    }
    agg = aggregate_class("battery", dm, readings)
    assert set(agg) == set(dm.aggregates["battery"])
