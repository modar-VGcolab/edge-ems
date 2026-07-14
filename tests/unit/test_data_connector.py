import json
import time

import pytest
from common.points import GOOD, STALE
from controller.data_connector import InfluxAggregateReader, MqttSetpointPublisher

NOW = 1_000_000.0


def _reader(dm, rows):
    return InfluxAggregateReader(dm, "vgcolab-01", timeout_s=2.0, query_rows=lambda: rows)


def test_fresh_rows_are_good(dm):
    rows = [("battery", "soc_pct", 57.5, NOW - 0.5)]
    out = _reader(dm, rows).read(["battery"], now=NOW)
    pv = out["battery"]["soc_pct"]
    assert pv.value == 57.5
    assert pv.quality == GOOD


def test_old_rows_marked_stale(dm):
    rows = [("battery", "soc_pct", 57.5, NOW - 5.0)]
    out = _reader(dm, rows).read(["battery"], now=NOW)
    assert out["battery"]["soc_pct"].quality == STALE


def test_unrequested_class_ignored_and_missing_class_empty(dm):
    rows = [("pv", "active_power_kw", -100.0, NOW)]
    out = _reader(dm, rows).read(["battery"], now=NOW)
    assert out == {"battery": {}}


def test_non_canonical_field_never_reaches_control(dm):
    rows = [("battery", "banana_count", 7.0, NOW), ("battery", "soc_pct", 50.0, NOW)]
    out = _reader(dm, rows).read(["battery"], now=NOW)
    assert "banana_count" not in out["battery"]
    assert out["battery"]["soc_pct"].value == 50.0


class FakeMqtt:
    def __init__(self):
        self.published = []  # (topic, payload_dict)
        self.fail = False

    def publish(self, topic, payload, qos):
        class Info:
            rc = 1 if self.fail else 0

        if not self.fail:
            self.published.append((topic, json.loads(payload)))
        return Info()


def test_publish_builds_conformant_payload(dm):
    mqtt = FakeMqtt()
    pub = MqttSetpointPublisher(dm, "vgcolab-01", mqtt)
    ok = pub.publish_setpoints("battery", {"active_power_setpoint_kw": -312.5}, now=time.time())
    assert ok
    topic, payload = mqtt.published[0]
    assert topic == "site/vgcolab-01/setpoints/battery"
    assert payload["seq"] == 1
    assert payload["setpoints"]["active_power_setpoint_kw"] == -312.5


def test_seq_is_monotonic(dm):
    mqtt = FakeMqtt()
    pub = MqttSetpointPublisher(dm, "vgcolab-01", mqtt)
    pub.publish_setpoints("battery", {"active_power_setpoint_kw": -1.0})
    pub.publish_setpoints("battery", {"active_power_setpoint_kw": -2.0})
    seqs = [p["seq"] for _, p in mqtt.published]
    assert seqs == [1, 2]


def test_invalid_setpoint_name_raises_instead_of_publishing(dm):
    mqtt = FakeMqtt()
    pub = MqttSetpointPublisher(dm, "vgcolab-01", mqtt)
    with pytest.raises(ValueError, match="soc_pct"):
        pub.publish_setpoints("battery", {"soc_pct": 50.0})
    assert mqtt.published == []


def test_broker_outage_parks_latest_and_flushes_on_recovery(dm):
    mqtt = FakeMqtt()
    pub = MqttSetpointPublisher(dm, "vgcolab-01", mqtt)
    mqtt.fail = True
    assert not pub.publish_setpoints("battery", {"active_power_setpoint_kw": -10.0})
    assert not pub.publish_setpoints("battery", {"active_power_setpoint_kw": -20.0})
    assert pub.pending_topics == ["site/vgcolab-01/setpoints/battery"]
    mqtt.fail = False
    assert pub.publish_setpoints("battery", {"active_power_setpoint_kw": -30.0})
    payloads = [p["setpoints"]["active_power_setpoint_kw"] for _, p in mqtt.published]
    # -30 (live) then -20 (flushed latest); -10 was superseded and never sent
    assert -30.0 in payloads and -20.0 in payloads and -10.0 not in payloads
    assert pub.pending_topics == []
