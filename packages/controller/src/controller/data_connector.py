"""Controller data path (plan task 17).

InfluxAggregateReader: last-value reads of the `aggregate` measurement with
staleness marking (system design §3.3, §5). The Flux query function is
injectable so the parsing/staleness logic is unit-testable without a database;
the real query path is exercised in SIL.

MqttSetpointPublisher: seq-numbered, conformity-validated payloads on
`site/{site_id}/setpoints/{asset_class}` (QoS 1), with a bounded latest-wins
queue per topic for broker outages (system design §3.2, §5).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from common.conformity import setpoint_topic, validate_setpoint_payload
from common.data_model import DataModel
from common.points import GOOD, STALE, PointValue

# (asset_class, field, value, ts_unix)
Row = tuple[str, str, float, float]

FLUX_LAST_AGGREGATES = """
from(bucket: "{bucket}")
  |> range(start: -{window}s)
  |> filter(fn: (r) => r._measurement == "aggregate" and r.site_id == "{site_id}")
  |> last()
"""


class InfluxAggregateReader:
    def __init__(
        self,
        dm: DataModel,
        site_id: str,
        bucket: str = "edge_ems",
        timeout_s: float = 2.0,
        window_s: float = 60.0,
        query_rows: Callable[[], Iterable[Row]] | None = None,
        url: str | None = None,
        token: str | None = None,
        org: str | None = None,
    ):
        self._dm = dm
        self._site_id = site_id
        self._bucket = bucket
        self._timeout = timeout_s
        self._window = window_s
        if query_rows is not None:
            self._query_rows = query_rows
            self._client = None
        else:
            from influxdb_client import InfluxDBClient

            self._client = InfluxDBClient(url=url, token=token, org=org)
            self._query_rows = self._query_influx

    def read(
        self, classes: list[str], now: float | None = None
    ) -> dict[str, dict[str, PointValue]]:
        """Latest aggregate per class; points older than timeout come back STALE.

        A class with no rows at all yields an empty dict — the controller treats
        that as COMM_FAIL for its required inputs (HOLD/SAFE path).
        """
        now = time.time() if now is None else now
        out: dict[str, dict[str, PointValue]] = {c: {} for c in classes}
        for asset_class, fld, value, ts in self._query_rows():
            bucket = out.get(asset_class)
            if bucket is None:
                continue
            if self._dm.validate_fields(asset_class, [fld], direction="input"):
                continue  # non-canonical field: never let it reach control logic
            quality = GOOD if (now - ts) <= self._timeout else STALE
            bucket[fld] = PointValue(value, ts, quality)
        return out

    def _query_influx(self) -> Iterable[Row]:  # pragma: no cover - exercised in SIL
        flux = FLUX_LAST_AGGREGATES.format(
            bucket=self._bucket, window=int(self._window), site_id=self._site_id
        )
        for table in self._client.query_api().query(flux):
            for record in table.records:
                yield (
                    record.values.get("asset_class", ""),
                    record.get_field(),
                    float(record.get_value()),
                    record.get_time().timestamp(),
                )


class MqttSetpointPublisher:
    """The paho client is injectable; anything with `.publish(topic, payload, qos)`
    returning an object with `.rc == 0` on success works (fake in unit tests)."""

    def __init__(self, dm: DataModel, site_id: str, client, data_model_version: str | None = None):
        self._dm = dm
        self._site_id = site_id
        self._client = client
        self._version = data_model_version or dm.version
        self._seq = 0
        self._pending: dict[str, str] = {}  # topic -> latest unsent payload (latest wins)

    @property
    def pending_topics(self) -> list[str]:
        return sorted(self._pending)

    def publish_setpoints(
        self, asset_class: str, setpoints: dict[str, float], now: float | None = None
    ) -> bool:
        """Build, validate, and publish one setpoint message. Returns False if the
        broker is unreachable (message parked in the latest-wins queue)."""
        self._seq += 1
        payload = {
            "data_model_version": self._version,
            "site_id": self._site_id,
            "ts": datetime.fromtimestamp(now or time.time(), tz=timezone.utc).isoformat(),
            "seq": self._seq,
            "asset_class": asset_class,
            "setpoints": setpoints,
        }
        errors = validate_setpoint_payload(payload, self._dm)
        if errors:
            raise ValueError(f"refusing to publish non-conformant payload: {errors}")
        topic = setpoint_topic(self._site_id, asset_class)
        body = json.dumps(payload)
        if self._try(topic, body):
            self.flush_pending()
            return True
        self._pending[topic] = body
        return False

    def flush_pending(self) -> int:
        """Re-send parked messages (freshest only, one per topic). Returns sent count."""
        sent = 0
        for topic in list(self._pending):
            if self._try(topic, self._pending[topic]):
                del self._pending[topic]
                sent += 1
        return sent

    def _try(self, topic: str, body: str) -> bool:
        try:
            info = self._client.publish(topic, body, qos=1)
            return getattr(info, "rc", 1) == 0
        except Exception:  # noqa: BLE001 - broker loss must not kill the loop
            return False
