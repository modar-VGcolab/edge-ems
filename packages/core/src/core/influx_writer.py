"""InfluxDB writer (plan task 14).

Field names are validated against the data model before anything is written —
core.py physically cannot put a non-canonical field in the database.

Record quality tag: GOOD when every requested point contributed a value,
COMM_FAIL when the record is incomplete (only the good fields are written;
gaps in time + the tag convey degradation to the controller's staleness check).
Assets with zero good points produce no record at all.
"""

from __future__ import annotations

from common.conformity import validate_influx_fields
from common.data_model import DataModel
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from core.adapters.base import COMM_FAIL, GOOD, PointValue

AGGREGATE_MEASUREMENT = "aggregate"


class InfluxWriter:
    def __init__(
        self,
        dm: DataModel,
        site_id: str,
        bucket: str,
        org: str,
        url: str | None = None,
        token: str | None = None,
        write_api=None,  # injectable for tests
    ):
        self._dm = dm
        self._site_id = site_id
        self._bucket = bucket
        self._org = org
        if write_api is not None:
            self._client = None
            self._write_api = write_api
        else:
            self._client = InfluxDBClient(url=url, token=token, org=org)
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    # -- point builders --------------------------------------------------------

    def build_asset_point(
        self, asset_class: str, asset_id: str, values: dict[str, PointValue]
    ) -> Point | None:
        good = {n: pv for n, pv in values.items() if pv.quality == GOOD and pv.value is not None}
        if not good:
            return None
        errors = validate_influx_fields(asset_class, list(good), self._dm)
        if errors:
            raise ValueError(f"non-canonical fields for '{asset_class}': {errors}")
        quality = GOOD if len(good) == len(values) else COMM_FAIL
        p = (
            Point(asset_class)
            .tag("site_id", self._site_id)
            .tag("asset_id", asset_id)
            .tag("quality", quality)
        )
        for name, pv in good.items():
            p = p.field(name, float(pv.value))
        p = p.time(int(max(pv.ts for pv in good.values()) * 1e9))
        return p

    def build_aggregate_point(
        self, asset_class: str, values: dict[str, PointValue]
    ) -> Point | None:
        good = {n: pv for n, pv in values.items() if pv.quality == GOOD and pv.value is not None}
        if not good:
            return None
        errors = self._dm.validate_fields(asset_class, list(good), direction="input")
        if errors:
            raise ValueError(f"non-canonical aggregate fields: {errors}")
        quality = GOOD if len(good) == len(values) else COMM_FAIL
        p = (
            Point(AGGREGATE_MEASUREMENT)
            .tag("site_id", self._site_id)
            .tag("asset_class", asset_class)
            .tag("quality", quality)
        )
        for name, pv in good.items():
            p = p.field(name, float(pv.value))
        p = p.time(int(max(pv.ts for pv in good.values()) * 1e9))
        return p

    # -- write path -------------------------------------------------------------

    def write_cycle(self, points: list[Point | None]) -> int:
        """Write one control cycle's records in a single batched call."""
        real = [p for p in points if p is not None]
        if real:
            self._write_api.write(bucket=self._bucket, org=self._org, record=real)
        return len(real)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
