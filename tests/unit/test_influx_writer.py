import time

import pytest
from core.adapters.base import COMM_FAIL, GOOD, PointValue
from core.influx_writer import InfluxWriter


class FakeWriteApi:
    def __init__(self):
        self.calls = []

    def write(self, bucket, org, record):
        self.calls.append((bucket, org, record))


@pytest.fixture()
def writer(dm):
    return InfluxWriter(
        dm, site_id="vgcolab-01", bucket="edge_ems", org="edge", write_api=FakeWriteApi()
    )


def _pv(value, quality=GOOD):
    return PointValue(value, time.time(), quality)


def test_asset_point_line_protocol(writer):
    p = writer.build_asset_point(
        "battery", "bess-01", {"soc_pct": _pv(57.5), "active_power_kw": _pv(-312.5)}
    )
    line = p.to_line_protocol()
    assert line.startswith("battery,")
    assert "site_id=vgcolab-01" in line
    assert "asset_id=bess-01" in line
    assert "quality=GOOD" in line
    assert "soc_pct=57.5" in line
    assert "active_power_kw=-312.5" in line


def test_incomplete_record_tagged_comm_fail(writer):
    p = writer.build_asset_point(
        "battery", "bess-01", {"soc_pct": _pv(57.5), "active_power_kw": _pv(None, COMM_FAIL)}
    )
    line = p.to_line_protocol()
    assert "quality=COMM_FAIL" in line
    assert "soc_pct=57.5" in line
    assert "active_power_kw" not in line  # bad field not written


def test_no_good_fields_skips_record(writer):
    p = writer.build_asset_point("battery", "bess-01", {"soc_pct": _pv(None, COMM_FAIL)})
    assert p is None


def test_non_canonical_field_rejected(writer):
    with pytest.raises(ValueError, match="banana"):
        writer.build_asset_point("battery", "bess-01", {"banana_count": _pv(7.0)})


def test_setpoint_as_measurement_field_rejected(writer):
    with pytest.raises(ValueError, match="active_power_setpoint_kw"):
        writer.build_asset_point("battery", "bess-01", {"active_power_setpoint_kw": _pv(-10.0)})


def test_aggregate_point(writer):
    p = writer.build_aggregate_point("battery", {"soc_pct": _pv(65.0)})
    line = p.to_line_protocol()
    assert line.startswith("aggregate,")
    assert "asset_class=battery" in line
    assert "soc_pct=65" in line


def test_write_cycle_batches_and_skips_none(writer):
    p1 = writer.build_asset_point("battery", "bess-01", {"soc_pct": _pv(57.5)})
    n = writer.write_cycle([p1, None, None])
    assert n == 1
    fake = writer._write_api
    assert len(fake.calls) == 1
    bucket, org, record = fake.calls[0]
    assert bucket == "edge_ems" and org == "edge" and len(record) == 1


def test_write_cycle_with_nothing_writes_nothing(writer):
    assert writer.write_cycle([None, None]) == 0
    assert writer._write_api.calls == []
