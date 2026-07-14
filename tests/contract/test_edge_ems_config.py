import copy

import pytest
from common.config_models import validate_edge_ems_config


def test_example_config_validates(edge_ems_config_raw, dm):
    cfg = validate_edge_ems_config(edge_ems_config_raw, dm)
    assert cfg.controller.update_period == 1.0
    assert set(cfg.asset_aggregation) == {"battery", "pv", "flexible_load", "pcc"}


def test_version_mismatch_rejected(edge_ems_config_raw, dm):
    bad = copy.deepcopy(edge_ems_config_raw)
    bad["data_model_version"] = "9.9"
    with pytest.raises(ValueError, match="mismatch"):
        validate_edge_ems_config(bad, dm)


def test_non_monotonic_droop_x_rejected(edge_ems_config_raw, dm):
    bad = copy.deepcopy(edge_ems_config_raw)
    bad["droop"]["p_f_droop"]["f"] = [49.5, 49.8, 49.8, 50.5]  # not strictly increasing
    with pytest.raises(Exception, match="strictly increasing"):
        validate_edge_ems_config(bad, dm)


def test_decreasing_droop_y_rejected(edge_ems_config_raw, dm):
    bad = copy.deepcopy(edge_ems_config_raw)
    bad["droop"]["q_v_droop"]["dQ_V"] = [0.1, 0.05, -0.05, -0.1]
    with pytest.raises(Exception, match="non-decreasing"):
        validate_edge_ems_config(bad, dm)


def test_wrong_droop_array_length_rejected(edge_ems_config_raw, dm):
    bad = copy.deepcopy(edge_ems_config_raw)
    bad["droop"]["p_f_droop"]["dP_f"] = [-0.1, 0.0, 0.1]
    with pytest.raises(Exception, match="4 values"):
        validate_edge_ems_config(bad, dm)


def test_unknown_aggregation_measurement_rejected(edge_ems_config_raw, dm):
    bad = copy.deepcopy(edge_ems_config_raw)
    bad["asset_aggregation"]["battery"]["measurements"].append("temperature_c")
    with pytest.raises(ValueError, match="temperature_c"):
        validate_edge_ems_config(bad, dm)


def test_setpoint_as_aggregation_measurement_rejected(edge_ems_config_raw, dm):
    bad = copy.deepcopy(edge_ems_config_raw)
    # outputs are not measurements
    bad["asset_aggregation"]["pv"]["measurements"].append("derate_factor_setpoint")
    with pytest.raises(ValueError, match="derate_factor_setpoint"):
        validate_edge_ems_config(bad, dm)


def test_timeout_shorter_than_period_rejected(edge_ems_config_raw, dm):
    bad = copy.deepcopy(edge_ems_config_raw)
    bad["controller"]["timeout_period"] = 0.5
    with pytest.raises(ValueError, match="timeout_period"):
        validate_edge_ems_config(bad, dm)
