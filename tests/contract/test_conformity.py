from common.conformity import (
    setpoint_topic,
    validate_influx_fields,
    validate_setpoint_payload,
)


def _payload(**overrides):
    base = {
        "data_model_version": "0.1",
        "site_id": "vgcolab-01",
        "ts": "2026-06-12T14:03:22.512Z",
        "seq": 48211,
        "asset_class": "battery",
        "setpoints": {"active_power_setpoint_kw": -312.5, "reactive_power_setpoint_kvar": 0.0},
    }
    base.update(overrides)
    return base


def test_topic_format():
    assert setpoint_topic("vgcolab-01", "battery") == "site/vgcolab-01/setpoints/battery"


def test_valid_battery_payload_passes(dm):
    assert validate_setpoint_payload(_payload(), dm) == []


def test_version_mismatch_caught(dm):
    errors = validate_setpoint_payload(_payload(data_model_version="9.9"), dm)
    assert any("mismatch" in e for e in errors)


def test_missing_keys_caught(dm):
    p = _payload()
    del p["seq"]
    errors = validate_setpoint_payload(p, dm)
    assert any("missing" in e for e in errors)


def test_input_point_as_setpoint_rejected(dm):
    errors = validate_setpoint_payload(_payload(setpoints={"soc_pct": 50}), dm)
    assert any("soc_pct" in e for e in errors)


def test_wrong_class_setpoint_rejected(dm):
    errors = validate_setpoint_payload(
        _payload(asset_class="pv", setpoints={"active_power_setpoint_kw": -10}), dm
    )
    assert any("active_power_setpoint_kw" in e for e in errors)


def test_non_numeric_setpoint_rejected(dm):
    errors = validate_setpoint_payload(_payload(setpoints={"active_power_setpoint_kw": "full"}), dm)
    assert any("numeric" in e for e in errors)


def test_influx_fields_for_class_measurement(dm):
    assert validate_influx_fields("battery", ["soc_pct", "active_power_kw"], dm) == []
    assert validate_influx_fields("battery", ["active_power_setpoint_kw"], dm) != []


def test_control_measurement_is_free_form(dm):
    assert validate_influx_fields("control", ["pcc_error_kw", "loop_duration_ms"], dm) == []
