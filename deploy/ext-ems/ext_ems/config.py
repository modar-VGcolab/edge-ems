"""Env-driven settings for the external-EMS container.

All durations are wall-clock seconds *after* applying TIME_SCALE: a TIME_SCALE
of 60 turns the default 5-min publish / 6-min watchdog / 10-min reconnect
scenario into 5 s / 6 s / 10 s, so a full takeover+release run fits in ~25 s.
The unscaled defaults (the table in the implementation plan) are the real-rig
timings; scaling only divides them for fast test runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    site_id: str = field(default_factory=lambda: _s("SITE_ID", "vgcolab-01"))

    mqtt_broker: str = field(default_factory=lambda: _s("MQTT_BROKER", "mosquitto"))
    mqtt_port: int = field(default_factory=lambda: _i("MQTT_PORT", 1883))
    mqtt_username: str = field(default_factory=lambda: _s("MQTT_USERNAME", ""))
    mqtt_password: str = field(default_factory=lambda: _s("MQTT_PASSWORD", ""))

    controller_url: str = field(
        default_factory=lambda: _s("CONTROLLER_URL", "http://controller:5000")
    )

    influx_url: str = field(default_factory=lambda: _s("INFLUX_URL", "http://influxdb:8086"))
    influx_org: str = field(default_factory=lambda: _s("INFLUX_ORG", "edge"))
    influx_bucket: str = field(default_factory=lambda: _s("INFLUX_BUCKET", "edge_ems"))
    influx_token: str = field(default_factory=lambda: _s("INFLUX_TOKEN", ""))

    # Raw (unscaled) timings — minutes-scale on the real rig.
    publish_interval_s: float = field(default_factory=lambda: _f("EXT_PUBLISH_INTERVAL_S", 300.0))
    watchdog_timeout_s: float = field(default_factory=lambda: _f("EXT_WATCHDOG_TIMEOUT_S", 360.0))
    outage_at_s: float = field(default_factory=lambda: _f("EXT_OUTAGE_AT_S", 600.0))
    reconnect_at_s: float = field(default_factory=lambda: _f("EXT_RECONNECT_AT_S", 1200.0))

    time_scale: float = field(default_factory=lambda: _f("TIME_SCALE", 1.0))

    # Random P* range, kept inside the PCC export/import limits.
    p_min_kw: float = field(default_factory=lambda: _f("EXT_P_MIN_KW", -300.0))
    p_max_kw: float = field(default_factory=lambda: _f("EXT_P_MAX_KW", 400.0))

    # Effective PCC target while the gateway runs self-consumption.
    self_consumption_kw: float = field(default_factory=lambda: _f("EXT_SELF_CONSUMPTION_KW", 0.0))

    status_port: int = field(default_factory=lambda: _i("STATUS_PORT", 5010))

    # Gateway tick cadence (how often it re-evaluates freshness). Defaults to 1 s
    # of *scaled* time so fast runs stay responsive.
    tick_interval_s: float = field(default_factory=lambda: _f("EXT_TICK_INTERVAL_S", 1.0))

    log_level: str = field(default_factory=lambda: _s("LOG_LEVEL", "INFO"))

    @property
    def external_topic(self) -> str:
        return f"site/{self.site_id}/external/pcc_setpoint"

    # --- scaled views ---------------------------------------------------------
    # TIME_SCALE compresses the scenario: scaled = raw / TIME_SCALE.

    def scale(self, seconds: float) -> float:
        scale = self.time_scale if self.time_scale > 0 else 1.0
        return seconds / scale

    @property
    def publish_interval(self) -> float:
        return self.scale(self.publish_interval_s)

    @property
    def watchdog_timeout(self) -> float:
        return self.scale(self.watchdog_timeout_s)

    @property
    def outage_at(self) -> float:
        return self.scale(self.outage_at_s)

    @property
    def reconnect_at(self) -> float:
        return self.scale(self.reconnect_at_s)


def load_settings() -> Settings:
    return Settings()
