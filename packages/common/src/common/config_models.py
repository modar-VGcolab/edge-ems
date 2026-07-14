"""Pydantic models + cross-validation against the data model for both config files."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.data_model import DataModel

_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- asset_config


class Flexibility(StrictModel):
    control: Literal["power_setpoint", "derate", "none"] = "none"
    limits: dict[str, float] = Field(default_factory=dict)


class CommConfig(StrictModel):
    protocol: Literal["modbus_tcp"] = "modbus_tcp"
    host: str
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=0, le=255)
    register_map: str


class SiteConfig(StrictModel):
    id: str = Field(pattern=_ID_PATTERN)
    name: str = ""
    location: tuple[float, float] | None = None


class AssetEntry(StrictModel):
    id: str = Field(pattern=_ID_PATTERN)
    name: str = ""
    asset_class: str = Field(alias="class")
    state: Literal["active", "inactive"] = "active"
    flexibility: Flexibility = Field(default_factory=Flexibility)
    nominal: dict[str, float] = Field(default_factory=dict)
    limits: dict[str, float] = Field(default_factory=dict)
    comm: CommConfig | None = None


class AssetConfigFile(StrictModel):
    data_model_version: str
    site: SiteConfig
    assets: list[AssetEntry]


def validate_asset_config(raw: dict, dm: DataModel) -> AssetConfigFile:
    """Parse and cross-validate a site config. Raises ValueError on any error."""
    cfg = AssetConfigFile.model_validate(raw)
    errors: list[str] = []
    if cfg.data_model_version != dm.version:
        errors.append(
            f"data_model_version mismatch: config has '{cfg.data_model_version}', "
            f"model is '{dm.version}'"
        )
    seen: set[str] = set()
    for a in cfg.assets:
        if a.id in seen:
            errors.append(f"duplicate asset id '{a.id}'")
        seen.add(a.id)
        cls_ = dm.asset_classes.get(a.asset_class)
        if cls_ is None:
            errors.append(f"{a.id}: unknown asset class '{a.asset_class}'")
            continue
        if a.flexibility.control not in ("none", cls_.control):
            errors.append(
                f"{a.id}: flexibility.control '{a.flexibility.control}' not allowed "
                f"for class '{a.asset_class}' (expected 'none' or '{cls_.control}')"
            )
        for key in list(a.limits) + list(a.flexibility.limits):
            if key not in cls_.limits_schema:
                errors.append(f"{a.id}: unknown limit '{key}' for class '{a.asset_class}'")
        for key in a.nominal:
            if key not in cls_.nominal_schema:
                errors.append(f"{a.id}: unknown nominal '{key}' for class '{a.asset_class}'")
    if errors:
        raise ValueError("asset config invalid: " + "; ".join(errors))
    return cfg


def check_controller_requirements(cfg: AssetConfigFile) -> list[str]:
    """The controller requires exactly one active PCC and at least one active battery."""
    errors: list[str] = []
    pccs = [a for a in cfg.assets if a.asset_class == "pcc" and a.state == "active"]
    batteries = [a for a in cfg.assets if a.asset_class == "battery" and a.state == "active"]
    if len(pccs) != 1:
        errors.append(f"controller requires exactly 1 active pcc, found {len(pccs)}")
    if not batteries:
        errors.append("controller requires at least 1 active battery")
    return errors


# ------------------------------------------------------------- edge_ems_config


class InfluxConfig(StrictModel):
    url: str
    org: str
    bucket: str
    token: str


class MqttConfig(StrictModel):
    broker_address: str
    broker_port: int = Field(default=1883, ge=1, le=65535)
    client_id: str
    username: str = ""
    password: str = ""


class ControllerSettings(StrictModel):
    update_period: float = Field(default=1.0, gt=0)
    Kp: float = 0.5
    Ki: float = 0.1
    Kd: float = Field(default=0.0, ge=0)  # derivative gain (on measurement); 0 = pure PI
    deriv_filter_tau: float = Field(default=0.0, ge=0)  # s; low-pass on the derivative
    pv_feedforward_gain: float = Field(default=0.0, ge=0, le=1)  # PV disturbance feedforward
    timeout_period: float = Field(default=2.0, gt=0)
    hold_max_s: float = Field(default=10.0, ge=0)
    slew_limit_kw_s: float | None = Field(default=None, gt=0)
    pcc_setpoint_kw: float = 0.0  # PI target at the PCC (0 = self-consumption; <0 = export)


def _check_len4(name: str, v: list[float]) -> list[float]:
    if len(v) != 4:
        raise ValueError(f"{name} must have exactly 4 values")
    return v


def _check_nondecreasing(name: str, v: list[float]) -> list[float]:
    if any(b < a for a, b in zip(v, v[1:])):
        raise ValueError(f"{name} must be monotonically non-decreasing")
    return v


def _check_strictly_increasing(name: str, v: list[float]) -> list[float]:
    if any(b <= a for a, b in zip(v, v[1:])):
        raise ValueError(f"{name} must be strictly increasing")
    return v


class PfDroop(StrictModel):
    enabled: bool = False
    dP_f: list[float]
    dQ_f: list[float]
    f: list[float]

    @field_validator("dP_f", "dQ_f")
    @classmethod
    def _dy(cls, v: list[float], info) -> list[float]:
        return _check_nondecreasing(info.field_name, _check_len4(info.field_name, v))

    @field_validator("f")
    @classmethod
    def _dx(cls, v: list[float], info) -> list[float]:
        return _check_strictly_increasing(info.field_name, _check_len4(info.field_name, v))


class QvDroop(StrictModel):
    enabled: bool = False
    dP_V: list[float]
    dQ_V: list[float]
    V: list[float]

    @field_validator("dP_V", "dQ_V")
    @classmethod
    def _dy(cls, v: list[float], info) -> list[float]:
        return _check_nondecreasing(info.field_name, _check_len4(info.field_name, v))

    @field_validator("V")
    @classmethod
    def _dx(cls, v: list[float], info) -> list[float]:
        return _check_strictly_increasing(info.field_name, _check_len4(info.field_name, v))


class DroopConfig(StrictModel):
    enabled: bool = False
    p_f_droop: PfDroop
    q_v_droop: QvDroop


_HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class PfcTarget(StrictModel):
    """A power-factor goal: a target magnitude in (0, 1] and which side of unity.

    mode: unity -> no reactive command (Q = 0); lagging -> the site presents an
    inductive PF at the PCC (positive/inductive reactive per data_model.yaml);
    leading -> capacitive PF (negative reactive). pf_target is ignored when
    mode == 'unity'.
    """

    pf_target: float = Field(default=1.0, gt=0.0, le=1.0)
    mode: Literal["unity", "lagging", "leading"] = "unity"


class PfcWindow(PfcTarget):
    """A tariff time-of-day window mapping to a PF goal. `start`/`end` are local
    wall-clock 'HH:MM'; a window with start > end wraps past midnight."""

    start: str = Field(pattern=_HHMM_PATTERN)
    end: str = Field(pattern=_HHMM_PATTERN)

    @field_validator("start", "end")
    @classmethod
    def _not_equal_placeholder(cls, v: str) -> str:  # format already enforced by pattern
        return v


class PfcConfig(StrictModel):
    """Tariff-driven power-factor control at the PCC.

    The active window (by local time) selects a PF goal; the controller converts
    it to a battery reactive setpoint. `q_sign_convention` (+1/-1) calibrates the
    battery VarSet direction against the live PCC meter on the rig -- like the
    other sign flags, it may need flipping once verified. `s_rated_kva`, when set,
    clamps the reactive command to the battery inverter capability circle
    sqrt(S^2 - P^2); omit it to disable the clamp.
    """

    enabled: bool = False
    q_sign_convention: float = 1.0
    s_rated_kva: float | None = Field(default=None, gt=0)
    default: PfcTarget = Field(default_factory=PfcTarget)
    windows: list[PfcWindow] = Field(default_factory=list)

    @field_validator("q_sign_convention")
    @classmethod
    def _sign(cls, v: float) -> float:
        if v not in (1.0, -1.0):
            raise ValueError("q_sign_convention must be +1 or -1")
        return v


class AggregationSpec(StrictModel):
    measurements: list[str]
    setpoint_type: str


class HttpApiConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=5000, ge=1, le=65535)
    debug: bool = False


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str | None = None
    max_file_size: int | None = None
    backup_count: int | None = None


class EdgeEmsConfigFile(StrictModel):
    data_model_version: str
    influxdb: InfluxConfig
    mqtt: MqttConfig
    controller: ControllerSettings
    droop: DroopConfig
    pfc: PfcConfig = Field(default_factory=PfcConfig)
    asset_aggregation: dict[str, AggregationSpec]
    http_api: HttpApiConfig = Field(default_factory=HttpApiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def validate_edge_ems_config(raw: dict, dm: DataModel) -> EdgeEmsConfigFile:
    """Parse and cross-validate the runtime config. Raises ValueError on any error."""
    cfg = EdgeEmsConfigFile.model_validate(raw)
    errors: list[str] = []
    if cfg.data_model_version != dm.version:
        errors.append(
            f"data_model_version mismatch: config has '{cfg.data_model_version}', "
            f"model is '{dm.version}'"
        )
    for cname, spec in cfg.asset_aggregation.items():
        errors.extend(dm.validate_fields(cname, spec.measurements, direction="input"))
    if cfg.controller.timeout_period < cfg.controller.update_period:
        errors.append("controller.timeout_period must be >= update_period")
    if errors:
        raise ValueError("edge ems config invalid: " + "; ".join(errors))
    return cfg
