import pytest
from common.data_model import DataModel


def test_loads_and_versioned(dm: DataModel):
    assert dm.version == "0.1"


def test_all_asset_classes_present(dm: DataModel):
    assert set(dm.asset_classes) == {"pcc", "battery", "pv", "flexible_load", "meter"}


def test_battery_soc_unit_and_direction(dm: DataModel):
    soc = dm.asset_classes["battery"].points["soc_pct"]
    assert soc.unit == "%"
    assert soc.direction == "input"


def test_pv_outputs_are_derate_only(dm: DataModel):
    outputs = dm.asset_classes["pv"].points_by_direction("output")
    assert set(outputs) == {"derate_factor_setpoint"}


def test_battery_control_is_power_setpoint(dm: DataModel):
    assert dm.asset_classes["battery"].control == "power_setpoint"
    assert dm.asset_classes["meter"].control == "none"


def test_validate_fields_catches_typo(dm: DataModel):
    errors = dm.validate_fields("battery", ["soc_pct", "active_powerr_kw"])
    assert len(errors) == 1
    assert "active_powerr_kw" in errors[0]


def test_validate_fields_respects_direction(dm: DataModel):
    # soc_pct is an input; asking for it as an output must fail
    errors = dm.validate_fields("battery", ["soc_pct"], direction="output")
    assert len(errors) == 1


def test_unknown_class_reported(dm: DataModel):
    errors = dm.validate_fields("wind_turbine", ["active_power_kw"])
    assert errors == ["unknown asset class 'wind_turbine'"]


def test_aggregates_reference_known_points(dm: DataModel):
    for cname, fields in dm.aggregates.items():
        for f in fields:
            assert f in dm.asset_classes[cname].points


def test_self_check_rejects_bad_aggregates(dm: DataModel):
    bad = DataModel(version="x", asset_classes=dm.asset_classes, aggregates={"battery": ["nope"]})
    with pytest.raises(Exception):
        errors = bad._self_check()
        if errors:
            raise ValueError(errors[0])
