"""Conformity checks for everything crossing an interface (MQTT, InfluxDB)."""

from __future__ import annotations

from numbers import Number

from common.data_model import DataModel

SETPOINT_TOPIC_FMT = "site/{site_id}/setpoints/{asset_class}"

_REQUIRED_PAYLOAD_KEYS = {
    "data_model_version",
    "site_id",
    "ts",
    "seq",
    "asset_class",
    "setpoints",
}


def setpoint_topic(site_id: str, asset_class: str) -> str:
    return SETPOINT_TOPIC_FMT.format(site_id=site_id, asset_class=asset_class)


def validate_setpoint_payload(payload: dict, dm: DataModel) -> list[str]:
    """Return error strings for a setpoint MQTT payload. Empty list = conformant."""
    errors: list[str] = []
    missing = _REQUIRED_PAYLOAD_KEYS - payload.keys()
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")
        return errors
    if str(payload["data_model_version"]) != dm.version:
        errors.append(
            f"data_model_version mismatch: payload '{payload['data_model_version']}' "
            f"vs model '{dm.version}'"
        )
    if not isinstance(payload["seq"], int) or payload["seq"] < 0:
        errors.append("seq must be a non-negative integer")
    asset_class = payload["asset_class"]
    setpoints = payload["setpoints"]
    if not isinstance(setpoints, dict) or not setpoints:
        errors.append("setpoints must be a non-empty mapping")
        return errors
    errors.extend(dm.validate_fields(asset_class, setpoints.keys(), direction="output"))
    for k, v in setpoints.items():
        if not isinstance(v, Number) or isinstance(v, bool):
            errors.append(f"setpoint '{k}' must be numeric, got {type(v).__name__}")
    return errors


def validate_influx_fields(measurement: str, fields: list[str], dm: DataModel) -> list[str]:
    """Validate field names for a per-asset-class measurement write."""
    if measurement == "aggregate":
        return ["aggregate fields are validated per asset_class tag, not measurement"]
    if measurement == "control":
        return []  # controller-internal telemetry, not part of the asset ontology
    return dm.validate_fields(measurement, fields, direction="input")
