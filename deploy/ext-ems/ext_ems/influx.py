"""Write takeover/release telemetry to InfluxDB.

Measurement `external_ems` (tag `site_id`) with fields:
  * active_source        1 = following external, 0 = self-consumption (takeover)
  * pcc_setpoint_kw       the effective PCC target the gateway is applying
  * last_external_age_s   seconds since the last fresh external message (-1 = none)

The live plot adds an `active_source` trace so takeover/release shows up against
PCC power and SoC. If no token is configured the writer is a no-op (logs only),
so the gateway still runs in environments without InfluxDB.
"""

from __future__ import annotations

import logging

log = logging.getLogger("ext_ems.influx")

MEASUREMENT = "external_ems"


class InfluxStateWriter:
    def __init__(self, url: str, org: str, bucket: str, token: str, site_id: str):
        self._bucket = bucket
        self._org = org
        self._site_id = site_id
        self._enabled = bool(token)
        self._client = None
        self._write_api = None
        if not self._enabled:
            log.warning("INFLUX_TOKEN empty -> external_ems telemetry disabled (no-op writer)")
            return
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        self._Point = __import__("influxdb_client", fromlist=["Point"]).Point
        self._client = InfluxDBClient(url=url, token=token, org=org)
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    def write(self, fields: dict) -> None:
        if not self._enabled:
            return
        try:
            p = self._Point(MEASUREMENT).tag("site_id", self._site_id)
            for k, v in fields.items():
                p = p.field(k, float(v))
            self._write_api.write(bucket=self._bucket, org=self._org, record=p)
        except Exception as exc:  # noqa: BLE001 - telemetry loss must not kill the gateway
            log.warning("influx write failed: %s", exc)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
