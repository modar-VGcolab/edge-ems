import copy

import pytest
from common.config_models import (
    check_controller_requirements,
    validate_asset_config,
)


def test_example_config_validates(asset_config_raw, dm):
    cfg = validate_asset_config(asset_config_raw, dm)
    assert cfg.site.id == "vgcolab-01"
    assert len(cfg.assets) == 5


def test_example_satisfies_controller_requirements(asset_config_raw, dm):
    cfg = validate_asset_config(asset_config_raw, dm)
    assert check_controller_requirements(cfg) == []


def test_version_mismatch_rejected(asset_config_raw, dm):
    bad = copy.deepcopy(asset_config_raw)
    bad["data_model_version"] = "9.9"
    with pytest.raises(ValueError, match="mismatch"):
        validate_asset_config(bad, dm)


def test_duplicate_asset_id_rejected(asset_config_raw, dm):
    bad = copy.deepcopy(asset_config_raw)
    bad["assets"][1]["id"] = bad["assets"][0]["id"]
    with pytest.raises(ValueError, match="duplicate"):
        validate_asset_config(bad, dm)


def test_unknown_limit_key_rejected(asset_config_raw, dm):
    bad = copy.deepcopy(asset_config_raw)
    bad["assets"][0]["limits"]["max_bananas"] = 7
    with pytest.raises(ValueError, match="max_bananas"):
        validate_asset_config(bad, dm)


def test_unknown_nominal_key_rejected(asset_config_raw, dm):
    bad = copy.deepcopy(asset_config_raw)
    bad["assets"][1]["nominal"]["capacity_mwh"] = 2
    with pytest.raises(ValueError, match="capacity_mwh"):
        validate_asset_config(bad, dm)


def test_wrong_control_for_class_rejected(asset_config_raw, dm):
    bad = copy.deepcopy(asset_config_raw)
    # PV is derate-controlled; power_setpoint must be rejected
    bad["assets"][2]["flexibility"]["control"] = "power_setpoint"
    with pytest.raises(ValueError, match="flexibility.control"):
        validate_asset_config(bad, dm)


def test_display_name_as_id_rejected(asset_config_raw, dm):
    bad = copy.deepcopy(asset_config_raw)
    bad["assets"][0]["id"] = "PCC Monitor"  # spaces/uppercase: the old fragile style
    with pytest.raises(Exception):
        validate_asset_config(bad, dm)


def test_missing_battery_fails_controller_requirements(asset_config_raw, dm):
    raw = copy.deepcopy(asset_config_raw)
    raw["assets"] = [a for a in raw["assets"] if a["class"] != "battery"]
    cfg = validate_asset_config(raw, dm)
    errors = check_controller_requirements(cfg)
    assert any("battery" in e for e in errors)
